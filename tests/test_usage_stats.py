"""Usage statistics and exports against hand-calculated golden totals (#169, F.3).

NOW is 2026-09-21T12:00:00Z. Every expected number below was worked out on paper from the
records listed in LEDGER; none was produced by running the code under test.
"""

import csv
import io
import json

import pytest

from garuda.core import usage_stats
from garuda.observability.ledger import Ledger

NOW = 1789992000.0
H, D = 3600.0, 86400.0


def native(key, ago, purpose, model, inp, out, total, cost, dur, role, project, session):
    return {"kind": "native_model_call", "key": key, "time": NOW - ago, "session_id": session,
            "call_purpose": purpose, "harness": "native", "model": model, "input_tokens": inp,
            "output_tokens": out, "total_tokens": total, "cost_usd": cost, "duration_ms": dur,
            "role": role, "project_id": project}


LEDGER = [
    native("r1", 1 * H, "controller", "A", 100, 20, 120, 0.10, 1000, "coder", "p1", "s1"),
    native("r2", 2 * H, "controller", "A", 200, 30, 230, 0.20, 3000, "coder", "p1", "s1"),
    native("r3", 3 * H, "collector", "B", 50, 10, 60, None, 500, "coder", "p1", "s1"),
    native("r4", D + 1, "summarizer", "A", 80, 20, 100, 0.05, 2000, "reviewer", "p2", "s2"),
    native("r5", 3 * D, "controller", "B", 300, 50, 350, 0.30, 4000, "reviewer", "p2", "s2"),
    native("r6", 10 * D, "controller", "A", 10, 5, 15, None, 100, None, None, "s3"),
    native("r7", 40 * D, "controller", "A", 999, 99, 1098, 9.0, 9, "coder", "p1", "s9"),
    {"kind": "acp_usage_delta", "key": "a1", "time": NOW - 1800, "session_id": "s4",
     "harness": "claude", "input_tokens": 400, "output_tokens": 40, "role": "reviewer",
     "project_id": "p2", "adapter": "claude", "adapter_version": "0.85.0"},
    {"kind": "acp_usage_snapshot", "key": "a2", "time": NOW - 1000, "session_id": "s4",
     "harness": "claude", "input_tokens": 9000, "context_used": 5000, "context_size": 10000},
]


@pytest.fixture
def records(tmp_path):
    ledger = Ledger(tmp_path / "usage")
    ledger.append_many(LEDGER)
    ledger.append_many(LEDGER)  # presented twice: a replay must not change a single total
    return list(ledger.records())


def test_24h_totals(records):
    s = usage_stats.stats(records, "24h", NOW)
    # r1, r2, r3 native; a1 an ACP turn; a2 a snapshot (left out)
    assert s["measures"]["native_calls"] == 3 and s["measures"]["acp_turns"] == 1
    assert s["measures"]["input_tokens"] == 100 + 200 + 50 + 400 == 750
    assert s["measures"]["output_tokens"] == 20 + 30 + 10 + 40 == 100
    assert s["measures"]["total_tokens"] == 120 + 230 + 60 + 440 == 850
    assert s["measures"]["cost"] == {"state": "partial", "known_usd": 0.30, "unpriced_records": 2}
    assert s["excluded"]["snapshots"] == 1
    assert s["median"] == {"tokens_per_native_call": 120, "native_call_duration_ms": 1000}
    share = s["work_type_share"]
    assert share["unit"] == "native calls" and share["denominator"] == 3
    assert share["excluded_acp_turns"] == 1
    assert [(r["work_type"], r["native_calls"], r["share"]) for r in share["rows"]] == [
        ("collector", 1, 0.333333), ("controller", 2, 0.666667)]
    assert s["coverage"] == {"native_calls_with_unknown_cost": 1, "native_calls": 3}


