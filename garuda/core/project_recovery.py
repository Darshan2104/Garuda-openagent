"""Recover project ids after the project key is lost (plan task B.1, #157).

``garuda doctor --recover-project-ids``. When the key behind project ids is
gone, every new session refuses (``session.project_key_missing``) rather than
silently splitting projects. Recovery rebuilds the ids under a new key:

1. **Refuse while anything may be running.** Any workspace lease whose owner is
   alive or of unknown liveness stops recovery.
2. **Plan.** A new key is generated and staged as ``key.next``. Each session's
   recorded project path is re-checked against its recorded filesystem identity
   (device, inode and root commit); only a match maps it to a new id. A path alone is not
   proof — a missing or replaced repository stays *unmapped* under its old id.
   Name reservations move with their project; two old ids that would merge
   into one project with the same name are a conflict, and conflicts refuse
   before anything changes.
3. **Journal, apply, publish.** The plan is written to ``recovery.json`` before
   any session is touched. Session metadata gains the new id and keeps the old
   one in ``previous_project_ids``; name directories move; finally ``key.next``
   replaces ``key`` and the journal becomes a receipt. An interrupted run is
   resumed from the journal on the next invocation, never mixed with a fresh
   key. Repeating a finished recovery is a no-op.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from garuda.core.project_aliases import MANIFEST, key_digest, read_manifest, validate_manifest
from garuda.core.project_identity import ProjectIdentityError, compute_project_id, identity_dir
from garuda.runtime.strict_store import (
    StorageError,
    exclusive_lock,
    read_document,
    read_private_bytes,
    write_document,
)


class RecoveryRefused(Exception):
    """Recovery cannot proceed safely; nothing was changed."""


@dataclass
class RecoveryReport:
    mapped: dict[str, str] = field(default_factory=dict)  # old id -> new id
    unmapped: list[str] = field(default_factory=list)  # old ids left as they were
    sessions_updated: int = 0
    resumed: bool = False
    noop: bool = False


def _sessions(store_root: Path):
    for directory in sorted(Path(store_root).iterdir()):
        if directory.name.startswith(".") or not (directory / "meta.json").is_file():
            continue
        try:
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("project_id"):
            yield directory.name, meta


def _fs_id(path: str) -> str | None:
    from garuda.core.project_identity import filesystem_identity

    return filesystem_identity(path) or None


def _plan(store_root: Path, new_key: bytes) -> dict:
    mapping: dict[str, str] = {}
    unmapped: set[str] = set()
    sessions: dict[str, dict] = {}
    for session_id, meta in _sessions(store_root):
        old = meta["project_id"]
        path = meta.get("project_path")
        recorded_fs = meta.get("project_fs_id")
        if isinstance(path, str) and recorded_fs and _fs_id(path) == recorded_fs:
            new = compute_project_id(new_key, path)
            if mapping.get(old, new) != new:
                raise RecoveryRefused(
                    f"project id {old} maps to two different projects; refusing"
                )
            mapping[old] = new
            sessions[session_id] = {"old": old, "new": new}
        else:
            unmapped.add(old)
    for old in list(unmapped):
        if old in mapping:
            unmapped.discard(old)  # some sessions of it were verified
    names_root = Path(store_root) / ".names"
    claimed: dict[tuple[str, str], str] = {}
    conflicts = []
    for old, new in mapping.items():
        directory = names_root / old
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            key = (new, entry.name)
            if key in claimed and claimed[key] != old:
                conflicts.append(f"name {entry.name!r} in {claimed[key]} and {old}")
            claimed[key] = old
            if (names_root / new / entry.name).exists() and new != old:
                conflicts.append(f"name {entry.name!r} already reserved under {new}")
    if conflicts:
        raise RecoveryRefused("name conflicts would be created: " + "; ".join(conflicts))
    try:
        prior = read_manifest(identity_dir(store_root))
    except (ProjectIdentityError, StorageError) as exc:
        raise RecoveryRefused("prior project alias evidence cannot be verified; refusing") from exc
    aliases = dict(mapping)
    # Earlier epochs are carried forward only through currently verified projects.
    for old, previous in (prior or {}).get("aliases", {}).items():
        if previous in mapping:
            current = mapping[previous]
            if old in aliases and aliases[old] != current:
                raise RecoveryRefused("project aliases disagree with verified recovery")
            aliases[old] = current
    plan = {
        "version": 1,
        "state": "planned",
        "aliases": aliases,
        "epoch": secrets.token_hex(16),
        "key_digest": hashlib.sha256(new_key).hexdigest(),
        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "mapping": mapping,
        "unmapped": sorted(unmapped),
        "sessions": sessions,
    }
    try:
        validate_manifest({key: plan[key] for key in ("version", "epoch", "key_digest", "aliases")})
    except ProjectIdentityError as exc:
        raise RecoveryRefused("planned project aliases cannot be verified; refusing") from exc
    return plan


def _apply(store_root: Path, plan: dict) -> int:
    from garuda.core.sessions import merge_meta

    updated = 0
    for session_id, change in plan["sessions"].items():
        meta_path = Path(store_root) / session_id / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("project_id") == change["new"]:
            continue  # applied by an earlier, interrupted run
        previous = list(meta.get("previous_project_ids") or [])
        if change["old"] not in previous:
            previous.append(change["old"])
        merge_meta(meta_path, {"project_id": change["new"], "previous_project_ids": previous})
        updated += 1
    names_root = Path(store_root) / ".names"
    for old, new in plan["mapping"].items():
        source = names_root / old
        if not source.is_dir() or old == new:
            continue
        target = names_root / new
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        for entry in source.iterdir():
            os.replace(entry, target / entry.name)
        source.rmdir()
    return updated


def recover_project_ids(store_root: str | Path, *, leases=None) -> RecoveryReport:
    """Rebuild project ids under a new key. See the module docstring."""
    from garuda.workspace.lease import LeaseStore

    store_root = Path(store_root)
    ident = identity_dir(store_root)
    key_path, staged, journal = ident / "key", ident / "key.next", ident / "recovery.json"
    with exclusive_lock(ident) as identity_fd:
        plan = read_document(journal, versions=(1,))
        if plan is None and key_path.exists():
            return RecoveryReport(noop=True)
        live = (leases or LeaseStore()).possibly_live_holders()
        if live:
            names = ", ".join(sorted({h.session_id for h in live}))
            raise RecoveryRefused(
                f"sessions may still be running ({names}); stop them before recovering"
            )
        resumed = plan is not None
        if plan is None:
            new_key = secrets.token_hex(32)
            plan = _plan(store_root, bytes.fromhex(new_key))
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(staged, flags, 0o600)
            try:
                os.write(fd, new_key.encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            write_document(journal, plan)
        elif not staged.exists() and plan.get("state") != "published":
            if not key_path.exists():
                raise RecoveryRefused(
                    "the recovery journal exists but neither its staged key nor a "
                    "published key does; refusing to guess. Inspect " + str(journal)
                )
            plan["state"] = "published"  # the key was moved before the journal said so
        try:
            epoch = validate_manifest({name: plan[name] for name in (
                "version", "epoch", "key_digest", "aliases")})
            raw_key = read_private_bytes(identity_fd, "key.next" if staged.exists() else "key")
            if key_digest(raw_key) != epoch["key_digest"]:
                raise ProjectIdentityError("recovery key disagrees with its planned epoch")
        except (KeyError, ProjectIdentityError, StorageError, ValueError, UnicodeError) as exc:
            raise RecoveryRefused("recovery epoch cannot be verified; preserve its journal") from exc
        updated = _apply(store_root, plan)
        if plan.get("state") != "published":
            os.replace(staged, key_path)
            plan["state"] = "published"
            plan["finished_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            write_document(journal, plan)
        # Publish one source-free alias epoch only after the key is committed.
        # A retained journal keeps readers from consuming partial recovery.
        fd = os.open(key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            published_digest = key_digest(handle.read())
        if published_digest != plan["key_digest"]:
            raise RecoveryRefused("recovery key disagrees with its planned epoch; refusing")
        manifest = {"version": 1, "epoch": plan["epoch"], "key_digest": published_digest,
            "aliases": plan["aliases"]}
        try:
            validate_manifest(manifest)
        except ProjectIdentityError as exc:
            raise RecoveryRefused("planned project aliases cannot be verified; refusing") from exc
        write_document(ident / MANIFEST, manifest)
        receipt = ident / f"recovery-{plan['started_at'].replace(':', '')}.json"
        write_document(receipt, {**plan, "sessions": len(plan["sessions"])})
        os.unlink(journal)
        directory_fd = os.open(ident, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return RecoveryReport(
        mapped=dict(plan["mapping"]),
        unmapped=list(plan["unmapped"]),
        sessions_updated=updated,
        resumed=resumed,
    )
