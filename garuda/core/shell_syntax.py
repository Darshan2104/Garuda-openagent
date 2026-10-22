"""Shared lexical views for shell guardrails, not a complete shell parser.

Raw command text remains authoritative for hard refusals. Comment removal is a
convenience for simple commands and must never remove a raw refusal.
"""

import shlex
from dataclasses import dataclass

SHELL_BLANKS = " \t\n"
SHELL_OPERATORS = ";&|()<>"
_COMPLEX_SYNTAX = ("$(", "${", "$'", '$"', "`", "<<", "((", "[[")


@dataclass(frozen=True)
class ShellCharacter:
    index: int
    char: str
    quote: str | None
    escaped: bool
    comment: bool


@dataclass(frozen=True)
class ShellScan:
    characters: tuple[ShellCharacter, ...]
    complete: bool


def shell_lexer(text: str, *, punctuation_chars: str = "") -> shlex.shlex:
    """Tokenize without comments, using Bash separators instead of Unicode space."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=punctuation_chars)
    lexer.whitespace = SHELL_BLANKS
    lexer.whitespace_split = True
    lexer.commenters = ""
    return lexer


def scan_shell(text: str, *, comments: bool = False) -> ShellScan:
    """Annotate simple quotes, escapes and Bash word/comment boundaries."""
    result = []
    quote = None
    escaped = comment = False
    word_start = True
    for index, char in enumerate(text):
        if comments and not quote and not escaped and word_start and char == "#":
            comment = True
        result.append(ShellCharacter(index, char, quote, escaped, comment))
        if comment:
            if char == "\n":
                comment = False
                word_start = True
            continue
        if escaped:
            escaped = False
            if char != "\n":
                word_start = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            word_start = False
        else:
            word_start = char in SHELL_BLANKS + SHELL_OPERATORS
    return ShellScan(tuple(result), quote is None and not escaped)


def strip_simple_comments(text: str) -> str:
    """Remove only simple comments; retain complex syntax conservatively."""
    if any(marker in text for marker in _COMPLEX_SYNTAX):
        return text
    return "".join(item.char for item in scan_shell(text, comments=True).characters
                   if not item.comment or item.char == "\n")


def has_history_reference(text: str) -> bool:
    """History may expand outside literal single quotes, even in comments."""
    if "!" not in text:
        return False
    if any(marker in text for marker in _COMPLEX_SYNTAX):
        return True
    return any(item.char == "!" and (item.comment or not item.escaped and item.quote != "'")
               for item in scan_shell(text, comments=True).characters)


def split_segments(command: str) -> list[str]:
    """Split unquoted, unescaped control operators without removing comments."""
    text = command or ""
    operators = ("&&", "||", "|", ";", "\n")
    segments = []
    start = 0
    for item in scan_shell(text).characters:
        if item.index < start or item.quote or item.escaped:
            continue
        operator = next((op for op in operators if text.startswith(op, item.index)), None)
        if operator is not None:
            segments.append(text[start:item.index])
            start = item.index + len(operator)
    segments.append(text[start:])
    return [segment for segment in segments if segment.strip()]
