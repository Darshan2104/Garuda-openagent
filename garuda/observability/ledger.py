"""The idempotent usage ledger (plan task E.1, #168).

One append-only JSONL file per month under ``<global home>/usage/`` (``2026-10.jsonl``),
written under the store lock. It is the cross-session source for usage statistics; a
native session's own metrics (``core/metrics.py``) are unchanged, and the ledger's native
totals must equal them.

**Kinds**, each with a distinct meaning:

====================  =======================================================  =============
``native_model_call`` one inference attempt (retries and unpriced ones too)    one call
``acp_usage_snapshot`` an adapter-reported occupancy/cumulative figure          latest only
``acp_usage_delta``   per-turn usage, or a delta from a *proved* cumulative     summed
``limit_event``       a structured provider limit stop (E.2)                    counted once
``fallback_start``    a recorded pre-start choice of another candidate          counted once
====================  =======================================================  =============

**Idempotency.** Every record has a stable ``key``. The writer refuses a second record
with the same key under the lock, so replays, retries and a crash between an append and
the cursor that follows it can finish an identified record but never create a second
charge. A torn final line (a crash mid-write) is fenced off by the next append and
ignored by readers.

**Schema only.** A record holds identities and counts: ids, role/origin/purpose, harness,
adapter and exact model id, token counts, a known cost or ``null``, durations. It never
holds task text, prompts, outputs, tool arguments, account names, raw provider payloads
or paths; the writer refuses any field outside the per-kind schema and any string that
looks like a path. Unknown stays ``null`` and is never zero-filled.

**Retention.** Month files are kept for 13 months by default and pruned by month;
cleaning up sessions (``sessions.keep_days``) never touches the ledger.

POSIX only, like the other strict stores.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from garuda.runtime import strict_store as ss

VERSION = 1
KEEP_MONTHS = 13

NATIVE, SNAPSHOT, DELTA, LIMIT, FALLBACK = (
    "native_model_call", "acp_usage_snapshot", "acp_usage_delta", "limit_event", "fallback_start")


class LedgerRefused(ValueError):
    """A record the ledger will not write. ``code`` is stable."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+=/-]{0,127}$")


def _token(value: Any) -> bool:
    return (isinstance(value, str) and bool(_TOKEN.match(value)) and ".." not in value
            and not value.startswith(("/", "~")))


def _count(value: Any) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _number(value: Any) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)
                             and value >= 0 and value == value)


def _flag(value: Any) -> bool:
    return value is None or isinstance(value, bool)


def _opt_token(value: Any) -> bool:
    return value is None or _token(value)


_COMMON = {
    "version": lambda v: v == VERSION, "kind": _token, "key": _token,
    "time": lambda v: _number(v) and v is not None,
    "project_id": _opt_token, "session_id": _opt_token, "root_id": _opt_token,
    "role": _opt_token, "origin": _opt_token, "flow_step": _opt_token, "attempt": _count,
    "harness": _opt_token, "adapter": _opt_token, "adapter_version": _opt_token,
    "model": _opt_token, "call_purpose": _opt_token, "binding_role": _opt_token,
}
_TOKENS = {"input_tokens": _count, "output_tokens": _count, "total_tokens": _count,
           "cache_tokens": _count, "cost_usd": _number, "pricing_source": _opt_token}
SCHEMA: dict[str, dict[str, Any]] = {
    NATIVE: {**_COMMON, **_TOKENS, "call_id": _opt_token, "calls": _count, "duration_ms": _number,
             "outcome": _opt_token, "retry": _count, "fallback_reason": _opt_token,
             "accounting": _opt_token},
    SNAPSHOT: {**_COMMON, **_TOKENS, "segment": _opt_token, "source": _opt_token,
               "context_used": _count, "context_size": _count, "cumulative": _flag,
               "delta_unavailable": _opt_token, "report_id": _opt_token, "seq": _count},
    DELTA: {**_COMMON, **_TOKENS, "segment": _opt_token, "source": _opt_token, "epoch": _count,
            "window": _opt_token, "turn_id": _opt_token, "report_id": _opt_token, "seq": _count,
            "duration_ms": _number},
    LIMIT: {**_COMMON, "segment": _opt_token, "source": _opt_token, "reset_at": _number,
            "account_digest": _opt_token, "limit_id": _opt_token, "reason": _opt_token},
    FALLBACK: {**_COMMON, "from_harness": _opt_token, "to_harness": _opt_token,
               "reason": _opt_token},
}


def validate(record: dict) -> dict:
    """The record if it fits the schema, else :class:`LedgerRefused`."""
    if not isinstance(record, dict) or record.get("kind") not in SCHEMA:
        raise LedgerRefused("ledger.unknown_kind", "not a ledger record kind")
    fields = SCHEMA[record["kind"]]
    for name, value in record.items():
        check = fields.get(name)
        if check is None:
            raise LedgerRefused("ledger.field_not_in_schema", f"{name!r} is not a field of "
                                f"{record['kind']}")
        if not check(value):
            raise LedgerRefused("ledger.invalid_value", f"{name!r} is not a valid value")
    for required in ("version", "kind", "key", "time"):
        if required not in record:
            raise LedgerRefused("ledger.missing_field", f"{required!r} is required")
    return record


def default_root() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "usage"


