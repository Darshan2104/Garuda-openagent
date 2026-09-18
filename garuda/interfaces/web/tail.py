"""Byte-offset tailing of an ``events.jsonl`` while it is being written.

This is what makes the dashboard live for **every** run, not only ones it launched:
``run_agent_task`` and ``cli.py`` both call ``events.attach_persistence()``, so the file is
already being appended to by any ``garuda run`` or ``garuda chat`` in another terminal. No
write mode, no callback registration, no change to ``EventStore``.

Three properties carry the whole module, and each is a bug that would otherwise be
permanent rather than transient:

**Bytes, not lines.** A cursor has to be a byte offset because that is the only thing
``seek`` accepts, and re-counting lines to find position N would re-read the file every
poll — which is the cost this exists to avoid.

**A torn final line is held back.** ``EventStore.append`` does open/write/close per event,
so an append is *not* atomic: a poll can land mid-write and see half a JSON object. The
offset only ever advances past the last ``b"\\n"``, so the fragment stays unconsumed and
arrives whole on the next poll. Advancing past a partial line would desync the cursor
**permanently**, not just for that poll — every subsequent read would start mid-object.

**Truncation is detected, not inferred.** Harbor's ``events.save()`` rewrites a trial's log
wholesale, so a re-run shrinks the file. ``offset > size`` catches that and tells the client
to restart from zero rather than seeking past the end and reporting a quiet, permanent EOF.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: One poll's ceiling. A run that produces a burst larger than this is delivered over
#: several polls with `eof: false`, which the client already handles — it polls again
#: immediately rather than waiting out the interval.
MAX_TAIL_BYTES = 4 * 1024 * 1024


@dataclass
class TailResult:
    """One poll's worth of a growing log."""

    #: Where the client should resume. Always on a line boundary.
    offset: int
    #: The file's size when it was read, so a client can show how far behind it is.
    size: int
    events: list[dict[str, Any]] = field(default_factory=list)
    #: The cursor has reached the end of the file as of this read.
    eof: bool = True
    #: The file shrank or was replaced; `offset` has been reset to 0 and the client should
    #: discard what it has rather than appending to it.
    truncated: bool = False
    #: Complete lines that would not parse. Skipped, but counted — silently dropping them
    #: would make a corrupt log look like a quiet one.
    malformed: int = 0
    #: The file does not exist. Not an error: a session directory exists before its first
    #: event is written.
    missing: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "size": self.size,
            "events": self.events,
            "eof": self.eof,
            "truncated": self.truncated,
            "malformed": self.malformed,
            "missing": self.missing,
        }


def tail_jsonl(path: Path, offset: int = 0, *, max_bytes: int = MAX_TAIL_BYTES) -> TailResult:
    """Read whatever complete JSONL records have appeared since ``offset``."""
    offset = max(0, offset)
    try:
        size = path.stat().st_size
    except OSError:
        # A session directory exists before its first event lands, and a poller that
        # starts with the run must not fail on that gap.
        return TailResult(offset=offset, size=0, eof=True, missing=True)

    if offset > size:
        return TailResult(offset=0, size=size, eof=False, truncated=True)
    if offset == size:
        return TailResult(offset=offset, size=size, eof=True)

    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(max_bytes)
    except OSError:
        logger.debug("Could not read %s at %d", path, offset, exc_info=True)
        return TailResult(offset=offset, size=size, eof=False)

    cut = chunk.rfind(b"\n")
    if cut == -1:
        # A partial line and nothing else. Consume nothing: advancing here would leave the
        # cursor inside a JSON object and desync every later read.
        return TailResult(offset=offset, size=size, eof=False)

    events: list[dict[str, Any]] = []
    malformed = 0
    for line in chunk[: cut + 1].splitlines():
        if not line.strip():
            # Blank lines are skipped silently, matching `EventStore.load`.
            continue
        try:
            record = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(record, dict):
            events.append(record)
        else:
            malformed += 1

    consumed = offset + cut + 1
    return TailResult(
        offset=consumed,
        size=size,
        events=events,
        eof=consumed >= size,
        malformed=malformed,
    )
