"""The size-rotated ACP event trail of one session directory.

An external harness's run appends one normalized metadata record per event to
``acp-events.jsonl``. A step that runs for half a day would grow that file
without bound, so the writer rotates it by size: when the active file would
pass :data:`ROTATE_BYTES`, it is renamed to ``acp-events.000001.jsonl`` (the
next number after the newest rotated part) and a fresh active file starts.
At most :data:`KEEP_ROTATED` rotated parts are kept; older ones are deleted,
oldest first.

Readers see the trail as one sequence: rotated parts in number order, then the
active file. A session written before rotation has only the active file and
reads exactly as before. A trail whose lowest kept part is not ``000001`` had
earlier parts deleted, and readers say so (``pruned``) instead of presenting
what is left as the whole trail.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ACP_EVENTS_FILE = "acp-events.jsonl"
#: The size at which the active file is rotated (64 MiB).
ROTATE_BYTES = 64 * 1024 * 1024
#: How many rotated parts are kept besides the active file.
KEEP_ROTATED = 8

_ROTATED = re.compile(r"^acp-events\.(\d{6})\.jsonl$")


def rotated_name(number: int) -> str:
    """The file name of rotated part ``number`` (``acp-events.000001.jsonl``)."""
    return f"acp-events.{number:06d}.jsonl"


def _rotated_numbers(names) -> list[int]:
    return sorted(int(match.group(1)) for name in names if (match := _ROTATED.match(name)))


def rotated_numbers(directory: str | Path) -> list[int]:
    """The kept rotated part numbers, oldest first; empty for an unrotated trail."""
    try:
        return _rotated_numbers(os.listdir(directory))
    except OSError:
        return []


def part_names(names) -> list[str]:
    """Trail file names among ``names`` in reading order: rotated parts, then the active file."""
    names = set(names)
    ordered = [rotated_name(number) for number in _rotated_numbers(names)]
    return ordered + ([ACP_EVENTS_FILE] if ACP_EVENTS_FILE in names else [])


def part_paths(directory: str | Path) -> list[Path]:
    """The trail's existing files in reading order (only the active file before rotation)."""
    root = Path(directory)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return [root / name for name in part_names(names) if (root / name).is_file()]


def trail_facts(directory: str | Path) -> dict:
    """``{"parts", "pruned"}``: how many files the trail has and whether early parts were deleted."""
    numbers = rotated_numbers(directory)
    active = (Path(directory) / ACP_EVENTS_FILE).is_file()
    return {"parts": len(numbers) + int(active), "pruned": bool(numbers) and numbers[0] != 1}


def iter_lines(directory: str | Path, *, binary: bool = False) -> Iterator:
    """Every line of the trail in order, across rotated parts; unreadable parts are skipped."""
    for path in part_paths(directory):
        try:
            with path.open("rb" if binary else "r", **({} if binary else {"encoding": "utf-8"})) as handle:
                yield from handle
        except OSError:
            continue


@contextmanager
def open_trail(directory: str | Path):
    """``with open_trail(d) as lines:`` — a text handle's place for a reader that walks
    the whole trail line by line (rotated parts, then the active file)."""
    lines = iter_lines(directory)
    try:
        yield lines
    finally:
        lines.close()


def append(directory: str | Path, line: str, *, max_bytes: int | None = None,
           keep: int | None = None) -> None:
    """Append ``line`` to the active file, rotating it first when it would pass ``max_bytes``."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    limit = ROTATE_BYTES if max_bytes is None else max_bytes
    active = root / ACP_EVENTS_FILE
    try:
        size = active.stat().st_size
    except FileNotFoundError:
        size = 0
    if size and size + len(line.encode()) > limit:
        rotate(root, keep=KEEP_ROTATED if keep is None else keep)
    with active.open("a", encoding="utf-8") as handle:
        handle.write(line)


def rotate(directory: str | Path, *, keep: int = KEEP_ROTATED) -> Path:
    """Rename the active file to the next rotated part and delete parts beyond ``keep``."""
    root = Path(directory)
    numbers = rotated_numbers(root)
    number = (numbers[-1] + 1) if numbers else 1
    target = root / rotated_name(number)
    os.replace(root / ACP_EVENTS_FILE, target)
    for old in [*numbers, number][:-keep] if keep > 0 else [*numbers, number]:
        try:
            (root / rotated_name(old)).unlink()
        except FileNotFoundError:
            pass
    return target