def test_harness_model_table_24h(records):
    rows = {(r["harness"], r["model"]): r for r in usage_stats.stats(records, "24h", NOW)["harness_model"]}
    assert set(rows) == {("native", "A"), ("native", "B"), ("claude", "not reported")}
    a = rows[("native", "A")]
    assert (a["native_calls"], a["acp_turns"], a["input_tokens"], a["total_tokens"]) == (2, 0, 300, 350)
    assert a["cost"] == {"state": "known", "known_usd": 0.30, "unpriced_records": 0}
    b = rows[("native", "B")]
    assert (b["native_calls"], b["total_tokens"]) == (1, 60)
    assert b["cost"] == {"state": "unknown", "known_usd": None, "unpriced_records": 1}  # not 0
    c = rows[("claude", "not reported")]
    assert (c["native_calls"], c["acp_turns"], c["total_tokens"]) == (0, 1, 440)  # turns, not calls


def test_7d_totals_tables_and_median(records):
    s = usage_stats.stats(records, "7d", NOW)
    assert s["measures"]["native_calls"] == 5 and s["measures"]["acp_turns"] == 1
    assert s["measures"]["input_tokens"] == 100 + 200 + 50 + 80 + 300 + 400 == 1130
    assert s["measures"]["output_tokens"] == 20 + 30 + 10 + 20 + 50 + 40 == 170
    assert s["measures"]["total_tokens"] == 120 + 230 + 60 + 100 + 350 + 440 == 1300
    assert s["measures"]["cost"] == {"state": "partial", "known_usd": 0.65, "unpriced_records": 2}
    assert s["median"] == {"tokens_per_native_call": 120, "native_call_duration_ms": 2000}
    assert [(r["work_type"], r["native_calls"], r["share"]) for r in s["work_type_share"]["rows"]] == [
        ("collector", 1, 0.2), ("controller", 3, 0.6), ("summarizer", 1, 0.2)]
    roles = {r["role"]: r for r in s["by_role"]}
    assert (roles["coder"]["native_calls"], roles["coder"]["total_tokens"]) == (3, 410)
    assert roles["coder"]["cost"] == {"state": "partial", "known_usd": 0.30, "unpriced_records": 1}
    assert (roles["reviewer"]["native_calls"], roles["reviewer"]["acp_turns"],
            roles["reviewer"]["total_tokens"]) == (2, 1, 100 + 350 + 440)
    assert roles["reviewer"]["cost"] == {"state": "partial", "known_usd": 0.35, "unpriced_records": 1}
    projects = {r["project_id"]: r for r in s["by_project"]}
    assert set(projects) == {"p1", "p2"} and projects["p1"]["total_tokens"] == 410
    assert [(d["date"], d["native_calls"], d["acp_turns"], d["total_tokens"]) for d in s["daily"]] == [
        ("2026-09-18", 1, 0, 350), ("2026-09-20", 1, 0, 100), ("2026-09-21", 3, 1, 410 + 440)]


def test_30d_median_of_an_even_count_and_the_record_outside_every_range(records):
    s = usage_stats.stats(records, "30d", NOW)
    assert s["measures"]["native_calls"] == 6  # r7 (40 days ago) is in no range
    assert s["measures"]["total_tokens"] == 1300 + 15 == 1315
    assert s["median"]["tokens_per_native_call"] == (100 + 120) / 2  # 15,60,100,120,230,350
    assert {r["role"] for r in s["by_role"]} == {"coder", "reviewer", "unknown"}


def test_a_range_of_only_unpriced_calls_reads_unknown_not_zero(tmp_path):
    ledger = Ledger(tmp_path / "usage")
    ledger.append_many([native("u1", H, "controller", "A", 1, 1, 2, None, 5, "r", "p", "s"),
                        native("u2", 2 * H, "controller", "A", 1, 1, 2, None, 5, "r", "p", "s")])
    s = usage_stats.stats(list(ledger.records()), "24h", NOW)
    assert s["measures"]["cost"] == {"state": "unknown", "known_usd": None, "unpriced_records": 2}
    empty = usage_stats.stats([], "24h", NOW)
    assert empty["measures"]["cost"]["state"] == "none" and empty["median"]["tokens_per_native_call"] is None
    assert empty["work_type_share"]["rows"] == [] and empty["work_type_share"]["denominator"] == 0


