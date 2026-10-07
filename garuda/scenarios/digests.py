"""Canonical starter digests shared by compilation and recorded-input validation."""

import hashlib
import json


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=_json_value).encode("utf-8")).hexdigest()


def _json_value(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"unsupported digest value: {type(value).__name__}")
