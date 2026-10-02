"""ACP usage reports into the ledger, against hand-calculated expectations (#168, E.1).

Every expected number below was worked out on paper from the report sequences in the test
itself; none comes from running the normalizer and copying what it said.
"""

import pytest

from garuda.observability.acp_usage import AcpUsageNormalizer, UsageReport
from garuda.observability.ledger import Ledger, totals

SRC = "claude:0.85.0:seg1"


def report(seq, inp, out, *, version="0.85.0", reset=False, kind="cumulative", source=SRC,
           report_id=None, turn_id=None, cost=None, at=1_790_000_000.0, **extra):
    counters = {"input_tokens": inp, "output_tokens": out}
    if cost is not None:
        counters["cost_usd"] = cost
    return UsageReport(source=source, adapter="claude", adapter_version=version, kind=kind,
                       report_id=report_id, seq=seq, turn_id=turn_id, counters=counters,
                       reset=reset, observed_at=at, session_id="sess1", **extra)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "usage")


def proved(policy):
    return lambda adapter, version: policy


def deltas(ledger):
    return [r for r in ledger.records(kind="acp_usage_delta")]


def tally(ledger):
    summary = totals(list(ledger.records()))
    return summary["input_tokens"], summary["output_tokens"], summary["acp_delta_records"]


def test_nothing_proved_means_every_report_is_a_snapshot_and_nothing_is_summed(ledger):
    normalizer = AcpUsageNormalizer(ledger)  # the shipped table is empty: both adapters unproved
    for seq, (inp, out) in enumerate([(100, 10), (250, 30), (400, 50)], start=1):
        normalizer.ingest(report(seq, inp, out))
    assert deltas(ledger) == []
    assert len(list(ledger.records(kind="acp_usage_snapshot"))) == 3
    assert totals(list(ledger.records()))["coverage"] == "snapshots_excluded"
    assert tally(ledger) == (0, 0, 0)


