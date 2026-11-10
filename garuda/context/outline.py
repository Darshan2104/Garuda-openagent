"""Host-side Python AST metadata; never executes workspace source."""

import ast
import hashlib
import io
import tokenize
from bisect import bisect_right
from collections import OrderedDict
from copy import copy
from dataclasses import dataclass

from garuda.context.shaper import DEFAULT_MAX_OUTPUT_BYTES

MAX_SOURCE_BYTES = 1024 * 1024
MAX_CACHE_ENTRIES = 32


@dataclass(frozen=True)
class Outline:
    sha256: str
    content: str


def _read_file_lines(normalized: str) -> list[tuple[int, int]]:
    """Map each AST physical line to its first/last read_file (str.splitlines) line."""
    spans = [(0, 0)]
    next_line = 1
    for physical in normalized.split("\n"):
        # read_file also breaks on \f, \v, \x1c-\x1e, \x85, U+2028 and U+2029, so one
        # physical line may span several read_file lines; ranges cover all of them.
        count = len((physical + "\n").splitlines())
        spans.append((next_line, next_line + count - 1))
        next_line += count
    return spans


class _Declarations(ast.NodeVisitor):
    def __init__(self, source: str):
        self.spans = _read_file_lines(source)
        self.scope: list[str] = []
        self.lines: list[str] = []
        self.decorators: list[tuple[int, int]] = []
        start = None
        statement_start = True
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in {tokenize.INDENT, tokenize.DEDENT, tokenize.NL, tokenize.COMMENT}:
                continue
            if token.type == tokenize.NEWLINE:
                if start is not None:
                    self.decorators.append((start, token.start[0]))
                start = None
                statement_start = True
            else:
                if statement_start and token.string == "@":
                    start = token.start[0]
                statement_start = False
        self.decorator_starts = [start for start, _ in self.decorators]

    def _range(self, first: int, last: int) -> str:
        return f"lines {self.spans[first][0]}-{self.spans[last][1]}"

    def _decorator_range(self, node: ast.expr) -> tuple[int, int]:
        # AST expression ranges omit a decorator's @ and surrounding parentheses.
        index = bisect_right(self.decorator_starts, node.lineno) - 1
        if index < 0:
            raise ValueError("missing decorator range")
        start, end = self.decorators[index]
        if not start <= node.lineno <= node.end_lineno <= end:
            raise ValueError("ambiguous decorator range")
        return start, end

    def _start(self, node: ast.stmt) -> int:
        return min([node.lineno, *(
            self._decorator_range(d)[0] for d in getattr(node, "decorator_list", [])
        )])

    def _definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        name = ".".join([*self.scope, node.name])
        start = self._start(node)
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        header = copy(node)
        header.body = []
        header.decorator_list = []
        self.lines.extend([
            f"{kind} {name} [{self._range(start, node.end_lineno)}]",
            f"  signature: {ast.unparse(header)}",
        ])
        for decorator in node.decorator_list:
            first, last = self._decorator_range(decorator)
            self.lines.append(
                f"  decorator [{self._range(first, last)}]: "
                f"@{ast.unparse(decorator)}"
            )
        body_start = self._start(node.body[0])
        self.lines.append(f"  hidden body {name} [{self._range(body_start, node.end_lineno)}]")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _definition
    visit_AsyncFunctionDef = _definition
    visit_ClassDef = _definition

    def visit_Import(self, node: ast.Import | ast.ImportFrom) -> None:
        scope = ".".join(self.scope) or "<module>"
        self.lines.append(
            f"import in {scope} [{self._range(node.lineno, node.end_lineno)}]: {ast.unparse(node)}"
        )

    visit_ImportFrom = visit_Import


class PythonOutlineCache:
    """Bounded metadata cache keyed only by content digest and rendering settings."""

    def __init__(self):
        self._entries: OrderedDict[tuple[str, int, int], Outline] = OrderedDict()

    def render(self, source: str, *, max_lines: int, max_line_chars: int) -> Outline:
        encoded = source.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        key = (digest, max_lines, max_line_chars)
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            return cached

        prefix = f"source_sha256: {digest} (UTF-8 of Environment.read_file text)"
        reason = None
        if len(encoded) > MAX_SOURCE_BYTES:
            reason = f"source exceeds the {MAX_SOURCE_BYTES}-byte host parsing limit"
        else:
            try:
                tree = ast.parse(source)
                normalized = source.replace("\r\n", "\n").replace("\r", "\n")
                declarations = _Declarations(normalized)
                declarations.visit(tree)
                total = len(source.splitlines())
                omitted = f"lines 1-{total}" if total else "none (empty file)"
                content = "\n".join([
                    "Python AST outline (normalized metadata; not a full source read)",
                    prefix,
                    f"Original source omitted: {omitted} (including comments and executable statements).",
                    "Hidden body ranges include nested declarations; those are metadata below, too.",
                    *declarations.lines,
                ])
                lines = content.splitlines()
                if (len(lines) > max_lines or any(len(line) > max_line_chars for line in lines)
                        or len(content.encode("utf-8")) > DEFAULT_MAX_OUTPUT_BYTES):
                    reason = "complete outline exceeds read_file output bounds"
            except SyntaxError as exc:
                reason = f"invalid Python syntax at line {exc.lineno}: {exc.msg}"
            except (ValueError, RecursionError, tokenize.TokenError):
                reason = "source could not be parsed/rendered safely by the host Python AST parser"

        if reason is not None:
            content = (
                f"Python outline unavailable: {reason}. No outline returned.\n{prefix}\n"
                "Use read_file without outline, with offset/limit for raw source."
            )
        result = Outline(digest, content)
        self._entries[key] = result
        if len(self._entries) > MAX_CACHE_ENTRIES:
            self._entries.popitem(last=False)
        return result
