"""ACP usage reports into the ledger (plan task E.1, #168).

An adapter's usage report is **not** a billable per-call figure unless the exact adapter
version has been *proved* to report one (spikes A.3/A.6; the upstream protocol itself
disagrees about per-turn versus cumulative end-turn usage). Until it is proved, a report
is a *snapshot*: display evidence, latest wins, never summed, never a call count.

A source's **policy** decides what its reports may become, and is chosen per
``(adapter, version)``, never inferred from the reports:

``snapshot_only``  (the default) every report is a snapshot.
``per_turn``       a report carrying a stable ``report_id`` and ``turn_id`` is that turn's
                   usage and becomes one ``acp_usage_delta``. Cumulative-kind reports of
                   the same source stay snapshots: one accounting source per window.
``cumulative``     reports carry running totals; deltas come from a persisted high-water
                   mark. Per-turn-kind reports of the source stay snapshots.

Cumulative rules (the high-water mark and the last sequence number are persisted with the
source's cursor, so a restart continues from them):

* no baseline yet → snapshot only; the report becomes the baseline (a delta would have to
  invent the consumption before it);
* the next report in sequence → delta = counters − high-water, each field;
* a repeated or older ``seq`` (replay, duplicate, out of order) → snapshot only, the
  cursor does not move;
* a jump in ``seq`` (a reconnect gap) or a change of adapter version → snapshot, the delta
  is unavailable (``gap`` / ``adapter_changed``) and the report is the new baseline;
* a counter that went down without a reported ``reset`` → snapshot, unavailable
  (``decrease``), the report is the new baseline. A negative delta is never clamped to zero;
* a reported ``reset`` starts a new epoch whose consumption is the report's own counters.

Records are keyed so that presenting the same report again — a replay, or a crash between
the append and the cursor — writes nothing a second time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from garuda.observability.ledger import DELTA, SNAPSHOT, Ledger

POLICIES = ("snapshot_only", "per_turn", "cumulative")
#: Adapter versions whose usage semantics have been proved: ``{(adapter, version): policy}``.
#: Empty on purpose — both captured adapters report ``usage_update`` as ``unknown``.
PROVED: dict[tuple[str, str], str] = {}
COUNTERS = ("input_tokens", "output_tokens", "cache_tokens", "cost_usd")


@dataclass
class UsageReport:
    """One adapter usage report, reduced to counters and identities."""

    source: str  # stable id of the reporting source, e.g. "<adapter>:<version>:<segment>"
    adapter: str
    adapter_version: str
    kind: str = "cumulative"  # "cumulative" | "per_turn"
    report_id: str | None = None
    seq: int | None = None
    turn_id: str | None = None
    counters: dict = field(default_factory=dict)
    context_used: int | None = None
    context_size: int | None = None
    reset: bool = False
    observed_at: float = 0.0
    session_id: str | None = None
    project_id: str | None = None
    harness: str | None = None
    model: str | None = None  # only when the adapter reported it; never the selected role's


def _digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


def policy_for(adapter: str, version: str) -> str:
    return PROVED.get((adapter, version), "snapshot_only")


class AcpUsageNormalizer:
    def __init__(self, ledger: Ledger, *, policy=policy_for):
        self._ledger = ledger
        self._policy = policy

    # --- records ---------------------------------------------------------------------

    def _base(self, report: UsageReport, kind: str, key: str) -> dict:
        record = {"kind": kind, "key": key, "time": report.observed_at,
                  "session_id": report.session_id, "project_id": report.project_id,
                  "harness": report.harness, "adapter": report.adapter,
                  "adapter_version": report.adapter_version, "source": None,
                  "model": report.model}
        return {k: v for k, v in record.items() if v is not None}

    def _snapshot(self, report: UsageReport, unavailable: str | None = None) -> dict:
        ident = report.report_id or _digest(report.source, report.seq, report.counters,
                                            report.context_used, report.context_size)
        record = self._base(report, SNAPSHOT, f"snap:{report.source}:{ident}")
        record.update(
            segment=report.source[:128], cumulative=report.kind == "cumulative",
            context_used=report.context_used, context_size=report.context_size,
            report_id=report.report_id, seq=report.seq, delta_unavailable=unavailable,
            **{k: report.counters.get(k) for k in COUNTERS})
        return {k: v for k, v in record.items() if v is not None}

    def _delta(self, report: UsageReport, counters: dict, epoch: int, window: str) -> dict:
        ident = report.turn_id if window == "turn" else f"{epoch}:{report.seq}"
        record = self._base(report, DELTA, f"delta:{report.source}:{ident}")
        record.update(segment=report.source[:128], epoch=epoch, window=window,
                      turn_id=report.turn_id, report_id=report.report_id, seq=report.seq,
                      **{k: counters.get(k) for k in COUNTERS})
        return {k: v for k, v in record.items() if v is not None}

    # --- ingest ----------------------------------------------------------------------

    def ingest(self, report: UsageReport) -> list[dict]:
        """Record what ``report`` may become; returns the records it stands for.

        The records are appended *before* the cursor moves. A crash between the two is
        finished by replaying the report: it yields the same keys, which the ledger
        refuses a second time, and then the cursor advances."""
        policy = self._policy(report.adapter, report.adapter_version)
        if policy not in POLICIES:
            policy = "snapshot_only"
        new_cursor = None
        cursor = None
        if policy == "per_turn" and report.kind == "per_turn" and report.report_id \
                and report.turn_id:
            records = [self._snapshot(report), self._delta(report, report.counters, 0, "turn")]
        elif policy == "cumulative" and report.kind == "cumulative":
            cursor = self._ledger.cursors().get(report.source)
            records, new_cursor = self._plan(report, cursor)
        else:
            records = [self._snapshot(report)]
        self._ledger.append_many(records)
        if new_cursor is not None:
            def publish(sources: dict) -> None:
                if sources.get(report.source) == cursor:  # nobody else advanced it meanwhile
                    sources[report.source] = new_cursor

            self._ledger.update_cursors(publish)
        return records

    def _plan(self, report: UsageReport, cursor: dict | None) -> tuple[list[dict], dict | None]:
        """``(records, the cursor to publish or None)`` for a cumulative report. Pure."""
        counters = {k: report.counters.get(k) for k in COUNTERS
                    if report.counters.get(k) is not None}
        if cursor is None or report.seq is None:
            return ([self._snapshot(report, "missing_baseline")],
                    self._cursor(report, counters, 0) if report.seq is not None else None)
        if report.adapter_version != cursor["adapter_version"]:
            return ([self._snapshot(report, "adapter_changed")],
                    self._cursor(report, counters, cursor["epoch"] + 1))
        if report.seq <= cursor["seq"]:
            return [self._snapshot(report)], None  # replay or out of order: the cursor stays
        if report.reset:
            epoch = cursor["epoch"] + 1
            return ([self._snapshot(report), self._delta(report, counters, epoch, "cumulative")],
                    self._cursor(report, counters, epoch))
        if report.seq != cursor["seq"] + 1:
            return ([self._snapshot(report, "gap")],
                    self._cursor(report, counters, cursor["epoch"] + 1))
        high = cursor["high_water"]
        if any(counters[k] < high.get(k, 0) for k in counters):  # only what it reported
            return ([self._snapshot(report, "decrease")],
                    self._cursor(report, counters, cursor["epoch"] + 1))
        # A counter the source stopped reporting is unknown, not zero.
        delta = {k: (round(counters[k] - high.get(k, 0), 8) if k in counters else None)
                 for k in COUNTERS}
        return ([self._snapshot(report), self._delta(report, delta, cursor["epoch"], "cumulative")],
                self._cursor(report, {**high, **counters}, cursor["epoch"]))

    @staticmethod
    def _cursor(report: UsageReport, counters: dict, epoch: int) -> dict:
        return {"seq": report.seq, "epoch": epoch, "high_water": counters,
                "adapter_version": report.adapter_version}
