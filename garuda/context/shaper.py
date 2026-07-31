"""Head/tail truncation of a single tool observation.

The one place a tool result is made to fit. Lossy by construction — the buffer
(``core/buffer.py``) is the lossless alternative and is tried first; this is what
happens when there is no buffer, or when storing to it failed.
"""

# Ceiling on one tool result in the context window. 30 KiB, shared by every caller
# that used to redeclare the literal, so the cap can be reasoned about in one place.
DEFAULT_MAX_OUTPUT_BYTES = 30_720

# Errors are truncated tail-heavy on purpose: a traceback's exception line, a
# compiler's error summary, and pytest's failure block are all at the *end* of the
# output, so an even split throws away the half that says what went wrong.
ERROR_HEAD_FRACTION = 0.3


def shape_observation(
    text: str, max_bytes: int, is_error: bool = False, head_fraction: float | None = None
) -> str:
    if not text:
        return "(command failed with no output)" if is_error else "(no output)"
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    if head_fraction is None:
        head_fraction = ERROR_HEAD_FRACTION if is_error else 0.5
    head_bytes = max(0, min(max_bytes, int(max_bytes * head_fraction)))
    tail_bytes = max_bytes - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    omitted = len(encoded) - max_bytes
    return f"{head}\n\n...[truncated {omitted} bytes]...\n\n{tail}"
