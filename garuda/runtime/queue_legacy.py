"""Read old queues for diagnosis; archive only safely admitted records.

Version 1 jobs lack bindings; version 2 claims lack activation history. Both
refuse migration. Empty records and fully bound, ordered version 2 waiters
are archived byte-for-byte through the locked directory before publication.
"""

from __future__ import annotations

import json

from garuda.runtime.strict_store import StorageError, preserve_private_backup, read_private_bytes


class LegacyQueueError(ValueError):
    """Legacy migration cannot be proved safe; preserve records for diagnosis."""


def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def project(document: dict) -> dict:
    """A validated diagnostic projection, without inventing admission authority."""
    scopes = document.get("scopes")
    if (type(document.get("version")) is not int or document["version"] != 1
            or not _integer(document.get("seq")) or not isinstance(scopes, dict)
            or set(document) != {"version", "seq", "scopes"}):
        raise LegacyQueueError("legacy queue record is partial or malformed; refusing")
    projected = {}
    for scope, entry in scopes.items():
        if (not isinstance(scope, str) or not isinstance(entry, dict)
                or set(entry) != {"capacity", "waiting", "claims"}
                or not _integer(entry.get("capacity")) or entry["capacity"] < 1
                or not isinstance(entry.get("waiting"), list)
                or not isinstance(entry.get("claims"), dict)):
            raise LegacyQueueError("legacy queue scope is partial or malformed; refusing")
        waiting, claims = entry["waiting"], entry["claims"]
        if (not all(isinstance(w, dict) and isinstance(w.get("id"), str)
                    and _integer(w.get("seq")) for w in waiting)
                or not all(isinstance(k, str) and isinstance(v, dict) for k, v in claims.items())):
            raise LegacyQueueError("legacy queue entries are partial or malformed; refusing")
        harness = scope.rsplit(":", 1)[-1]
        projected[scope] = {
            "waiting": [{"id": w["id"], "seq": w["seq"], "user": None,
                         "harness": harness, "session_id": None, "config_digest": None,
                         "enqueued_at": None} for w in waiting],
            "claims": {k: {**v, "user": None, "harness": harness, "session_id": None,
                           "config_digest": None, "claimed_at": v.get("heartbeat")}
                       for k, v in claims.items()},
        }
    return {"version": 2, "seq": document["seq"], "scopes": projected}


def preserve_previous(directory_fd: int, expected: dict) -> None:
    """Archive safe old records durably, without inventing activation history."""
    try:
        raw = read_private_bytes(directory_fd, "state.json")
        source = json.loads(raw)
        if not isinstance(source, dict):
            raise LegacyQueueError("queue migration source is not a document; refusing")
        version = source.get("version")
        if type(version) is not int or version not in (1, 2):
            raise LegacyQueueError("unsupported queue migration source; refusing")
        if version == 1:
            projected = project(source)
            unsafe = any(s["waiting"] or s["claims"] for s in projected["scopes"].values())
        else:
            if set(source) != {"version", "seq", "scopes"}:
                raise LegacyQueueError("version 2 queue has unknown fields; refusing")
            projected = source
            unsafe = any(s["claims"] or any(
                type(w.get("seq")) is not int or not 1 <= w["seq"] <= source["seq"]
                or not all(isinstance(w.get(k), str) and w[k]
                        for k in ("user", "harness", "session_id", "config_digest"))
                for w in s["waiting"]) for s in projected["scopes"].values())
            unsafe = unsafe or any(
                any(a["seq"] >= b["seq"] for a, b in zip(s["waiting"], s["waiting"][1:], strict=False))
                for s in projected["scopes"].values())
        if unsafe:
            raise LegacyQueueError(
                "legacy queue has unproved activation or missing user/session/configuration "
                "bindings; refusing migration even for dead owners. Inspect the original "
                "state.json and resolve work with the prior version; its bytes remain available"
            )
        if projected != expected:
            raise LegacyQueueError("legacy source changed before backup; refusing")
        preserve_private_backup(directory_fd, f"state.json.v{version}", raw)
    except (OSError, ValueError, StorageError) as exc:
        raise LegacyQueueError(f"cannot preserve legacy source/backup: {exc}; refusing") from exc
