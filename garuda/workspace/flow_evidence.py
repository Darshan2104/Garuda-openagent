"""Private, complete boundary deltas for an owned flow verification phase.

These records are written by a supervised read worker while the original
mutating parent lease remains live. A final manifest is an immutable summary of
every receipted attempt boundary, not an inference from clipped child metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from garuda.runtime.private_records import FLAGS, open_directory, private_file
from garuda.runtime.strict_store import CorruptRecord, StorageUnavailable, preserve_private_backup
from garuda.workspace.diff import BASELINE_CAPTURED, Baseline, capture_baseline, session_delta

MAX_PATHS = 100_000
MAX_BYTES = 16 * 1024 * 1024


def _encoded(document):
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_BYTES:
        raise StorageUnavailable("flow evidence exceeds its byte bound")
    return raw


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(directory, name):
    fd = os.open(name, os.O_RDONLY | FLAGS, dir_fd=directory)
    try:
        private_file(fd)
        with os.fdopen(os.dup(fd), "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CorruptRecord("flow evidence record exceeds its byte bound")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise CorruptRecord("duplicate flow evidence field")
                result[key] = value
            return result

        def constant(_value):
            raise CorruptRecord("nonfinite flow evidence number")

        try:
            document = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        except (ValueError, UnicodeError) as exc:
            raise CorruptRecord("unreadable flow evidence record") from exc
        if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
            raise CorruptRecord("unsupported flow evidence record")
        return document, _digest(raw)
    finally:
        os.close(fd)


def _paths(paths):
    if (not isinstance(paths, list) or len(paths) > MAX_PATHS
            or any(not isinstance(path, str) or not path or "\0" in path for path in paths)
            or paths != sorted(set(paths))):
        raise CorruptRecord("incomplete or invalid flow evidence paths")
    _encoded(paths)
    return paths


def _full_changed(delta):
    paths = set(delta.changed)
    paths.update(item.renamed_from for item in delta.files
                 if not item.preexisting and item.renamed_from is not None)
    return sorted(paths)


class FlowEvidenceStore:
    """One flow's immutable baseline, attempt boundaries and final manifest."""

    def __init__(self, session_dir, parent_id, workspace, snapshot):
        self.session = Path(session_dir)
        self.path = self.session / "flow" / "evidence"
        if (str(uuid.UUID(parent_id)) != parent_id or self.session.name != parent_id
                or not os.path.isabs(workspace) or os.path.realpath(workspace) != workspace
                or snapshot.get("lease", {}).get("session_id") != parent_id
                or snapshot["lease"].get("workspace") != workspace
                or snapshot["lease"].get("mode") != "mutating"):
            raise StorageUnavailable("flow evidence owner binding is invalid")
        info = os.stat(workspace, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise StorageUnavailable("flow evidence workspace is not a directory")
        self.workspace = workspace
        self.binding = {"parent_id": parent_id, "workspace": workspace,
                        "workspace_identity": [info.st_dev, info.st_ino],
                        "owner_digest": _digest(_encoded(snapshot))}

    def begin(self):
        """Capture once; an abandoned directory can never authorize a fresh baseline."""
        parent = open_directory(self.path.parent)
        directory = None
        try:
            os.mkdir(self.path.name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
            directory = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | FLAGS, dir_fd=parent)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise StorageUnavailable("flow evidence directory is not private")
            baseline = capture_baseline(self.workspace)
            if baseline.state != BASELINE_CAPTURED:
                raise StorageUnavailable("flow verification needs an attributable Git baseline")
            document = {"version": 1, "binding": self.binding,
                        "directory": [info.st_dev, info.st_ino], "baseline": baseline.to_dict()}
            raw = _encoded(document)
            preserve_private_backup(directory, "baseline.json", raw)
            return {"baseline_digest": _digest(raw)}
        finally:
            if directory is not None:
                os.close(directory)
            os.close(parent)

    def _open(self):
        directory = open_directory(self.path)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise StorageUnavailable("flow evidence directory is not private")
            start, start_digest = _read(directory, "baseline.json")
            if (set(start) != {"version", "binding", "directory", "baseline"}
                    or start["binding"] != self.binding
                    or start["directory"] != [info.st_dev, info.st_ino]):
                raise CorruptRecord("flow baseline binding changed")
            baseline = Baseline.from_dict(start["baseline"])
            if baseline.state != BASELINE_CAPTURED:
                raise CorruptRecord("flow baseline is not attributable")
            return directory, baseline, start_digest
        except BaseException:
            os.close(directory)
            raise

    def boundary(self, ordinal: int, step: str, attempt: int):
        """Persist all paths at one settled attempt boundary, before its receipt."""
        if (type(ordinal) is not int or not 0 <= ordinal <= 512 or type(attempt) is not int
                or not 1 <= attempt <= 512 or not isinstance(step, str) or not step
                or len(step) > 128):
            raise ValueError("invalid flow evidence boundary identity")
        directory, baseline, start_digest = self._open()
        try:
            try:
                os.stat("manifest.json", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise StorageUnavailable("flow evidence was already finalized")
            delta = session_delta(baseline, self.workspace)
            if not delta.attributable:
                raise StorageUnavailable("flow evidence boundary is not attributable")
            changed, preexisting = _full_changed(delta), sorted(set(delta.preexisting))
            document = {"version": 1, "binding": self.binding, "baseline_digest": start_digest,
                        "ordinal": ordinal, "step": step, "attempt": attempt,
                        "changed": _paths(changed), "preexisting": _paths(preexisting),
                        "attribution": delta.attribution}
            raw = _encoded(document)
            preserve_private_backup(directory, f"boundary-{ordinal}.json", raw)
            return {"boundary_digest": _digest(raw), "changed_count": len(changed)}
        finally:
            os.close(directory)

    def finalize(self, coverage: list[dict]):
        """Require every expected attempt exactly once; retain restored changes."""
        if (not isinstance(coverage, list) or len(coverage) > 512
                or any(not isinstance(row, dict) or set(row) != {"step", "attempt"}
                       for row in coverage)):
            raise ValueError("invalid flow evidence coverage")
        identities = [(row["step"], row["attempt"]) for row in coverage]
        if (any(not isinstance(step, str) or not step or type(attempt) is not int or attempt < 1
                for step, attempt in identities) or len(set(identities)) != len(identities)):
            raise ValueError("duplicate or invalid flow evidence attempt")
        directory, baseline, start_digest = self._open()
        try:
            expected_names = {f"boundary-{index}.json" for index in range(len(coverage))}
            actual_names = {name for name in os.listdir(directory) if name.startswith("boundary-")}
            if actual_names != expected_names:
                raise CorruptRecord("flow evidence boundary coverage differs")
            changed, segments = set(), []
            for ordinal, identity in enumerate(coverage):
                document, segment_digest = _read(directory, f"boundary-{ordinal}.json")
                if (set(document) != {"version", "binding", "baseline_digest", "ordinal",
                                     "step", "attempt", "changed", "preexisting", "attribution"}
                        or document["binding"] != self.binding
                        or document["baseline_digest"] != start_digest
                        or type(document["ordinal"]) is not int or document["ordinal"] != ordinal
                        or document["step"] != identity["step"]
                        or document["attempt"] != identity["attempt"]
                        or document["attribution"] != BASELINE_CAPTURED):
                    raise CorruptRecord("flow evidence segment differs from coverage")
                changed.update(_paths(document["changed"]))
                _paths(document["preexisting"])
                segments.append({"step": identity["step"], "attempt": identity["attempt"],
                                 "digest": segment_digest})
            final = session_delta(baseline, self.workspace)
            if not final.attributable:
                raise StorageUnavailable("final flow delta is not attributable")
            changed.update(_full_changed(final))
            manifest = {"version": 1, "binding": self.binding, "baseline_digest": start_digest,
                        "segments": segments, "changed": _paths(sorted(changed)),
                        "final_changed": _paths(_full_changed(final)), "complete": True}
            raw = _encoded(manifest)
            preserve_private_backup(directory, "manifest.json", raw)
            return {"manifest_digest": _digest(raw), "changed_count": len(changed)}
        finally:
            os.close(directory)

    def manifest(self, expected_digest: str):
        directory, _baseline, start_digest = self._open()
        try:
            document, actual = _read(directory, "manifest.json")
            if (actual != expected_digest
                    or set(document) != {"version", "binding", "baseline_digest", "segments",
                                         "changed", "final_changed", "complete"}
                    or document["binding"] != self.binding
                    or document["baseline_digest"] != start_digest
                    or document["complete"] is not True):
                raise CorruptRecord("flow evidence manifest does not match its commitment")
            changed = _paths(document["changed"])
            final_changed = _paths(document["final_changed"])
            segments = document["segments"]
            if not isinstance(segments, list) or len(segments) > 512:
                raise CorruptRecord("flow evidence segment coverage is invalid")
            names = {name for name in os.listdir(directory) if name.startswith("boundary-")}
            if names != {f"boundary-{index}.json" for index in range(len(segments))}:
                raise CorruptRecord("flow evidence boundary coverage differs")
            cumulative = set(final_changed)
            attempts = set()
            for ordinal, row in enumerate(segments):
                if (not isinstance(row, dict) or set(row) != {"step", "attempt", "digest"}
                        or not isinstance(row["step"], str) or not row["step"]
                        or type(row["attempt"]) is not int or row["attempt"] < 1
                        or not isinstance(row["digest"], str) or len(row["digest"]) != 64):
                    raise CorruptRecord("flow evidence segment reference is invalid")
                identity = (row["step"], row["attempt"])
                if identity in attempts:
                    raise CorruptRecord("flow evidence attempt is duplicated")
                attempts.add(identity)
                segment, segment_digest = _read(directory, f"boundary-{ordinal}.json")
                if (segment_digest != row["digest"]
                        or set(segment) != {"version", "binding", "baseline_digest", "ordinal",
                                            "step", "attempt", "changed", "preexisting", "attribution"}
                        or segment["binding"] != self.binding
                        or segment["baseline_digest"] != start_digest
                        or type(segment["ordinal"]) is not int or segment["ordinal"] != ordinal
                        or segment["step"] != row["step"]
                        or type(segment["attempt"]) is not int
                        or segment["attempt"] != row["attempt"]
                        or segment["attribution"] != BASELINE_CAPTURED):
                    raise CorruptRecord("flow evidence segment differs from its commitment")
                cumulative.update(_paths(segment["changed"]))
                _paths(segment["preexisting"])
            if changed != sorted(cumulative):
                raise CorruptRecord("flow evidence cumulative paths differ from segments")
            return document
        finally:
            os.close(directory)


class OwnedFlowEvidence:
    """Async parent adapter; every Git probe and publication has owned closure."""

    def __init__(self, session_dir, parent_id, workspace, owner):
        self.owner = owner
        self.base = {"snapshot": owner.snapshot_ownership(),
                     "session_dir": str(session_dir),
                     "parent_id": parent_id, "workspace": workspace}

    async def _call(self, operation, **fields):
        from garuda.core.acceptance_work import owned_call

        return await self.owner.race(owned_call(operation, {**self.base, **fields}))

    async def begin(self):
        return await self._call("flow-evidence-begin")

    async def boundary(self, ordinal, step, attempt):
        return await self._call("flow-evidence-boundary", ordinal=ordinal,
                                step=step, attempt=attempt)

    async def finalize(self, coverage):
        reference = await self._call("flow-evidence-finalize", coverage=coverage)

        def read():
            reader = FlowEvidenceStore(self.base["session_dir"], self.base["parent_id"],
                                       self.base["workspace"], self.base["snapshot"])
            return reader.manifest(reference["manifest_digest"])

        manifest = await self.owner.race(asyncio.to_thread(read))
        return reference, manifest
