"""Committed, source-free project recovery aliases for usage grouping.

Recovery owns publication. Reports read one key-bound manifest under the
identity lock; raw ledger/export rows retain their original project ids.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from garuda.core.project_identity import ProjectIdentityError, identity_dir
from garuda.runtime.strict_store import (
    StorageError,
    exclusive_lock,
    read_document,
    read_private_bytes,
)

MANIFEST = "project-aliases.json"
_ID = re.compile(r"p1_[0-9a-f]{32}")
_HEX = re.compile(r"[0-9a-f]{64}")


def key_digest(raw: bytes) -> str:
    """Internal epoch binding; neither the key nor its digest is exported."""
    text = raw.decode("ascii").strip()
    if _HEX.fullmatch(text) is None:
        raise ProjectIdentityError("the project identity key is malformed; refusing")
    return hashlib.sha256(bytes.fromhex(text)).hexdigest()


def read_manifest(directory: Path) -> dict | None:
    """Read/validate schema; recovery separately verifies aliases against its plan."""
    doc = read_document(directory / MANIFEST, versions=(1,))
    if doc is None:
        return None
    return validate_manifest(doc)


def validate_manifest(doc: dict) -> dict:
    """The publication and read paths enforce the same flat alias schema."""
    if (set(doc) != {"version", "epoch", "key_digest", "aliases"}
            or not isinstance(doc["epoch"], str)
            or re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", doc["epoch"]) is None
            or not isinstance(doc["key_digest"], str)
            or _HEX.fullmatch(doc["key_digest"]) is None
            or not isinstance(doc["aliases"], dict)):
        raise ProjectIdentityError("the project alias manifest is malformed; refusing")
    aliases = doc["aliases"]
    for old, new in aliases.items():
        if (not isinstance(old, str) or _ID.fullmatch(old) is None
                or not isinstance(new, str) or _ID.fullmatch(new) is None
                or new in aliases):
            raise ProjectIdentityError("project aliases are invalid or ambiguous; refusing")
    return doc


def verified_aliases(store_root: Path) -> dict[str, str]:
    """Only a fully published recovery epoch may merge project usage groups."""
    directory = identity_dir(store_root)
    if not directory.exists():
        return {}
    try:
        with exclusive_lock(directory) as fd:
            if read_document(directory / "recovery.json", versions=(1,)) is not None:
                raise ProjectIdentityError("project recovery is unfinished; grouping is unavailable")
            doc = read_manifest(directory)
            if doc is None:
                return {}
            if doc["key_digest"] != key_digest(read_private_bytes(fd, "key")):
                raise ProjectIdentityError("project alias epoch does not match the current key")
            return dict(doc["aliases"])
    except (StorageError, OSError, UnicodeError, ValueError) as exc:
        raise ProjectIdentityError("project alias evidence cannot be read safely; refusing") from exc
