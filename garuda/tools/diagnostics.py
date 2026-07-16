"""Best-effort post-edit syntax diagnostics, run through the environment.

After an edit/write, a fast syntax check on known file types gives the model
immediate feedback ("you just introduced a SyntaxError") instead of it only
finding out on the next test run. Checks are cheap, run via ``env`` (so they work
in docker/remote), and never raise — a failed/unavailable checker yields no note.
"""

import json
import shlex

from garuda.workspace.protocol import Environment

DIAGNOSTIC_TIMEOUT = 20.0
LINT_TIMEOUT = 15.0
_MAX_DIAG_CHARS = 600

# High-signal, low-noise ruff selection: undefined names, undefined __all__ export,
# used-before-assignment, and syntax-level (E9) errors. Deliberately EXCLUDES unused
# imports/vars (F401/F841) and style — those are normal in a file mid-edit and would
# just nag the model.
_RUFF_SELECT = "E9,F821,F822,F823"


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _trim(text: str) -> str:
    text = " ".join((text or "").strip().splitlines()[-3:]).strip()
    return text[:_MAX_DIAG_CHARS]


async def check_syntax(env: Environment, path: str) -> str | None:
    """Return a short syntax-error description for ``path``, or None if it parses
    cleanly / is an unchecked type / the checker is unavailable.

    Every checker is single-file, fast, side-effect-free, and best-effort: an
    unavailable tool (exit 127) is treated as "no opinion" so this never turns a
    missing interpreter into a false positive.
    """
    ext = _ext(path)
    quoted = shlex.quote(path)
    try:
        if ext == "py":
            # ast.parse is a pure syntax check with no side effects (py_compile
            # would litter __pycache__ into the workspace).
            result = await env.execute(
                f'python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" {quoted}',
                timeout=DIAGNOSTIC_TIMEOUT,
            )
            if result.exit_code == 127:
                return None  # python3 unavailable in this environment
            if result.exit_code != 0:
                return _trim(result.stderr or result.stdout)
        elif ext in ("sh", "bash"):
            # `bash -n` (POSIX `sh -n` fallback) parses without executing — a real
            # syntax gate that catches unbalanced quotes/if/fi, etc.
            result = await env.execute(f"bash -n {quoted}", timeout=DIAGNOSTIC_TIMEOUT)
            if result.exit_code == 127:
                result = await env.execute(f"sh -n {quoted}", timeout=DIAGNOSTIC_TIMEOUT)
            if result.exit_code == 127:
                return None  # no shell available to check
            if result.exit_code != 0:
                return _trim(result.stderr or result.stdout)
        elif ext in ("js", "mjs", "cjs"):
            # `node --check` is a syntax-only parse (no execution, no side effects)
            # and needs no project/tsconfig — unlike a full tsc type-check.
            result = await env.execute(f"node --check {quoted}", timeout=DIAGNOSTIC_TIMEOUT)
            if result.exit_code == 127:
                return None  # node unavailable
            if result.exit_code != 0:
                return _trim(result.stderr or result.stdout)
        elif ext == "json":
            try:
                json.loads(await env.read_file(path))
            except (ValueError, OSError) as exc:
                return f"JSON: {exc}"
        elif ext in ("yaml", "yml"):
            import yaml

            try:
                yaml.safe_load(await env.read_file(path))
            except (yaml.YAMLError, OSError) as exc:
                return f"YAML: {_trim(str(exc))}"
    except Exception:
        return None
    return None


def _trim_lint(text: str, max_lines: int = 6, max_chars: int = 800) -> str:
    lines = text.splitlines()
    shown = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        shown += f"\n… (+{len(lines) - max_lines} more)"
    return shown[:max_chars]


async def check_lint(env: Environment, path: str) -> str | None:
    """Fast, single-file *semantic* lint after an edit (best-effort).

    Currently Python-only, via ruff: catches undefined names / use-before-assign —
    real bugs the syntax check can't see — before the model wastes a test run
    discovering them. ``--isolated`` ignores the repo's own ruff config so the
    selection is deterministic regardless of the project. Returns None when ruff is
    unavailable, the file is clean, or the type isn't linted — never a false positive.
    """
    if _ext(path) != "py":
        return None
    quoted = shlex.quote(path)
    try:
        result = await env.execute(
            f"ruff check --isolated --select {_RUFF_SELECT} --no-cache --quiet "
            f"--output-format concise {quoted}",
            timeout=LINT_TIMEOUT,
        )
    except Exception:
        return None
    # Only surface real violations (exit 1). 0 = clean, 127 = ruff absent, 2 = ruff
    # internal/arg error — all "no opinion", so stay silent.
    if result.exit_code != 1:
        return None
    out = (result.stdout or "").strip()
    return _trim_lint(out) if out else None


async def post_edit_report(env: Environment, path: str, ctx) -> str:
    """Diagnostic appendix for a file just written by edit/write_file/multi_edit.

    Returns text to append to the tool message (empty when clean or disabled).
    Runs the syntax gate first and only lints when syntax is valid and linting is
    enabled — linting a file with broken syntax is just noise. Honors
    ``ctx.post_edit_diagnostics`` (syntax) and ``ctx.post_edit_lint`` (semantic).
    """
    if not getattr(ctx, "post_edit_diagnostics", True):
        return ""
    problem = await check_syntax(env, path)
    if problem:
        return f"\n\n⚠ Syntax check failed:\n{problem}"
    if getattr(ctx, "post_edit_lint", True):
        lint = await check_lint(env, path)
        if lint:
            return f"\n\n⚠ Lint issues (fix if introduced by this change):\n{lint}"
    return ""
