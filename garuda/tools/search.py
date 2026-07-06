import shlex

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

DEFAULT_GREP_MAX_RESULTS = 100
GLOB_MAX_RESULTS = 200


def _ctx_int(value) -> int:
    """Coerce a context-line argument to a non-negative int (0 when absent/invalid)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


class GrepTool:
    name = "grep"
    description = (
        "Search file contents for a regular expression (extended regex, like `grep -E`). "
        "Returns matching lines as path:line:content. Use glob to filter filenames "
        "(e.g. '*.py') and path to scope the search to a directory or file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Extended regular expression to search for"},
            "path": {
                "type": "string",
                "description": "Directory or file to search (default '.')",
            },
            "glob": {
                "type": "string",
                "description": "Filename filter, e.g. '*.py' (optional)",
            },
            "context": {
                "type": "integer",
                "description": "Lines of context to show before AND after each match (grep -C)",
            },
            "before_context": {
                "type": "integer",
                "description": "Lines of context before each match (grep -B)",
            },
            "after_context": {
                "type": "integer",
                "description": "Lines of context after each match (grep -A)",
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "content (default): matching lines; files_with_matches: just file "
                "paths; count: match count per file",
            },
            "max_results": {
                "type": "integer",
                "description": f"Maximum output lines to return (default {DEFAULT_GREP_MAX_RESULTS})",
            },
        },
        "required": ["pattern"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        pattern = arguments["pattern"]
        path = arguments.get("path") or "."
        glob = arguments.get("glob")
        max_results = arguments.get("max_results") or DEFAULT_GREP_MAX_RESULTS
        if max_results < 1:
            max_results = 1
        output_mode = arguments.get("output_mode") or "content"

        q_path = shlex.quote(path)
        q_pattern = shlex.quote(pattern)
        include = f" --include={shlex.quote(glob)}" if glob else ""

        # Output-mode flags: -l lists files, -c counts per file; both suppress the
        # per-line context flags. Otherwise apply -A/-B/-C context (ripgrep-style).
        if output_mode == "files_with_matches":
            mode_flags = " -l"
            line_flags = "-H"  # -l ignores -n; keep -H harmless
        elif output_mode == "count":
            mode_flags = " -c"
            line_flags = "-H"
        else:
            mode_flags = ""
            line_flags = "-nH"
            ctx_c = _ctx_int(arguments.get("context"))
            ctx_b = _ctx_int(arguments.get("before_context"))
            ctx_a = _ctx_int(arguments.get("after_context"))
            if ctx_c:
                mode_flags += f" -C {ctx_c}"
            else:
                if ctx_b:
                    mode_flags += f" -B {ctx_b}"
                if ctx_a:
                    mode_flags += f" -A {ctx_a}"

        # Branch on file vs directory so single-file and symlinked targets work
        # reliably across GNU and BSD/macOS grep. Directories (incl. symlinks to
        # dirs) use `-R` with a trailing slash — BSD grep won't descend into a
        # symlink-to-dir given directly, but the trailing slash forces it. Single
        # files use plain `grep -nH` (no recursion; `-r <file>` is unreliable on
        # BSD). `-H` keeps the path:line:content shape even for one file.
        command = (
            f"p={q_path}; "
            f'if [ -d "$p" ]; then '
            f'grep -R {line_flags} -E{mode_flags}{include} --exclude-dir=.git -e {q_pattern} -- "$p/"; '
            f"else "
            f'grep {line_flags} -E{mode_flags} -e {q_pattern} -- "$p"; '
            f"fi"
        )

        result = await env.execute(command)
        if result.exit_code == 1 and not result.stdout.strip():
            return ToolResult(
                tool_call_id="",
                content=f"No matches found for {pattern}",
            )
        if result.exit_code not in (0, 1):
            stderr = result.stderr.strip()
            hint = ""
            if "No such file" in stderr:
                hint = f" (path {path!r} does not exist or is not readable)"
            return ToolResult(
                tool_call_id="",
                content=f"grep failed (exit {result.exit_code}): {stderr}{hint}",
                is_error=True,
            )

        lines = result.stdout.splitlines()
        capped = len(lines) > max_results
        if capped:
            lines = lines[:max_results]
        output = "\n".join(lines)
        if capped:
            output += f"\n(results capped at {max_results})"
        return ToolResult(tool_call_id="", content=output)


class GlobTool:
    name = "glob"
    description = (
        "Find files by name pattern. Supports simple globs like '*.py' "
        "(matched anywhere under the base path) and path globs like 'src/**/*.ts'. "
        "Returns matching file paths, one per line."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.py', '*.md', or 'src/*.ts'",
            },
            "path": {
                "type": "string",
                "description": "Base directory to search from (default '.')",
            },
        },
        "required": ["pattern"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        pattern = arguments["pattern"]
        base = arguments.get("path") or "."

        if "/" not in pattern:
            # Bare filename glob: match anywhere under the base path.
            matcher = f"-name {shlex.quote(pattern)}"
        else:
            # Path glob. In `find -path`, `*` matches `/` too, so `**` collapses
            # naturally: `src/**/*.py` -> `./src/**.py` matches any depth,
            # including direct children.
            converted = pattern.replace("**/", "*")
            if not converted.startswith(("./", "/", "*")):
                converted = "./" + converted
            matcher = f"-path {shlex.quote(converted)}"

        # `-L` follows symlinks so a symlinked directory under the base is
        # traversed (matches shell globbing / bash reach).
        command = (
            f"cd {shlex.quote(base)} && "
            f"find -L . -type f {matcher} -not -path '*/.git/*' | sort"
        )
        result = await env.execute(command)
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"glob failed (exit {result.exit_code}): {result.stderr.strip()}",
                is_error=True,
            )

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return ToolResult(tool_call_id="", content=f"No files matched pattern {pattern}")
        capped = len(lines) > GLOB_MAX_RESULTS
        if capped:
            lines = lines[:GLOB_MAX_RESULTS]
        output = "\n".join(lines)
        if capped:
            output += f"\n(results capped at {GLOB_MAX_RESULTS})"
        return ToolResult(tool_call_id="", content=output)


class LsTool:
    name = "ls"
    description = "List directory contents (like `ls -la`)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory or file to list (default '.')",
            },
        },
        "required": [],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        path = arguments.get("path") or "."
        result = await env.execute(f"ls -la {shlex.quote(path)}")
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"ls failed (exit {result.exit_code}): {result.stderr.strip()}",
                is_error=True,
            )
        return ToolResult(tool_call_id="", content=result.stdout)