def test_acp_turns_and_snapshots_never_share_a_denominator_with_native_calls(records):
    share = usage_stats.stats(records, "24h", NOW)["work_type_share"]
    assert sum(r["native_calls"] for r in share["rows"]) == share["denominator"] == 3
    assert abs(sum(r["share"] for r in share["rows"]) - 1.0) < 1e-5


def test_the_boundary_is_inclusive_at_the_start_and_exclusive_at_the_end(tmp_path):
    ledger = Ledger(tmp_path / "usage")
    ledger.append_many([
        native("at-start", D, "controller", "A", 1, 1, 2, 0.1, 1, "r", "p", "s"),
        native("before", D + 1, "controller", "A", 1, 1, 2, 0.1, 1, "r", "p", "s"),
        native("at-end", 0, "controller", "A", 1, 1, 2, 0.1, 1, "r", "p", "s")])
    s = usage_stats.stats(list(ledger.records()), "24h", NOW)
    assert s["measures"]["native_calls"] == 1  # only at-start: [now-24h, now)


GOLDEN_CSV_24H = (
    "time,kind,session_id,project_id,role,origin,call_purpose,harness,model,calls,input_tokens,"
    "output_tokens,total_tokens,cache_tokens,cost_usd,duration_ms\n"
    "1789981200.0,native_model_call,s1,p1,coder,,collector,native,B,,50,10,60,,,500\n"
    "1789984800.0,native_model_call,s1,p1,coder,,controller,native,A,,200,30,230,,0.2,3000\n"
    "1789988400.0,native_model_call,s1,p1,coder,,controller,native,A,,100,20,120,,0.1,1000\n"
    "1789990200.0,acp_usage_delta,s4,p2,reviewer,,,claude,,,400,40,,,,\n"
    "1789991000.0,acp_usage_snapshot,s4,,,,,claude,,,9000,,,,,\n"
)


def test_the_csv_export_is_exactly_the_schema_fields_in_order(records):
    rows = usage_stats.export_rows(records, "24h", NOW)
    assert usage_stats.export_csv(rows) == GOLDEN_CSV_24H
    parsed = list(csv.DictReader(io.StringIO(usage_stats.export_csv(rows))))
    assert list(parsed[0]) == list(usage_stats.EXPORT_FIELDS)


def test_the_json_export_has_the_same_rows_and_nothing_else(records):
    rows = usage_stats.export_rows(records, "24h", NOW)
    document = json.loads(usage_stats.export_json(rows))
    assert document["fields"] == list(usage_stats.EXPORT_FIELDS)
    assert len(document["rows"]) == 5 and all(set(r) == set(usage_stats.EXPORT_FIELDS)
                                              for r in document["rows"])
    text = usage_stats.export_json(rows)
    for forbidden in ("key", "account", "prompt", "reset_at", "segment"):
        assert f'"{forbidden}"' not in text


def test_a_bad_range_is_refused():
    with pytest.raises(ValueError):
        usage_stats.stats([], "90d", NOW)


def test_routes_serve_stats_and_downloads(tmp_path, monkeypatch):
    import time

    from garuda.core.sessions import SessionStore
    from garuda.interfaces.web.routes import DashboardContext
    from tests.test_web_routes import PORT, TOKEN, body, call

    ledger = Ledger()
    record = native("live", 0, "controller", "A", 10, 5, 15, 0.1, 5, "r", "p", "s")
    record["time"] = time.time() - 60  # the routes read against the real clock
    ledger.append(record)
    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(tmp_path / "s"))
    assert body(call(ctx, "/api/usage", query="range=24h"))["measures"]["native_calls"] == 1
    assert call(ctx, "/api/usage", query="range=1y").status == 400
    csv_response = call(ctx, "/api/usage/export", query="range=24h&format=csv")
    assert csv_response.status == 200 and csv_response.content_type.startswith("text/csv")
    assert "attachment" in csv_response.headers["Content-Disposition"]
    assert csv_response.body.decode().splitlines()[0].startswith("time,kind,session_id")
    assert call(ctx, "/api/usage/export", query="range=24h&format=xml").status == 400
