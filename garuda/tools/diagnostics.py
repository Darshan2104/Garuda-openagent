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
_MAX_DIAG_CHARS = 600


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
