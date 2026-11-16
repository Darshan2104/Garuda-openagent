"""Conservative import-name rewrites, parsed on the host without executing source."""

import ast
import io
import keyword
import tokenize
from dataclasses import dataclass

MAX_REPLACEMENTS = 1000
_DYNAMIC_NAMES = {"getattr", "setattr", "delattr", "hasattr", "eval", "exec",
                  "globals", "locals", "vars", "__import__"}


@dataclass(frozen=True)
class Rewrite:
    content: str
    replacements: int
    issues: tuple[dict, ...]
    blocked: bool


def identifier(value: object) -> bool:
    return (isinstance(value, str) and len(value) <= 128 and value.isascii() and value.isidentifier()
            and not keyword.iskeyword(value))


def rename_import(content: str, module: str, old: str, new: str) -> Rewrite:
    issues = []

    def issue(code, node=None, *, blocking=False):
        issues.append({"code": code, "line": getattr(node, "lineno", None),
                       "blocking": blocking})

    try:
        tree = ast.parse(content)
        # Validate compiler-only syntax errors too, without executing the code.
        compile(tree, "<python-codemod>", "exec", dont_inherit=True)
        tokens = list(tokenize.generate_tokens(io.StringIO(content, newline=None).readline))
    except (SyntaxError, ValueError, tokenize.TokenError, RecursionError) as exc:
        return Rewrite(content, 0, ({"code": "syntax_error", "line": getattr(exc, "lineno", None),
                                     "blocking": True},), True)

    nodes = list(ast.walk(tree))
    selected = []
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                issue("star_import", node, blocking=True)
            if node.level == 0 and node.module == module:
                for alias in node.names:
                    if alias.name == old:
                        selected.append(alias)
                        if node not in tree.body:
                            issue("non_module_import", node, blocking=True)
        if isinstance(node, ast.Attribute) and node.attr == old:
            issue("attribute_not_transformed", node)
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            needle = old.encode() if isinstance(node.value, bytes) else old
            if needle in node.value:
                issue("string_not_transformed", node)
        if ((isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMES) or
                (isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_NAMES) or
                (isinstance(node, ast.ImportFrom) and node.module == "builtins" and
                 any(alias.name in _DYNAMIC_NAMES for alias in node.names))):
            issue("dynamic_access", node, blocking=True)

    if not selected:
        issue("no_matching_import")
    rename_references = any(alias.asname is None for alias in selected)
    if rename_references:
        if sum((alias.asname or old) == old for alias in selected) != 1:
            issue("ambiguous_binding", blocking=True)
        for node in nodes:
            collision = False
            if isinstance(node, ast.Name):
                collision = (node.id == new or
                             (node.id == old and not isinstance(node.ctx, ast.Load)))
            elif isinstance(node, ast.arg):
                collision = node.arg in {old, new}
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                collision = bool({old, new}.intersection(node.names))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias not in selected and (alias.asname or alias.name.split(".")[0]) in {old, new}:
                        collision = True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                   ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
                collision = node.name in {old, new}
            elif isinstance(node, ast.MatchMapping):
                collision = node.rest in {old, new}
            elif type(node).__name__ in {"TypeVar", "ParamSpec", "TypeVarTuple"}:
                collision = node.name in {old, new}
            if collision:
                issue("ambiguous_binding", node, blocking=True)

    lines = list(io.StringIO(content, newline=""))
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    names = {token.start: token for token in tokens if token.type == tokenize.NAME}
    positions = set()

    def select(node):
        # AST columns are UTF-8 byte offsets; tokenize columns count characters.
        line = lines[node.lineno - 1]
        column = len(line.encode("utf-8")[:node.col_offset].decode("utf-8"))
        token = names.get((node.lineno, column))
        if token is None or token.string != old:
            issue("uncertain_token_position", node, blocking=True)
        else:
            positions.add((offsets[node.lineno - 1] + column, len(old)))

    for alias in selected:
        select(alias)
    if rename_references:
        for node in nodes:
            if isinstance(node, ast.Name) and node.id == old and isinstance(node.ctx, ast.Load):
                select(node)
    if len(positions) > MAX_REPLACEMENTS:
        issue("replacement_limit", blocking=True)
    blocked = any(item["blocking"] for item in issues)
    if blocked:
        return Rewrite(content, 0, tuple(issues), True)
    updated = content
    for start, length in sorted(positions, reverse=True):
        updated = updated[:start] + new + updated[start + length:]
    try:
        compile(ast.parse(updated), "<python-codemod>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError):
        issue("invalid_result", blocking=True)
        return Rewrite(content, 0, tuple(issues), True)
    return Rewrite(updated, len(positions), tuple(issues), False)