def test_a_cumulative_source_yields_deltas_from_its_high_water_mark(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    n.ingest(report(1, 100, 10))  # the baseline: no consumption can be attributed yet
    assert deltas(ledger) == []
    n.ingest(report(2, 250, 30))  # 250-100 = 150 in, 30-10 = 20 out
    n.ingest(report(3, 400, 50))  # 150 in, 20 out
    assert [(d["input_tokens"], d["output_tokens"]) for d in deltas(ledger)] == [(150, 20), (150, 20)]
    assert tally(ledger) == (300, 40, 2)


def test_replayed_duplicate_and_out_of_order_reports_change_nothing(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    for args in [(1, 100, 10), (2, 250, 30), (3, 400, 50)]:
        n.ingest(report(*args))
    before = tally(ledger)  # 300 in, 40 out, two deltas
    n.ingest(report(3, 400, 50))  # replayed
    n.ingest(report(2, 250, 30))  # out of order
    n.ingest(report(1, 100, 10))
    assert tally(ledger) == before == (300, 40, 2)
    # and the cursor did not move: the next in-sequence report still continues from 400/50
    n.ingest(report(4, 460, 55))  # +60 in, +5 out
    assert tally(ledger) == (360, 45, 3)


def test_a_reported_reset_starts_a_new_epoch_from_zero(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    for args in [(1, 100, 10), (2, 250, 30)]:
        n.ingest(report(*args))  # one delta: 150 in, 20 out
    n.ingest(report(3, 60, 5, reset=True))  # new epoch: its counters are the consumption
    n.ingest(report(4, 160, 15))  # +100 in, +10 out within the new epoch
    assert tally(ledger) == (150 + 60 + 100, 20 + 5 + 10, 3)
    assert sorted({d["epoch"] for d in deltas(ledger)}) == [0, 1]


def test_an_unexplained_decrease_is_a_snapshot_not_a_negative_or_zero_delta(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    for args in [(1, 100, 10), (2, 250, 30)]:
        n.ingest(report(*args))
    n.ingest(report(3, 50, 2))  # the counters went down and nothing said why
    assert tally(ledger) == (150, 20, 1)  # no delta, and not a clamp to zero either
    (flagged,) = [r for r in ledger.records(kind="acp_usage_snapshot")
                  if r.get("delta_unavailable") == "decrease"]
    assert flagged["input_tokens"] == 50
    n.ingest(report(4, 80, 4))  # the decrease became the new baseline: +30 in, +2 out
    assert tally(ledger) == (180, 22, 2)


def test_a_reconnect_gap_is_unavailable_and_rebaselines(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    for args in [(1, 100, 10), (2, 250, 30)]:
        n.ingest(report(*args))
    n.ingest(report(5, 900, 90))  # 3 and 4 never arrived: what happened in between is unknown
    assert tally(ledger) == (150, 20, 1)
    n.ingest(report(6, 930, 94))  # +30, +4 from the new baseline
    assert tally(ledger) == (180, 24, 2)
    assert any(r.get("delta_unavailable") == "gap" for r in ledger.records(kind="acp_usage_snapshot"))


def test_an_adapter_change_rebaselines(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    for args in [(1, 100, 10), (2, 250, 30)]:
        n.ingest(report(*args))
    n.ingest(report(3, 400, 50, version="0.86.0"))  # another adapter version: counters not comparable
    assert tally(ledger) == (150, 20, 1)
    n.ingest(report(4, 410, 52, version="0.86.0"))  # +10, +2
    assert tally(ledger) == (160, 22, 2)


def test_a_missing_baseline_never_invents_the_consumption_before_it(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    n.ingest(report(7, 5000, 400))  # the first we ever heard of this source
    assert tally(ledger) == (0, 0, 0)
    (snap,) = ledger.records(kind="acp_usage_snapshot")
    assert snap["delta_unavailable"] == "missing_baseline" and snap["input_tokens"] == 5000


def test_a_counter_the_source_stops_reporting_is_unknown_not_zero(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    n.ingest(report(1, 100, 10, cost=0.50))
    n.ingest(report(2, 200, 20))  # no cost this time
    (delta,) = deltas(ledger)
    assert (delta["input_tokens"], delta["output_tokens"]) == (100, 10)
    assert "cost_usd" not in delta  # unknown, not a delta of zero
    assert totals([delta])["cost_unknown_records"] == 1


def test_a_per_turn_source_counts_each_turn_once_and_ignores_cumulative_reports(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("per_turn"))
    n.ingest(report(None, 80, 8, kind="per_turn", report_id="r1", turn_id="t1"))
    n.ingest(report(None, 80, 8, kind="per_turn", report_id="r1", turn_id="t1"))  # replayed
    n.ingest(report(None, 120, 12, kind="per_turn", report_id="r2", turn_id="t2"))
    # the same consumption also reported as running totals: an alternative, never an addend
    n.ingest(report(1, 200, 20, kind="cumulative"))
    assert tally(ledger) == (80 + 120, 8 + 12, 2)
    # a per-turn report with no stable id can only be a snapshot
    n.ingest(report(None, 999, 99, kind="per_turn"))
    assert tally(ledger) == (200, 20, 2)


def test_a_crash_between_the_append_and_the_cursor_neither_loses_nor_doubles_a_delta(
        ledger, monkeypatch):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    n.ingest(report(1, 100, 10))
    real = ledger.update_cursors

    def crash(mutate):
        raise KeyboardInterrupt  # the process dies after the records were appended

    monkeypatch.setattr(ledger, "update_cursors", crash)
    with pytest.raises(KeyboardInterrupt):
        n.ingest(report(2, 250, 30))
    monkeypatch.setattr(ledger, "update_cursors", real)
    assert tally(ledger) == (150, 20, 1)  # the delta made it to disk
    AcpUsageNormalizer(Ledger(ledger.root), policy=proved("cumulative")).ingest(report(2, 250, 30))
    assert tally(ledger) == (150, 20, 1)  # the replay wrote nothing a second time
    AcpUsageNormalizer(Ledger(ledger.root), policy=proved("cumulative")).ingest(report(3, 300, 35))
    assert tally(ledger) == (200, 25, 2)  # and the cursor did advance: +50, +5


def test_a_crash_before_the_append_is_finished_by_the_replay(ledger, monkeypatch):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    n.ingest(report(1, 100, 10))
    real = ledger.append_many
    monkeypatch.setattr(ledger, "append_many", lambda records: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        n.ingest(report(2, 250, 30))
    monkeypatch.setattr(ledger, "append_many", real)
    assert tally(ledger) == (0, 0, 0)
    n.ingest(report(2, 250, 30))
    assert tally(ledger) == (150, 20, 1)


def test_sources_do_not_share_cursors(ledger):
    n = AcpUsageNormalizer(ledger, policy=proved("cumulative"))
    for source in ("claude:0.85.0:a", "claude:0.85.0:b"):
        n.ingest(report(1, 100, 10, source=source))
        n.ingest(report(2, 130, 14, source=source))  # +30, +4 in each
    assert tally(ledger) == (60, 8, 2)


def test_a_snapshot_never_assigns_an_unreported_model(ledger):
    AcpUsageNormalizer(ledger).ingest(report(1, 100, 10))
    (record,) = ledger.records()
    assert "model" not in record  # the adapter did not name one; the selected role's is not borrowed
