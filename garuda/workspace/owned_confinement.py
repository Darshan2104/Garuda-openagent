"""Parent-owned Docker lifetimes for an owned read-only ACP role.

Docker resources live in a daemon, outside the subreaper's descendant tree.
Allocate names before any create, identify our exact containers by nonce label,
and prove their absence separately from local process closure.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from garuda.runtime.private_records import open_directory, read_record
from garuda.runtime.process_supervision import CleanupUncertain, LinuxProcessSupervisor, ProcessRequest
from garuda.runtime.strict_store import preserve_private_backup
from garuda.workspace.confined_acp import _PROBE, ConfinementRefused, _check_mounts, _docker_env, docker_argv


class DockerRoleOwner:
    def __init__(self, workspace, session_id, confinement, command):
        if confinement is None or not command:
            raise ConfinementRefused("a confinement image and command are required")
        _check_mounts(confinement.mounts, Path(workspace).resolve())
        self.docker = shutil.which("docker")
        if self.docker is None:
            raise ConfinementRefused("Docker is not available")
        self.workspace, self.confinement = workspace, confinement
        self.command = confinement.command or tuple(command)
        self.nonce = uuid.uuid4().hex
        self.names = {kind: f"garuda-role-{session_id}-{kind}" for kind in ("probe", "run")}
        self.attempted = []
        self.identities = {}
        self.removed = []
        self._closer = None
        self.session_id = session_id
        self.records = None

    async def _publish(self, call):
        task = asyncio.create_task(asyncio.to_thread(call))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise

    async def persist(self, store, parent_id, snapshot):
        """Record resources before any daemon launch; records never authorize replay."""
        self.records = store.root / "owned-docker" / self.session_id
        self.intent = {"version": 1, "session_id": self.session_id, "parent_id": parent_id,
                       "workspace": self.workspace, "parent_owner": snapshot,
                       "nonce": self.nonce, "names": self.names}

        def write():
            store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root = open_directory(store.root)
            directory = record = None
            try:
                info = os.fstat(root)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                    raise CleanupUncertain("Docker resource store is not privately owned")
                try:
                    os.mkdir("owned-docker", mode=0o700, dir_fd=root)
                except FileExistsError:
                    pass
                directory = os.open("owned-docker", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise CleanupUncertain("Docker resource record directory is not private")
                os.mkdir(self.session_id, mode=0o700, dir_fd=directory)
                record = os.open(self.session_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                preserve_private_backup(record, "intent.json", json.dumps(self.intent, sort_keys=True).encode())
                os.fsync(directory)
                os.fsync(root)
            finally:
                for fd in (record, directory, root):
                    if fd is not None:
                        os.close(fd)

        await self._publish(write)

    async def _command(self, *argv):
        # The CLI itself and any helpers also need bounded descendant closure.
        request = ProcessRequest((self.docker, *argv), "/", timeout=120,
                                 env=tuple(_docker_env().items()), purpose="role")
        async with await LinuxProcessSupervisor().prepare() as process:
            await process.activate(request)
            result = await process.wait()
        if result.outcome != "exited":
            raise CleanupUncertain("Docker resource operation did not settle")
        return result

    async def _record(self, name, document):
        def write():
            directory = open_directory(self.records)
            try:
                if read_record(directory, "intent.json") != self.intent:
                    raise CleanupUncertain("Docker resource intent changed before publication")
                if name == "closure.json":
                    for kind, container_name in self.names.items():
                        if container_name in self.identities and read_record(directory, f"{kind}.json") != {
                                "version": 1, "nonce": self.nonce, "name": container_name,
                                "id": self.identities[container_name]}:
                            raise CleanupUncertain("Docker resource identity record changed before closure")
                preserve_private_backup(directory, name, json.dumps(document, sort_keys=True).encode())
            finally:
                os.close(directory)

        await self._publish(write)

    async def _create(self, kind, command):
        if self.records is None:
            raise CleanupUncertain("Docker resources require a persisted owner before creation")
        name = self.names[kind]
        argv = docker_argv(self.workspace, self.confinement, list(command))
        # Retain the ordinary confinement flags, but creation never starts the
        # command and --rm cannot erase the identity before the owner observes it.
        options = argv[2:]
        options.remove("--rm")
        extra = ["--name", name, "--label", f"garuda.role-owner={self.nonce}", "--pull", "never"]
        if kind == "probe":
            extra += ["--network", "none"]
        self.attempted.append(name)  # includes a create with a lost response
        result = await self._command("create", *extra, *options)
        identity = result.stdout_tail.strip()
        if result.exit_code != 0 or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
            raise ConfinementRefused("the owned confinement container could not be created")
        self.identities[name] = identity
        await self._record(f"{kind}.json", {
            "version": 1, "nonce": self.nonce, "name": name, "id": identity})
        return identity

    async def prepare(self):
        probe = await self._create("probe", ("sh", "-c", _PROBE))
        result = await self._command("start", "--attach", probe)
        if result.exit_code != 0:
            raise ConfinementRefused("the owned confinement probe failed")
        await self._remove(self.names["probe"])
        return await self._create("run", self.command)

    async def _find(self, name):
        result = await self._command("container", "ls", "--all", "--quiet", "--no-trunc",
                                     "--filter", f"name=^/{name}$")
        if result.exit_code != 0:
            raise CleanupUncertain("Docker container absence cannot be verified")
        rows = result.stdout_tail.split()
        if len(rows) > 1 or any(re.fullmatch(r"[0-9a-f]{64}", row) is None for row in rows):
            raise CleanupUncertain("Docker container identity is ambiguous")
        return rows[0] if rows else None

    async def _remove(self, name):
        identity = await self._find(name)
        if identity is not None:
            result = await self._command("inspect", "--format", "{{json .Config.Labels}}", identity)
            try:
                labels = json.loads(result.stdout_tail)
            except ValueError as exc:
                raise CleanupUncertain("Docker resource ownership is unreadable") from exc
            if (result.exit_code != 0 or not isinstance(labels, dict)
                    or labels.get("garuda.role-owner") != self.nonce
                    or (name in self.identities and self.identities[name] != identity)):
                raise CleanupUncertain("Docker resource no longer matches its issued owner")
            result = await self._command("rm", "--force", identity)
            if result.exit_code != 0:
                raise CleanupUncertain("owned Docker container removal is unproved")
        if await self._find(name) is not None:
            raise CleanupUncertain("owned Docker container remains after removal")
        if name not in self.removed:
            self.removed.append(name)

    async def close(self):
        # Cancellation must not abandon a daemon cleanup command halfway through.
        if self._closer is None:
            self._closer = asyncio.create_task(self._close())
        while True:
            try:
                return await asyncio.shield(self._closer)
            except asyncio.CancelledError:
                if self._closer.done():
                    return self._closer.result()

    async def _close(self):
        failed = False
        for name in self.attempted:
            try:
                await self._remove(name)
            except Exception:
                failed = True
        if failed:
            raise CleanupUncertain("owned Docker resource closure is unproved")
        evidence = {"backend": "docker", "absent": list(self.removed)}
        if self.records is not None:
            try:
                await self._record("closure.json", {"version": 1, "nonce": self.nonce, **evidence})
            except Exception as exc:
                raise CleanupUncertain("Docker resource closure publication is unproved") from exc
        return evidence
