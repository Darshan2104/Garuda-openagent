"""Bounded, advisory post-edit diagnostics through the workspace environment."""

import hashlib
import json
import re
import shlex
import time

from garuda.types import ExecResult
from garuda.workspace.protocol import Environment

DIAGNOSTIC_TIMEOUT = 20.0
LINT_TIMEOUT = 15.0
_MAX_DIAG_CHARS = 600
_MAX_BASELINE_BYTES = 65536

# High-signal, low-noise selection; unused imports/vars and style are normal mid-edit.
_RUFF_SELECT = "E9,F821,F822,F823"
_SYNTAX_TYPES = {"py", "sh", "bash", "js", "mjs", "cjs", "json", "yaml", "yml"}


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _trim(text: str) -> str:
    text = " ".join((text or "").strip().splitlines()[-3:]).strip()
    return text[:_MAX_DIAG_CHARS]


def _trim_lint(text: str, max_lines: int = 6, max_chars: int = 800) -> str:
    lines = text.splitlines()
    shown = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        shown += f"\n… (+{len(lines) - max_lines} more)"
    return shown[:max_chars]


def _state(reason: str = "unsupported") -> dict:
    return {
        "available": None, "ran": False, "source_version": None,
        "status": "unavailable", "duration_ms": 0, "reason": reason,
        "details": "", "diagnostics": [], "complete": True,
        "baseline_source_version": None,
    }


def _violation(state: dict, message: str, line: int | None = None, column: int | None = None) -> None:
    state.update(status="violations", reason=None, details=message)
    state["diagnostics"] = [{
        "message": message, "line": line, "column": column, "code": None,
        "attribution": "unknown",
    }]


async def _execute(env: Environment, command: str, timeout: float) -> tuple[dict, ExecResult | None]:
    state = _state()
    start = time.monotonic()
    try:
        result = await env.execute(command, timeout=timeout)
        state.update(available=True, ran=True)
        if result.exit_code == 127:
            state.update(available=False, ran=False, reason="missing_checker")
        elif result.exit_code == 126:
            state.update(available=None, ran=False, status="error", reason="cannot_execute")
        elif result.exit_code == 124 or result.truncated:
            state.update(status="error", reason="timeout" if result.exit_code == 124 else "incomplete_output")
        elif result.exit_code == 0:
            state.update(status="clean", reason=None)
        else:
            state.update(status="error", reason="checker_failed")
        if state["status"] == "error":
            state["details"] = _trim(result.stderr or result.stdout)
        return state, result
    except Exception as exc:
        state.update(status="error", reason="timeout" if isinstance(exc, TimeoutError) else "checker_failed",
                     details=_trim(str(exc)))
        return state, None
    finally:
        state["duration_ms"] = int((time.monotonic() - start) * 1000)


async def _syntax(env: Environment, path: str, source: str, *, baseline: bool = False) -> dict:
    ext = _ext(path)
    quoted = shlex.quote(path)
    if ext in {"json", "yaml", "yml"}:
        state = _state()
        start = time.monotonic()
        try:
            state.update(available=True, ran=True)
            if ext == "json":
                try:
                    json.loads(source)
                except json.JSONDecodeError as exc:
                    _violation(state, f"JSON: {exc}"[:_MAX_DIAG_CHARS], exc.lineno, exc.colno)
            else:
                import yaml

                try:
                    yaml.safe_load(source)
                except yaml.YAMLError as exc:
                    mark = getattr(exc, "problem_mark", None)
                    _violation(state, f"YAML: {_trim(str(exc))}",
                               mark.line + 1 if mark else None, mark.column + 1 if mark else None)
            if state["status"] != "violations":
                state.update(status="clean", reason=None)
        except Exception as exc:
            state.update(status="error", reason="checker_failed", details=_trim(str(exc)))
        state["duration_ms"] = int((time.monotonic() - start) * 1000)
        return state
    if ext == "py":
        operand = shlex.quote(source) if baseline else quoted
        expression = "sys.argv[1]" if baseline else "open(sys.argv[1]).read()"
        command = f'python3 -c "import ast,sys; ast.parse({expression})" {operand}'
    elif ext in {"sh", "bash"}:
        command = f"bash -n {quoted}"
        if baseline:
            command = f"printf %s {shlex.quote(source)} | bash -n"
    elif ext in {"js", "mjs", "cjs"}:
        # Stdin can change Node's package/module interpretation. Do not invent a baseline.
        if baseline:
            return _state("unsupported_baseline")
        command = f"node --check {quoted}"
    else:
        return _state()
    state, result = await _execute(env, command, DIAGNOSTIC_TIMEOUT)
    if ext in {"sh", "bash"} and state["status"] == "unavailable":
        elapsed = state["duration_ms"]
        fallback = f"printf %s {shlex.quote(source)} | sh -n" if baseline else f"sh -n {quoted}"
        state, result = await _execute(env, fallback, DIAGNOSTIC_TIMEOUT)
        state["duration_ms"] += elapsed
    if state["reason"] != "checker_failed" or result is None:
        return state
    output = result.stderr or result.stdout
    if ext == "py":
        violation = result.exit_code == 1 and re.search(r"^(?:SyntaxError|IndentationError|TabError):", output, re.M)
    elif ext in {"sh", "bash"}:
        violation = result.exit_code == 2 and re.search(r"syntax error|unexpected EOF|unterminated", output, re.I)
    else:
        violation = result.exit_code == 1 and re.search(r"^SyntaxError:", output, re.M)
    if violation:
        location = re.search(r'File "<unknown>", line (\d+)', output) if ext == "py" else None
        _violation(state, _trim(output), int(location[1]) if location else None)
        state["complete"] = len(" ".join(output.strip().splitlines()[-3:])) <= _MAX_DIAG_CHARS
    return state


