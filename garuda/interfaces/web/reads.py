"""What the dashboard reads: the session list and one run's turn structure.

Deliberately thin. The two hard parts already exist and are already tested:
``eval/dashboard.py::row_from_session_meta`` turns a ``meta.json`` into
status/turns/tokens/cost/duration with all its on-disk coercion hardening, and
``observability/trajectory.py`` turns an ``events.jsonl`` into turns. This module
joins them and adds nothing of its own that could be wrong.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from garuda.context.condenser import make_condenser
from garuda.core.buffer import ToolOutputBuffer
from garuda.core.events import SUBAGENT_LOG_DIR
from garuda.core.sessions import SessionStore, validate_session_ref
from garuda.eval.dashboard import row_from_session_meta
from garuda.interfaces.web.tail import tail_jsonl
from garuda.observability.trajectory import Run, TrajectoryReader

logger = logging.getLogger(__name__)

#: `?file=` is an enum, never a path. There is no filesystem parameter to defend.
RAW_FILES = ("meta.json", "messages.json", "state.json")

#: A run list is a table; a ceiling keeps one request from reading ten thousand
#: meta.json files because a client sent limit=999999.
MAX_LIMIT = 500
DEFAULT_LIMIT = 100

#: One buffer can hold a whole `grep -r`. The UI asks for a slice; this is the cap on
#: what a single response will carry.
MAX_BUFFER_BYTES = 512 * 1024

#: How recently a log must have been written for its run to count as live.
#:
#: `meta.json` says `running` until a run *finishes writing it*, so a killed run — Ctrl-C, a
#: crash, a closed terminal — says `running` forever. Four sessions from a month ago did
#: exactly that on this machine. Anything keying off the status alone therefore polls those
#: runs for eternity, and the client that did so re-rendered the whole page every few
#: seconds. The log's mtime is the only evidence that something is actually happening.
LIVE_WINDOW_SECONDS = 90.0

#: The raw-event escape hatch serves a window, not a log. A finished harbor trial can
#: run to tens of thousands of events and no browser wants them in one response.
MAX_EVENT_WINDOW = 500

#: How many open runs keep a warm reader. Each holds its session's events in memory,
#: so this is a memory ceiling as much as a cache size.
READER_CACHE_SIZE = 8


class ReaderCache:
    """Warm :class:`TrajectoryReader` instances, keyed by resolved path.

    Two reasons this exists rather than constructing a reader per request. A reader
    keeps a byte cursor, so a refetch of an open run reads only the bytes appended
    since the last one — which is what makes the detail view cheap to poll in step 6.
    And ``build_run`` is re-run over the accumulated events, so its cost is paid on
    new data only.

    **The lock is held across ``refresh()``, deliberately.** A reader is not
    thread-safe: ``refresh`` reads its tail then extends its own event list, so two
    HTTP threads landing on one run would interleave those steps and append the same
    events twice — silently doubling every turn. Serializing the parse costs a few
    milliseconds on a large log and removes the entire class of bug. It also means the
    returned ``Run`` and event count are read from one consistent state; fetching them
    under separate locks could report a count from a later parse than the run.
    """

    def __init__(self, capacity: int = READER_CACHE_SIZE) -> None:
        self._capacity = max(1, capacity)
        self._readers: OrderedDict[Path, TrajectoryReader] = OrderedDict()
        self._lock = threading.Lock()

    def read(self, path: Path) -> tuple[Run, int]:
        """The current ``Run`` for a session or trial directory, and its event count."""
        with self._lock:
            reader = self._readers.pop(path, None)
            if reader is None:
                reader = TrajectoryReader(path)
            self._readers[path] = reader
            while len(self._readers) > self._capacity:
                self._readers.popitem(last=False)
            return reader.refresh(), reader.event_count

    def forget(self, path: Path) -> None:
        """Drop a reader. Only needed when a caller knows the file is gone — normal
        truncation and replacement are handled inside ``EventTail`` itself."""
        with self._lock:
            self._readers.pop(path, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._readers)


def _cache(readers: ReaderCache | None) -> ReaderCache:
    """``readers``, or a throwaway.

    Explicitly ``is not None`` and not ``readers or ReaderCache()``: ``ReaderCache``
    defines ``__len__``, so an *empty* cache is falsy and the ``or`` form silently
    substituted a throwaway on the first request of every run — the one request that
    most needed to seed the cache. It worked, which is what made it worth a helper.
    """
    return readers if readers is not None else ReaderCache()


def summarize_session(meta: dict[str, Any], store: SessionStore) -> dict[str, Any]:
    """One row for the run list.

    Built from the raw ``meta.json`` a session wrote, so a run still in flight — with
    no ``usage``, no ``final_message`` and possibly a partial file — degrades to
    missing cells rather than failing the row.
    """
    row = asdict(row_from_session_meta(meta))
    session_id = meta.get("session_id") or ""
    events_path = store.events_path(session_id) if session_id else None
    has_events = bool(events_path and events_path.is_file())
    claims_running = row.get("status") == "running"
    written_ago = _seconds_since_write(events_path) if has_events else None
    # Live means "the log is still being written to", not "meta says running". The two only
    # agree for a run that is genuinely in flight; for a killed one the status is a
    # permanent lie, and believing it is what made the UI poll forever.
    live = bool(claims_running and written_ago is not None and written_ago <= LIVE_WINDOW_SECONDS)
    return {
        **row,
        "live": live,
        #: Claims to be running but its log went quiet. Surfaced rather than corrected: the
        #: run really did not finish, and silently relabelling it "failed" would invent an
        #: outcome the harness never recorded.
        "stale": bool(claims_running and not live),
        "written_ago_seconds": round(written_ago, 1) if written_ago is not None else None,
        # `row.source` is truncated to 8 chars for the terminal table; the UI needs
        # the whole id to link anywhere.
        "session_id": session_id,
        "task": meta.get("task"),
        "agent": meta.get("agent"),
        "mode": meta.get("mode"),
        "workspace": meta.get("workspace"),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "resumed_from": meta.get("resumed_from"),
        "final_message": meta.get("final_message"),
        "metrics": meta.get("metrics"),
        "acceptance": meta.get("acceptance"),
        "has_events": has_events,
        "events_bytes": events_path.stat().st_size if has_events else 0,
    }


def list_runs(
    store: SessionStore,
    *,
    limit: int = DEFAULT_LIMIT,
    status: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """The run list, newest first, filtered.

    Filtering happens after the read because ``list_sessions`` is the only thing that
    knows how to sort and how to skip an unreadable session; re-implementing that here
    to push filters down would duplicate its defensive behaviour.
    """
    limit = max(0, min(limit, MAX_LIMIT))
    # Read a wider window than requested so filters do not silently return fewer rows
    # than exist. Still bounded.
    metas = store.list_sessions(limit=MAX_LIMIT)
    rows = []
    for meta in metas:
        if status and meta.get("status") != status:
            continue
        if agent and meta.get("agent") != agent:
            continue
        if model and model not in (meta.get("model") or ""):
            continue
        if query:
            haystack = f"{meta.get('task') or ''}\n{meta.get('session_id') or ''}".lower()
            if query.lower() not in haystack:
                continue
        rows.append(summarize_session(meta, store))
    return {"runs": rows[:limit], "total": len(rows), "limit": limit}


def condenser_threshold(name: str | None) -> float | None:
    """The usage fraction at which this run's condenser fires, or ``None``.

    Built by asking the actual condenser rather than copying a number into the
    frontend. The default strategy fires at 0.75 and ``recent_window`` at 0.85, so a
    single hardcoded threshold would draw the wrong line on most runs — and a line in
    the wrong place on a context-pressure chart is worse than no line, because it is
    read as the reason a compaction did or didn't happen.

    ``None`` for an unknown strategy, and for ``summarizing``, which has no trigger
    fraction of its own — it compacts when asked to.
    """
    if not name:
        return None
    try:
        condenser = make_condenser(name)
    except (ValueError, TypeError):
        return None
    for attribute in ("microcompact_fraction", "trigger_fraction"):
        value = getattr(condenser, attribute, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1:
            return float(value)
    return None


def read_run(
    store: SessionStore, session_id: str, *, readers: ReaderCache | None = None
) -> dict[str, Any] | None:
    """One run, turn-structured. ``None`` when the session has no readable log."""
    session_id = validate_session_ref(session_id)
    directory = store.session_dir(session_id)
    if not directory.is_dir():
        return None
    meta = _load_json(directory / "meta.json") or {}
    run, event_count = _cache(readers).read(directory)
    return {
        "run": summarize_session({**meta, "session_id": session_id}, store),
        "meta": meta,
        "trajectory": run.to_dict(include_events=False),
        "event_count": event_count,
        "condenser_threshold": condenser_threshold((run.config or {}).get("condenser")),
    }


def read_events(
    store: SessionStore,
    session_id: str,
    *,
    start: int = 0,
    limit: int = MAX_EVENT_WINDOW,
    readers: ReaderCache | None = None,
) -> dict[str, Any] | None:
    """A window of the verbatim log, addressed by **index**, for the escape hatch.

    Indices, not byte offsets, because that is what the trajectory graph references:
    ``Turn.events`` is a list of positions in ``Run.events``, so "show me the raw events
    behind this turn" is a slice. The live tail is byte-addressed instead and is a
    separate route — a cursor needs bytes, a cross-reference needs indices, and
    conflating them would make one of the two wrong.
    """
    session_id = validate_session_ref(session_id)
    directory = store.session_dir(session_id)
    if not directory.is_dir():
        return None
    run, event_count = _cache(readers).read(directory)
    start = max(0, start)
    limit = max(0, min(limit, MAX_EVENT_WINDOW))
    window = run.events[start : start + limit]
    return {
        "start": start,
        "limit": limit,
        "count": len(window),
        "total": event_count,
        "events": window,
    }


def read_subagent(
    store: SessionStore,
    session_id: str,
    sub_session_id: str,
    *,
    readers: ReaderCache | None = None,
) -> dict[str, Any] | None:
    """One nested run's own turn structure, from the sibling log beside its parent's.

    ``sub_session_id`` goes through ``validate_session_ref`` — the same guard the parent id
    gets — because it reaches this function from a URL. It is a session id in every sense
    (``EventStore`` generates it the same way), so it gets the same validator rather than a
    second, weaker one written for the occasion.

    ``None`` when there is no such log, which includes every subagent invoked before this
    build started persisting them. That reads as "not recorded", not as an error.
    """
    session_id = validate_session_ref(session_id)
    sub_session_id = validate_session_ref(sub_session_id)
    path = store.session_dir(session_id) / SUBAGENT_LOG_DIR / f"{sub_session_id}.jsonl"
    if not path.is_file():
        return None
    run, event_count = _cache(readers).read(path)
    return {
        "session_id": sub_session_id,
        "parent_session_id": session_id,
        "trajectory": run.to_dict(include_events=False),
        "event_count": event_count,
        "bytes": path.stat().st_size,
    }


def read_raw(store: SessionStore, session_id: str, name: str | None) -> Any | None:
    """One of three named documents from a session directory.

    ``name`` is matched against a fixed tuple, so this cannot be turned into an
    arbitrary-file read however the parameter is spelled. Deliberately **required**
    with no default: an enum has no sensible default, and ``parse_qs`` drops a blank
    value, so a client sending ``?file=`` would otherwise be silently served
    ``meta.json`` instead of being told it asked for nothing.
    """
    if name not in RAW_FILES:
        raise ValueError(
            f"`file` must be one of {', '.join(RAW_FILES)}; got {name!r}."
        )
    session_id = validate_session_ref(session_id)
    return _load_json(store.session_dir(session_id) / name)


def list_buffers(store: SessionStore, session_id: str) -> list[dict[str, Any]]:
    """Archived tool output for a session, for expanding a ``[buffer:…]`` stub."""
    session_id = validate_session_ref(session_id)
    # `root` is the buffers directory itself, not the sessions root — passing the
    # latter would look for *.txt beside the session dirs and always find none.
    buffer = ToolOutputBuffer(session_id=session_id, root=store.session_dir(session_id) / "buffers")
    return [asdict(ref) for ref in buffer.list_buffers()]


def read_buffer(store: SessionStore, session_id: str, buffer_id: str) -> dict[str, Any] | None:
    """One buffer's contents, capped.

    The id goes through ``ToolOutputBuffer``, whose own path builder already
    sanitises it — reusing that rather than writing a second validator.
    """
    session_id = validate_session_ref(session_id)
    # `root` is the buffers directory itself, not the sessions root — passing the
    # latter would look for *.txt beside the session dirs and always find none.
    buffer = ToolOutputBuffer(session_id=session_id, root=store.session_dir(session_id) / "buffers")
    try:
        content = buffer.read(buffer_id)
    except KeyError:
        # `read` raises rather than returning None for a missing buffer, and an id the
        # client made up is a 404, not a 500.
        return None
    truncated = len(content) > MAX_BUFFER_BYTES
    return {
        "buffer_id": buffer_id,
        "content": content[:MAX_BUFFER_BYTES],
        "truncated": truncated,
        "bytes": len(content),
    }


def tail_run(
    store: SessionStore, session_id: str, *, offset: int = 0
) -> dict[str, Any] | None:
    """One poll of a live run: new events plus the status that says whether to keep going.

    Status travels with the events deliberately. Two requests — one for the tail, one for
    the status — can interleave such that the client sees ``finished`` and then a batch of
    events it decides not to render, or sees ``running`` forever because the status read
    landed first. One response cannot disagree with itself.
    """
    session_id = validate_session_ref(session_id)
    directory = store.session_dir(session_id)
    if not directory.is_dir():
        return None
    events_path = store.events_path(session_id)
    result = tail_jsonl(events_path, offset)
    meta = _load_json(directory / "meta.json") or {}
    written_ago = _seconds_since_write(events_path)
    claims_running = meta.get("status") == "running"
    # Same definition as the run list uses, and for the same reason: `meta.json` says
    # `running` forever for a killed run, so a client that trusted the status alone would
    # poll a month-old session until the tab closed.
    live = bool(claims_running and written_ago is not None
                and written_ago <= LIVE_WINDOW_SECONDS)
    return {
        **result.as_dict(),
        "status": meta.get("status"),
        "session_id": session_id,
        # `eof and not running` is the client's stop condition, and it needs both halves
        # from the same read: a finished run whose last events have not been consumed yet
        # must keep polling.
        "running": live,
        "stale": bool(claims_running and not live),
        "written_ago_seconds": round(written_ago, 1) if written_ago is not None else None,
    }


def trajectory_from(path: Path) -> Run:
    """A Run from any events.jsonl-bearing directory."""
    return TrajectoryReader(path).refresh()


def _seconds_since_write(path: Path | None) -> float | None:
    """How long ago this file was last written, or ``None`` if it cannot be told."""
    if path is None:
        return None
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _load_json(path: Path) -> Any | None:
    """Parse a JSON document off disk, or ``None``.

    Defensive rather than using ``SessionStore.load_meta``, whose bare ``json.loads``
    raises on the partial ``meta.json`` a still-running session can have on disk.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("Unreadable JSON at %s", path, exc_info=True)
        return None
