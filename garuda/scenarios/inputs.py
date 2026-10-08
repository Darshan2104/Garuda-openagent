"""Bounded structured starter data; execution options are explicit typed fields."""

from __future__ import annotations

from copy import deepcopy

from garuda.context.redact import redact_text
from garuda.core.project_identity import validate_name
from garuda.scenarios.types import Starter, StarterError

MAX_FIELD_CHARS = 32_768
MAX_TASK_CHARS = 128_000
MAX_TASK_BYTES = 512_000


def _text(value, name):
    if not isinstance(value, str) or "\x00" in value:
        raise StarterError("starter.input_invalid", f"{name} must be text without NUL characters")
    if len(value) > MAX_FIELD_CHARS:
        raise StarterError("starter.input_too_large", f"narrow {name} to at most {MAX_FIELD_CHARS} characters")
    return redact_text(value)[0]


def validate_inputs(entry: Starter, inputs: dict) -> dict:
    if not isinstance(inputs, dict) or any(not isinstance(k, str) for k in inputs):
        raise StarterError("starter.input_invalid", "starter inputs must be a string-keyed mapping")
    if unknown := set(inputs) - set(entry.fields):
        raise StarterError("starter.input_invalid", f"unknown fields: {sorted(unknown)}")
    out = deepcopy(inputs)
    for name, field in entry.fields.items():
        if field.get("required") and (name not in out or not out[name]):
            raise StarterError("starter.input_required", f"supply {name}")
        if name not in out:
            continue
        kind, value = field["type"], out[name]
        if kind == "text":
            out[name] = _text(value, name)
            if field.get("required") and not out[name].strip():
                raise StarterError("starter.input_required", f"supply non-empty {name}")
        elif kind == "source-refs":
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise StarterError("starter.input_invalid", f"{name} must be a list of source references")
        elif kind == "execution-options":
            out[name] = validate_options(entry, value)
    return out


def validate_options(entry: Starter, value: dict) -> dict:
    allowed = entry.launch.get("options", [])
    if (not isinstance(value, dict) or any(not isinstance(k, str) for k in value)
            or set(value) - set(allowed)):
        raise StarterError("starter.option_unsupported", "use only the starter's supported execution options")
    out = deepcopy(value)
    if "name" in out:
        _text(out["name"], "options.name")
        validate_name(out["name"])
    if "bg" in out and type(out["bg"]) is not bool:
        raise StarterError("starter.input_invalid", "options.bg must be boolean")
    if "isolation" in out and (not isinstance(out["isolation"], str)
                              or out["isolation"] not in {"shared", "worktree", "auto"}):
        raise StarterError("starter.input_invalid", "options.isolation must be shared, worktree or auto")
    if "checks" in out:
        checks = out["checks"]
        if (not isinstance(checks, list) or len(checks) > 16
                or any(not isinstance(c, str) or not c.strip() or len(c) > 2048 or "\x00" in c for c in checks)):
            raise StarterError("starter.input_invalid", "options.checks needs at most 16 non-empty commands (2048 characters each)")
        # Do not silently rewrite an explicitly selected command by redaction.
        if any(redact_text(c)[0] != c for c in checks):
            raise StarterError("starter.input_invalid", "checks must not contain credential text")
    return out


def bound_task(task: str) -> str:
    if len(task) > MAX_TASK_CHARS or len(task.encode("utf-8")) > MAX_TASK_BYTES:
        raise StarterError("starter.input_too_large", "narrow the inputs: compiled task exceeds the delivery budget")
    return task
