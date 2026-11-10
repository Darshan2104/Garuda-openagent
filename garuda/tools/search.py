import base64
import hashlib
import json
import re
import shlex
import warnings

from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


def _glob_regex(pattern: str) -> "re.Pattern[str]":
    """Compile a glob into a regex with `*` confined to one path segment.

    `*` -> `[^/]*`, `?` -> `[^/]`, `**/` -> any number of segments, `**` -> anything,
    `[abc]`/`[!abc]` -> a character class. This is the shell's meaning of the
    pattern; `find -path` treats every `*` as crossing `/`, which is why `src/*.ts`
    used to return `src/nested/b.ts`.

    Character classes are passed through rather than escaped: this filter refines
    what `find` returned, so escaping `[` would make it *stricter* than the
    prefilter and `src/[ab].ts` would silently match nothing.
    """
    return re.compile(_glob_translate(pattern, ere=False))


# Characters a backslash makes literal in a POSIX extended regular expression.
_ERE_SPECIAL = set("\\^.[$()|*+?{")


def _glob_translate(pattern: str, *, ere: bool) -> str:
    """The anchored regex for ``pattern``: Python syntax, or POSIX ERE for `grep -E`."""
    group = "(" if ere else "(?:"

    def escape(char: str) -> str:
        if not ere:
            return re.escape(char)
        return "\\" + char if char in _ERE_SPECIAL else char

    pat = pattern.removeprefix("./")
    parts = [f"{group}\\./)?"]
    index = 0
    while index < len(pat):
        if pat.startswith("**/", index):
            parts.append(f"{group}[^/]+/)*")
            index += 3
        elif pat.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pat[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pat[index] == "?":
            # grep runs bytewise (LC_ALL=C) and only has to keep a superset for
            # the exact Python filter: one character is one to four UTF-8 bytes.
            parts.append("[^/]{1,4}" if ere else "[^/]")
            index += 1
        elif pat[index] == "[":
            close = pat.find("]", index + 1)
            if close == -1:
                # Unterminated: a literal bracket, as the shell treats it.
                parts.append(escape("["))
                index += 1
                continue
            if ere:
                # Classes differ between ERE and Python; the exact filter decides.
                parts.append("[^/]{1,4}")
                index = close + 1
                continue
            body = pat[index + 1 : close]
            # glob negates with `!`, regex with `^`.
            if body.startswith("!"):
                body = "^" + body[1:]
            parts.append(f"[{body}]")
            index = close + 1
        else:
            parts.append(escape(pat[index]))
            index += 1
    return "^" + "".join(parts) + "$"


DEFAULT_GREP_MAX_RESULTS = 100
GLOB_MAX_RESULTS = 200
# Production and capture bounds: the producer is stopped once a page (plus one
# look-ahead line) or this many bytes have been captured, whichever is first.
SEARCH_CAPTURE_BYTES = 512 * 1024
MAX_SEARCH_OFFSET = 10_000
LONG_LINE_CHARS = 2000
LINE_WINDOW_CHARS = 200
_EXIT_MARKER = "__GARUDA_SEARCH_EXIT__:"


def _bounded(command: str, max_lines: int | None, max_bytes: int) -> str:
    """Stop ``command`` early while keeping its exit status when it finishes.

    The producer's status is printed as a final marker line; if ``head`` cut the
    stream first, the marker is absent and the result is known to be partial.
    Closing the pipe stops rg/grep/find by SIGPIPE instead of letting them run on.
    """
    line_cap = f" | head -n {max_lines + 1}" if max_lines is not None else ""
    return (f"{{ {command}; printf '%s%s\\n' {_EXIT_MARKER} \"$?\"; }}"
            f"{line_cap} | head -c {max_bytes}")


def _split_capture(stdout: str) -> tuple[list[str], int | None, bool]:
    """(lines, producer exit status or None if stopped early, last line cut by the byte cap)."""
    partial = bool(stdout) and not stdout.endswith("\n")
    # Split on "\n" only, as head -n counts; matched content may hold \r or U+2028.
    lines = stdout.split("\n")
    if not partial:
        lines.pop()
    if lines and lines[-1].startswith(_EXIT_MARKER) and not partial:
        try:
            return lines[:-1], int(lines[-1][len(_EXIT_MARKER):]), False
        except ValueError:
            pass
    return lines, None, partial


def _window(line: str, regex: "re.Pattern[str] | None") -> str:
    """A long line reduced to a match-centred window with explicit omission markers."""
    if len(line) <= LONG_LINE_CHARS:
        return line
    match = regex.search(line) if regex is not None else None
    start, end = (match.start(), match.end()) if match else (0, 0)
    begin = max(0, start - LINE_WINDOW_CHARS)
    finish = min(len(line), max(end, start) + LINE_WINDOW_CHARS)
    if finish - begin > 2 * LINE_WINDOW_CHARS + LONG_LINE_CHARS // 2:
        finish = begin + 2 * LINE_WINDOW_CHARS + LONG_LINE_CHARS // 2
    head = f"[…{begin} chars omitted]" if begin else ""
    tail = f"[…{len(line) - finish} chars omitted]" if finish < len(line) else ""
    return f"{head}{line[begin:finish]}{tail}"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _encode_cursor(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _decode_cursor(token) -> dict:
    if not isinstance(token, str) or len(token) > 1024:
        raise ValueError("invalid cursor")
    try:
        value = json.loads(base64.urlsafe_b64decode(token.encode()))
    except ValueError as exc:
        raise ValueError("invalid cursor") from exc
    if (not isinstance(value, dict) or value.get("v") != 1
            or type(value.get("o")) is not int or not 0 < value["o"] <= MAX_SEARCH_OFFSET
            or not isinstance(value.get("q"), str) or not isinstance(value.get("d"), str)):
        raise ValueError("invalid cursor")
    return value


def _ctx_int(value) -> int:
    """Coerce a context-line argument to a non-negative int (0 when absent/invalid)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _rg_variant(
    q_pattern: str, q_path: str, glob: str | None, output_mode: str,
    context: int, before: int, after: int, no_ignore: bool,
    per_file: int | None = None,
) -> str:
    """ripgrep command producing the same ``path:line:content`` shape as grep.

    Respects .gitignore/.ignore and skips hidden files by default — the point of
    preferring rg (no node_modules/build/.git noise, and far faster on big trees).
    ``no_ignore`` restores a grep-like full sweep (but still skips the .git dir).
    ``--sort path`` makes the order stable so pages and cursors are reproducible.
    """
    flags = ["rg", "--color=never", "--no-heading", "-H", "--sort", "path"]
    if no_ignore:
        flags += ["--no-ignore", "--hidden", "-g", shlex.quote("!.git")]
    if glob:
        flags += ["-g", shlex.quote(glob)]
    if output_mode == "files_with_matches":
        flags.append("-l")
    elif output_mode == "count":
        flags.append("-c")
    else:
        flags.append("-n")
        if per_file:
            flags += ["--max-count", str(per_file)]
        if context:
            flags += ["-C", str(context)]
        else:
            if before:
                flags += ["-B", str(before)]
            if after:
                flags += ["-A", str(after)]
    flags += ["-e", q_pattern, "--", q_path]
    return " ".join(flags)


def _grep_variant(
    q_pattern: str, q_path: str, glob: str | None, output_mode: str,
    context: int, before: int, after: int, per_file: int | None = None,
) -> str:
    """GNU/BSD grep command (the fallback when ripgrep is not installed)."""
    include = f" --include={shlex.quote(glob)}" if glob else ""
    if output_mode == "files_with_matches":
        mode_flags = " -l"
        line_flags = "-H"
    elif output_mode == "count":
        mode_flags = " -c"
        line_flags = "-H"
    else:
        mode_flags = f" -m {per_file}" if per_file else ""
        line_flags = "-nH"
        if context:
            mode_flags += f" -C {context}"
        else:
            if before:
                mode_flags += f" -B {before}"
            if after:
                mode_flags += f" -A {after}"
    # Branch on file vs directory so single-file and symlinked targets work
    # reliably across GNU and BSD/macOS grep. Directories (incl. symlinks to
    # dirs) use `-R` with a trailing slash — BSD grep won't descend into a
    # symlink-to-dir given directly, but the trailing slash forces it. Single
    # files use plain `grep -nH` (no recursion; `-r <file>` is unreliable on BSD).
    return (
        f"p={q_path}; "
        f'if [ -d "$p" ]; then '
        f'grep -R {line_flags} -E{mode_flags}{include} --exclude-dir=.git -e {q_pattern} -- "$p/"; '
        f"else "
        f'grep {line_flags} -E{mode_flags} -e {q_pattern} -- "$p"; '
        f"fi"
    )


def _build_search_command(
    pattern: str, path: str, glob: str | None, output_mode: str,
    context: int, before: int, after: int, no_ignore: bool,
    per_file: int | None = None,
) -> str:
    """Prefer ripgrep when present, fall back to grep — chosen in one shell command
    so it works uniformly across local/docker/remote without an extra round-trip."""
    q_path = shlex.quote(path)
    q_pattern = shlex.quote(pattern)
    rg = _rg_variant(q_pattern, q_path, glob, output_mode, context, before, after, no_ignore,
                     per_file)
    grep = _grep_variant(q_pattern, q_path, glob, output_mode, context, before, after, per_file)
    return f"if command -v rg >/dev/null 2>&1; then {rg}; else {grep}; fi"


class GrepTool:
    effect = ToolEffect.READ_ONLY
    name = "grep"
    description = (
        "Search file contents for a regular expression (uses ripgrep when available, "
        "else `grep -E`). Returns matching lines as path:line:content. Use glob to filter "
        "filenames (e.g. '*.py') and path to scope the search to a directory or file. "
        "By default excludes files ignored by .gitignore/.ignore and hidden files; set "
        "no_ignore to search everything. Output is sorted by path; when capped, pass "
        "the reported next_cursor to read the next page. Long lines are shown as a "
        "window around the match."
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
            "no_ignore": {
                "type": "boolean",
                "description": "Search all files, including .gitignore/.ignore'd and hidden ones "
                "(default false: ignore-aware, like a developer's ripgrep). The .git dir is "
                "always skipped.",
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "content (default): matching lines; files_with_matches: just file"
                "paths; count: match count per file",
            },
            "max_results": {
                "type": "integer",
                "description": f"Maximum output lines to return (default {DEFAULT_GREP_MAX_RESULTS})",
            },
            "per_file_limit": {
                "type": "integer",
                "description": "content mode: at most this many matches per file, so one hot "
                "file cannot fill the page (default: no per-file limit)",
            },
            "offset": {
                "type": "integer",
                "description": "Skip this many output lines (prefer cursor, which detects changes)",
            },
            "cursor": {
                "type": "string",
                "description": "next_cursor from the previous page; refused if earlier results changed",
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
        no_ignore = bool(arguments.get("no_ignore", False))
        context = _ctx_int(arguments.get("context"))
        before = _ctx_int(arguments.get("before_context"))
        after = _ctx_int(arguments.get("after_context"))
        per_file = _ctx_int(arguments.get("per_file_limit")) or None
        if output_mode != "content":
            per_file = None  # a per-file cap would falsify counts and changes nothing for -l

        scope = _digest([pattern, path, glob, output_mode, no_ignore, context, before, after,
                         per_file, getattr(env, "workspace_root", None)])
        cursor = None
        try:
            offset = _ctx_int(arguments.get("offset"))
            if arguments.get("cursor"):
                cursor = _decode_cursor(arguments["cursor"])
                if cursor["q"] != scope:
                    raise ValueError("cursor belongs to a different search; restart without it")
                offset = cursor["o"]
            if offset > MAX_SEARCH_OFFSET:
                raise ValueError(f"offset exceeds {MAX_SEARCH_OFFSET}; narrow the search")
        except ValueError as exc:
            return ToolResult(tool_call_id="", content=f"grep failed: {exc}", is_error=True)

        command = _build_search_command(
            pattern, path, glob, output_mode, context, before, after, no_ignore, per_file
        )
        # One look-ahead line past the page tells a full page from a capped one.
        wanted = offset + max_results
        result = await env.execute(_bounded(command, wanted + 1, SEARCH_CAPTURE_BYTES))
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"grep failed (exit {result.exit_code}): {result.stderr.strip()}",
                is_error=True,
            )
        lines, status, partial = _split_capture(result.stdout)
        if status is not None and status not in (0, 1):
            stderr = result.stderr.strip()
            hint = ""
            if "No such file" in stderr:
                hint = f" (path {path!r} does not exist or is not readable)"
            return ToolResult(
                tool_call_id="",
                content=f"grep failed (exit {status}): {stderr}{hint}",
                is_error=True,
            )
        if status == 1 and not lines:
            return ToolResult(
                tool_call_id="",
                content=f"No matches found for {pattern}",
            )

        if cursor is not None and (len(lines) < offset or _digest(lines[:offset]) != cursor["d"]):
            return ToolResult(
                tool_call_id="",
                content="grep failed: stale cursor — results before this page changed; "
                "restart the search without a cursor",
                is_error=True,
            )
        if partial and len(lines) <= offset:
            return ToolResult(
                tool_call_id="",
                content=f"grep failed: the {SEARCH_CAPTURE_BYTES}-byte capture limit was reached "
                f"before offset {offset}; narrow the search",
                is_error=True,
            )
        if status is None and not lines:
            return ToolResult(tool_call_id="", content="grep failed: no output was captured",
                              is_error=True)
        if not lines[offset:]:
            return ToolResult(tool_call_id="", content=f"No more results at offset {offset}")

        regex = None
        if output_mode == "content":
            # Python re only locates the window; rg/grep already decided the match.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    regex = re.compile(pattern)
                except re.error:
                    regex = None
        page = lines[offset:wanted]
        output = "\n".join(_window(line, regex) for line in page)
        # Producer finished and nothing was cut: the count is exact.
        complete = status is not None and len(lines) <= wanted
        shown = f"{offset + 1}-{offset + len(page)}"
        if not complete:
            if partial or len(lines) <= wanted:
                output += (f"\n(capture limit of {SEARCH_CAPTURE_BYTES} bytes reached; "
                           "the last line may be cut and more results may exist)")
            if len(lines) > wanted or not partial:
                output += f"\n(results capped at {max_results})"
            output += f"\n(showing {shown}; total unknown, at least {len(lines)} lines)"
            # A byte-cut line is shown but not claimed: the next page starts on it.
            resume = offset + len(page) - (1 if partial and len(lines) <= wanted else 0)
            if offset < resume <= MAX_SEARCH_OFFSET:
                next_cursor = _encode_cursor({"v": 1, "q": scope, "o": resume,
                                              "d": _digest(lines[:resume])})
                output += f"\nnext_cursor: {next_cursor}"
            else:
                output += "\n(no further page is reachable; narrow the search)"
        elif offset:
            output += f"\n(showing {shown} of {len(lines)} lines)"
        if per_file and _per_file_reached(page, per_file):
            output += (f"\n(per-file limit of {per_file} reached in some files; "
                       "further matches there are omitted)")
        return ToolResult(tool_call_id="", content=output)


def _per_file_reached(lines: list[str], per_file: int) -> bool:
    """Whether any file shows ``per_file`` match lines (context lines use ``-``, not ``:``)."""
    counts: dict[str, int] = {}
    for line in lines:
        found = re.match(r"(.+?):(\d+):", line)
        if found:
            counts[found.group(1)] = counts.get(found.group(1), 0) + 1
    return any(count >= per_file for count in counts.values())


class GlobTool:
    effect = ToolEffect.READ_ONLY
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

        segment_filter: "re.Pattern[str] | None" = None
        refine = ""
        if "/" not in pattern:
            # Bare filename glob: match anywhere under the base path.
            matcher = f"-name {shlex.quote(pattern)}"
        else:
            # Path glob. `find -path` lets `*` match `/` as well, so `src/*.ts`
            # also returned `src/nested/b.ts` — a single `*` is meant to stay within
            # one path segment. `find` cannot express that, so it stays the broad
            # prefilter and `grep -E` applies the exact semantics before capture
            # is bounded: otherwise early-sorting non-matches (`src/node_modules/`)
            # could fill the capture and hide every real match. grep exits 1 when
            # nothing matches, which is not a failure here.
            converted = pattern.replace("**/", "*")
            if not converted.startswith(("./", "/", "*")):
                converted = "./" + converted
            matcher = f"-path {shlex.quote(converted)}"
            segment_filter = _glob_regex(pattern)
            ere = shlex.quote(_glob_translate(pattern, ere=True))
            refine = f" | {{ LC_ALL=C grep -E -e {ere} || [ $? -eq 1 ]; }}"

        # `-L` follows symlinks so a symlinked directory under the base is
        # traversed (matches shell globbing / bash reach).
        command = (
            f"cd {shlex.quote(base)} && "
            f"find -L . -type f {matcher} -not -path '*/.git/*' | sort{refine}"
        )
        # Sorting needs the whole listing, but capture of the filtered list stays
        # bounded; the result cap is applied below, so no line cap is applied here.
        result = await env.execute(_bounded(command, None, SEARCH_CAPTURE_BYTES))
        if result.exit_code != 0:
            return ToolResult(
                tool_call_id="",
                content=f"glob failed (exit {result.exit_code}): {result.stderr.strip()}",
                is_error=True,
            )

        captured, status, partial = _split_capture(result.stdout)
        if status is not None and status != 0:
            return ToolResult(
                tool_call_id="",
                content=f"glob failed (exit {status}): {result.stderr.strip()}",
                is_error=True,
            )
        if partial:
            captured = captured[:-1]  # a cut path is not a real file name
        lines = [line for line in captured if line.strip()]
        if segment_filter is not None:
            # The shell filter already ran; this keeps Python's exact semantics
            # if a grep implementation reads the expression more loosely.
            lines = [line for line in lines if segment_filter.match(line.strip())]
        if not lines and status is not None:
            return ToolResult(tool_call_id="", content=f"No files matched pattern {pattern}")
        capped = len(lines) > GLOB_MAX_RESULTS
        if capped:
            lines = lines[:GLOB_MAX_RESULTS]
        output = "\n".join(lines)
        if capped:
            output += f"\n(results capped at {GLOB_MAX_RESULTS})"
        if status is None:
            output += (f"\n(file list capture limit of {SEARCH_CAPTURE_BYTES} bytes reached; "
                       "matches may be incomplete — narrow the path or pattern)")
        return ToolResult(tool_call_id="", content=output.lstrip("\n"))


class LsTool:
    effect = ToolEffect.READ_ONLY
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