async def _lint(env: Environment, path: str, source: str, *, baseline: bool = False) -> dict:
    if _ext(path) != "py":
        return _state()
    command = (
        f"ruff check --isolated --select {_RUFF_SELECT} --no-cache --quiet "
        f"--output-format concise {shlex.quote(path)}"
    )
    if baseline:
        command = (
            f"printf %s {shlex.quote(source)} | "
            f"ruff check --isolated --select {_RUFF_SELECT} --no-cache --quiet "
            f"--output-format concise --stdin-filename {shlex.quote(path)} -"
        )
    state, result = await _execute(env, command, LINT_TIMEOUT)
    if state["reason"] != "checker_failed" or result is None or result.exit_code != 1:
        return state
    output = (result.stdout or "").strip()
    lines = output.splitlines()
    if not lines:
        return state
    state.update(status="violations", reason=None, details=_trim_lint(output),
                 complete=len(lines) <= 6 and len(output) <= 800)
    remaining = 800
    for line in lines[:6]:
        match = re.fullmatch(r".+?:(\d+):(\d+): ([\w-]+) (.+)", line)
        if match is None:
            state.update(status="error", reason="invalid_output", diagnostics=[], complete=False)
            break
        if len(line) > remaining:
            state["complete"] = False
            break
        state["diagnostics"].append({
            "message": match[4], "line": int(match[1]), "column": int(match[2]),
            "code": match[3], "attribution": "unknown",
        })
        remaining -= len(line) + 1
    return state


def _attribute(state: dict, previous: dict, before: str, after: str) -> None:
    state["baseline_source_version"] = hashlib.sha256(before.encode("utf-8")).hexdigest()
    state["baseline_status"] = previous["status"]
    state["baseline_duration_ms"] = previous["duration_ms"]
    if previous["status"] == "clean":
        for item in state["diagnostics"]:
            item["attribution"] = "introduced"
    elif previous["status"] == "violations" and previous["complete"]:
        old = previous["diagnostics"]
        if any((item["code"] or "").startswith("E9") or item["code"] == "invalid-syntax" for item in old):
            return  # Ruff cannot provide a semantic baseline for broken syntax.
        before_lines, after_lines = before.splitlines(), after.splitlines()
        for item in state["diagnostics"]:
            if item in old and item["line"] is not None:
                end = item["line"]
                if before_lines[:end] == after_lines[:end]:
                    item["attribution"] = "preexisting"
            elif item["code"] and not any(
                (prior["code"], prior["message"]) == (item["code"], item["message"]) for prior in old
            ):
                item["attribution"] = "introduced"


async def _collect(
    env: Environment, path: str, *, syntax: bool = True, lint: bool = True, baseline: str | None = None,
) -> dict:
    states = {"syntax": _state("disabled"), "lint": _state("disabled")}
    requested = {"syntax": syntax, "lint": lint}
    supported = {"syntax": _ext(path) in _SYNTAX_TYPES, "lint": _ext(path) == "py"}
    for name in states:
        if requested[name] and not supported[name]:
            states[name] = _state()
    active = [name for name in states if requested[name] and supported[name]]
    if not active:
        return states
    try:
        raw = await env.read_bytes(path)
        source = raw.decode("utf-8")
    except Exception as exc:
        for name in active:
            states[name].update(status="error", reason="source_unreadable", details=_trim(str(exc)))
        return states
    version = hashlib.sha256(raw).hexdigest()
    for name in active:
        if name == "lint" and syntax and states["syntax"]["status"] != "clean":
            states[name] = _state("syntax_not_clean")
        else:
            checker = _syntax if name == "syntax" else _lint
            states[name] = await checker(env, path, source)
            if (states[name]["status"] == "violations" and baseline is not None
                    and len(baseline.encode("utf-8")) <= _MAX_BASELINE_BYTES):
                previous = await checker(env, path, baseline, baseline=True)
                _attribute(states[name], previous, baseline, source)
        states[name]["source_version"] = version
    try:
        fresh = await env.read_bytes(path) == raw
    except Exception:
        fresh = False
    if not fresh:
        for name in active:
            states[name].update(status="error", reason="source_changed", details="", diagnostics=[])
    return states


async def check_syntax(env: Environment, path: str) -> str | None:
    """Compatibility text helper; structured callers use ``post_edit_report``."""
    state = (await _collect(env, path, lint=False))["syntax"]
    return state["details"] if state["status"] == "violations" else None


async def check_lint(env: Environment, path: str) -> str | None:
    """Compatibility text helper for the isolated, single-file Ruff selection."""
    state = (await _collect(env, path, syntax=False))["lint"]
    return state["details"] if state["status"] == "violations" else None


async def post_edit_report(
    env: Environment, path: str, ctx, *, metadata: dict | None = None, baseline: str | None = None,
) -> str:
    """Keep clean edits quiet; expose checker availability and freshness in metadata.

    Baselines are checked only on violations, using known pre-edit content through
    the same fixed checkers. No result here is acceptance verification.
    """
    enabled = getattr(ctx, "post_edit_diagnostics", True)
    states = await _collect(env, path, syntax=enabled,
                            lint=enabled and getattr(ctx, "post_edit_lint", True), baseline=baseline)
    if metadata is not None:
        metadata["diagnostics"] = {"advisory": True, "checkers": states}
    if states["syntax"]["status"] == "violations":
        return f'\n\n⚠ Syntax check failed:\n{states["syntax"]["details"]}'
    if states["lint"]["status"] == "violations":
        return f'\n\n⚠ Lint issues (fix if introduced by this change):\n{states["lint"]["details"]}'
    return ""