def month_of(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m")


class Ledger:
    """The usage ledger under one directory."""

    def __init__(self, root: str | Path | None = None, *, clock=time.time):
        self.root = Path(root) if root else default_root()
        self._clock = clock
        self._seen: dict[Path, tuple[int, set[str]]] = {}

    # --- files -----------------------------------------------------------------------

    def _path(self, moment: float) -> Path:
        return self.root / f"{month_of(moment)}.jsonl"

    def files(self) -> list[Path]:
        return sorted(self.root.glob("????-??.jsonl")) if self.root.is_dir() else []

    def _known(self, path: Path) -> set[str]:
        """Keys already in ``path``, read incrementally (call under the lock)."""
        offset, keys = self._seen.get(path, (0, set()))
        try:
            size = path.stat().st_size
        except OSError:
            self._seen[path] = (0, set())
            return self._seen[path][1]
        if size < offset:
            offset, keys = 0, set()
        if size > offset:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
            cut = chunk.rfind(b"\n")
            for line in chunk[: cut + 1].splitlines():
                try:
                    key = json.loads(line).get("key")
                except (ValueError, AttributeError):
                    continue
                if isinstance(key, str):
                    keys.add(key)
            offset += cut + 1
        self._seen[path] = (offset, keys)
        return keys

    # --- writing ---------------------------------------------------------------------

    def append(self, record: dict) -> bool:
        """Append ``record`` unless its key is already there. ``True`` when written."""
        return self.append_many([record]) == 1

    def append_many(self, records: list[dict]) -> int:
        """Append the records not already present; returns how many were written."""
        records = [validate({"version": VERSION, **r}) for r in records]
        written = 0
        try:
            with ss.exclusive_lock(self.root):
                for record in records:
                    path = self._path(record["time"])
                    if record["key"] in self._known(path):
                        continue
                    self._write(path, record)
                    written += 1
        except ss.StorageError as exc:
            raise LedgerRefused("ledger.unavailable", str(exc)) from exc
        return written

    def _write(self, path: Path, record: dict) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
            if os.fstat(fd).st_size and not self._ends_with_newline(path):
                data = b"\n" + data  # fence off a torn line left by a crash
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        offset, keys = self._seen.get(path, (0, set()))
        keys.add(record["key"])
        self._seen[path] = (os.stat(path).st_size, keys)

    @staticmethod
    def _ends_with_newline(path: Path) -> bool:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"

    # --- cursors (cumulative sources) ---------------------------------------------------

    @property
    def _cursors_path(self) -> Path:
        return self.root / "cursors.json"

    def cursors(self) -> dict:
        document = ss.read_document(self._cursors_path, versions=(1,))
        return (document or {}).get("sources", {})

    def update_cursors(self, mutate) -> dict:
        """Apply ``mutate(sources)`` to the cursor table under the lock, atomically."""
        try:
            with ss.exclusive_lock(self.root):
                document = ss.read_document(self._cursors_path, versions=(1,)) or {
                    "version": 1, "sources": {}}
                mutate(document["sources"])
                ss.write_document(self._cursors_path, document)
                return document["sources"]
        except ss.StorageError as exc:
            raise LedgerRefused("ledger.unavailable", str(exc)) from exc

    # --- reading ---------------------------------------------------------------------

    def records(self, *, since: float | None = None, until: float | None = None,
                kind: str | None = None) -> Iterator[dict]:
        """Every readable record in time order of files; torn or alien lines are skipped."""
        for path in self.files():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("version") != VERSION:
                    continue
                moment = record.get("time")
                if not isinstance(moment, (int, float)):
                    continue
                if (since is not None and moment < since) or (until is not None and moment >= until):
                    continue
                if kind is not None and record.get("kind") != kind:
                    continue
                yield record

    def malformed_lines(self) -> int:
        count = 0
        for path in self.files():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    if not isinstance(json.loads(line), dict):
                        count += 1
                except ValueError:
                    count += 1
        return count

    # --- retention -------------------------------------------------------------------

    def prune(self, keep_months: int = KEEP_MONTHS, now: float | None = None) -> list[str]:
        """Delete month files older than ``keep_months``; returns their names."""
        moment = datetime.fromtimestamp(self._clock() if now is None else now, timezone.utc)
        index = moment.year * 12 + moment.month - 1 - keep_months
        removed = []
        with ss.exclusive_lock(self.root):
            for path in self.files():
                year, month = (int(part) for part in path.stem.split("-"))
                if year * 12 + month - 1 < index:
                    path.unlink()
                    self._seen.pop(path, None)
                    removed.append(path.name)
        return removed


# --- totals ---------------------------------------------------------------------------


def totals(records: list[dict]) -> dict:
    """Sum what may be summed: native calls and *proved* ACP deltas.

    Snapshots are display evidence and are never added. ``coverage`` says what was left
    out, so a total is never mistaken for a complete one; unknown cost stays separate
    from known cost."""
    out = {"native_calls": 0, "acp_delta_records": 0, "input_tokens": 0, "output_tokens": 0,
           "total_tokens": 0, "cache_tokens": 0, "cost_usd": 0.0, "cost_unknown_records": 0,
           "snapshots_excluded": 0, "fallback_starts": 0, "limit_events": 0}
    for record in records:
        kind = record.get("kind")
        if kind == SNAPSHOT:
            out["snapshots_excluded"] += 1
            continue
        if kind == FALLBACK:
            out["fallback_starts"] += 1
            continue
        if kind == LIMIT:
            out["limit_events"] += 1
            continue
        out["native_calls" if kind == NATIVE else "acp_delta_records"] += 1
        for field in ("input_tokens", "output_tokens", "total_tokens", "cache_tokens"):
            out[field] += record.get(field) or 0
        if record.get("cost_usd") is None:
            out["cost_unknown_records"] += 1
        else:
            out["cost_usd"] = round(out["cost_usd"] + record["cost_usd"], 8)
    out["coverage"] = "snapshots_excluded" if out["snapshots_excluded"] else "complete"
    return out
