"""The idempotent usage ledger and native accounting (#168, plan task E.1)."""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.metrics import aggregate_model_metrics
from garuda.core.sessions import SessionStore
from garuda.model import accounting
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.observability import usage
from garuda.observability.ledger import Ledger, LedgerRefused, totals
from garuda.types import Message, Role

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_790_000_000.0  # 2026-09-21 in UTC


def call(key, **fields):
    return {"kind": "native_model_call", "key": key, "time": NOW, "session_id": "s1",
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15, **fields}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "usage")


# --- the writer ---------------------------------------------------------------------------


@pytest.mark.parametrize("record", [
    call("k", task="fix the login bug"),  # task text has no field
    call("k", prompt="..."), call("k", output="..."), call("k", path="/etc/passwd"),
    call("k", model="/etc/passwd"), call("k", model="a b"), call("k", model="../x"),
    call("k", account_name="me@example.com"), call("k", input_tokens=-1),
    call("k", cost_usd="free"), call("k", input_tokens=True),
    {**call("k"), "kind": "mystery"}, {"kind": "native_model_call", "key": "k"},
])
def test_the_writer_refuses_anything_outside_the_schema(ledger, record):
    with pytest.raises(LedgerRefused):
        ledger.append(record)
    assert ledger.files() == []


def test_the_same_key_is_written_once_however_often_it_is_presented(ledger):
    assert ledger.append(call("native:s1:0")) is True
    assert ledger.append(call("native:s1:0")) is False
    assert ledger.append_many([call("native:s1:0"), call("native:s1:1")]) == 1
    assert len(list(ledger.records())) == 2
    # a different instance (another process, after a restart) sees the same keys
    assert Ledger(ledger.root).append(call("native:s1:1")) is False


def test_records_land_in_the_month_they_happened(ledger):
    september, october = 1_789_000_000.0, 1_791_000_000.0
    ledger.append(call("a", time=september))
    ledger.append(call("b", time=october))
    assert [p.name for p in ledger.files()] == ["2026-09.jsonl", "2026-10.jsonl"]


def test_a_torn_last_line_is_fenced_off_and_never_read_as_a_record(ledger):
    ledger.append(call("a"))
    path = ledger.files()[0]
    with path.open("ab") as handle:
        handle.write(b'{"kind": "native_model_call", "key": "tor')  # a crash mid-write
    assert ledger.append(call("b")) is True
    keys = {r["key"] for r in Ledger(ledger.root).records()}
    assert keys == {"a", "b"} and ledger.malformed_lines() == 1


def test_storage_is_owner_only(ledger):
    ledger.append(call("a"))
    assert oct(ledger.root.stat().st_mode & 0o777) == "0o700"
    assert oct(ledger.files()[0].stat().st_mode & 0o777) == "0o600"


WRITER = """
import sys
from garuda.observability.ledger import Ledger
root, name, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
ledger = Ledger(root)
for i in range(count):
    ledger.append({"kind": "native_model_call", "key": f"{name}:{i}", "time": 1790000000.0,
                   "session_id": name, "input_tokens": 1, "output_tokens": 1})
    ledger.append({"kind": "native_model_call", "key": f"shared:{i}", "time": 1790000000.0,
                   "session_id": "shared", "input_tokens": 1, "output_tokens": 1})
"""


def test_writers_in_separate_processes_never_interleave_or_double_count(tmp_path):
    root = tmp_path / "usage"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    procs = [subprocess.Popen([sys.executable, "-c", textwrap.dedent(WRITER), str(root),
                               f"w{i}", "40"], env=env, stderr=subprocess.PIPE, text=True)
             for i in range(4)]
    for proc in procs:
        assert proc.wait(timeout=120) == 0, proc.stderr.read()
    ledger = Ledger(root)
    lines = ledger.files()[0].read_text().splitlines()
    assert all(json.loads(line) for line in lines)  # every line whole
    assert len(lines) == 4 * 40 + 40  # four private streams and one shared stream written once
    assert ledger.malformed_lines() == 0


def test_pruning_drops_old_months_only_and_session_cleanup_never_touches_it(ledger, tmp_path):
    old, recent = 1_700_000_000.0, 1_790_000_000.0  # Nov 2023, Sep 2026
    ledger.append(call("old", time=old))
    ledger.append(call("recent", time=recent))
    assert ledger.prune(keep_months=13, now=recent) == ["2023-11.jsonl"]
    assert [r["key"] for r in ledger.records()] == ["recent"]
    before = totals(list(ledger.records()))
    store = SessionStore(tmp_path / "sessions")
    store.begin("s1", task="t", model="m", agent="a", workspace=str(tmp_path))
    import shutil
    shutil.rmtree(store.session_dir("s1"))  # what pruning sessions does
    assert totals(list(ledger.records())) == before


# --- native accounting --------------------------------------------------------------------------


def _response(content="ok", prompt=100, completion=20, cost=None):
    usage_ = {"prompt_tokens": prompt, "completion_tokens": completion,
              "total_tokens": prompt + completion}
    if cost is not None:
        usage_["cost_usd"] = cost
    return ModelResponse(content=content, tool_calls=[], usage=usage_)


def _by_purpose(ledger_records):
    out = {}
    for record in ledger_records:
        bucket = out.setdefault(record.get("call_purpose") or "unknown",
                                {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                 "total_tokens": 0, "cost_unknown_calls": 0, "cost_usd": 0.0})
        bucket["calls"] += record.get("calls", 1)
        bucket["prompt_tokens"] += record.get("input_tokens") or 0
        bucket["completion_tokens"] += record.get("output_tokens") or 0
        bucket["total_tokens"] += record.get("total_tokens") or 0
        if record.get("cost_usd") is None:
            bucket["cost_unknown_calls"] += 1
        else:
            bucket["cost_usd"] = round(bucket["cost_usd"] + record["cost_usd"], 8)
    return out


async def test_auxiliary_calls_are_tagged_and_the_ledger_equals_the_session_metrics(tmp_path):
    from garuda.context.summarizer import summarize_incremental, summarize_three_step

    store = SessionStore(tmp_path / "sessions")
    events = EventStore("11111111-1111-1111-1111-111111111111")
    store.begin(events.session_id, task="t", model="m", agent="a", workspace=str(tmp_path))
    ledger = Ledger(tmp_path / "usage")
    usage.attach(events, store, ledger=ledger)
    messages = [Message(role=Role.USER, content="hello"), Message(role=Role.ASSISTANT, content="hi")]

    model = ScriptModel([_response(cost=0.001), _response(), _response(cost=0.002),
                         _response(cost=0.003)])
    token = accounting.bind(events)
    try:
        await summarize_three_step(model, messages, "task")  # three calls
        await summarize_incremental(model, "", messages, "task")  # one more
    finally:
        accounting.reset(token)
    events.append(EventType.MODEL_RESPONSE, {
        "turn": 1, "content": "x", "tool_calls": [], "usage": {
            "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        "model_binding_role": "reasoning", "call_purpose": "controller", "duration_ms": 5.0})

    metrics = aggregate_model_metrics(events.get_all())["by_call_purpose"]
    assert metrics["summarizer"]["calls"] == 4  # the backlog item: they were invisible
    assert _by_purpose(ledger.records()) == {
        k: {kk: vv for kk, vv in v.items() if kk != "duration_ms"} for k, v in metrics.items()}
    assert {r["origin"] for r in ledger.records()} == {"run"}


async def test_a_full_run_is_recorded_once_per_call(tmp_path):
    from garuda.core.loop import DefaultAgent
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall
    from garuda.workspace.local import LocalEnvironment

    store = SessionStore(tmp_path / "sessions")
    events = EventStore("22222222-2222-2222-2222-222222222222")
    store.begin(events.session_id, task="t", model="m", agent="a", workspace=str(tmp_path))
    ledger = Ledger(tmp_path / "usage")
    usage.attach(events, store, ledger=ledger)
    done = ModelResponse(content=None, tool_calls=[ToolCall(
        id="d", name="task_complete",
        arguments={"summary": "A fully detailed completion summary of the work done."})],
        usage={"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48, "cost_usd": 0.01})
    result = await DefaultAgent().run(
        task="t", model=ScriptModel([done]), env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools_for_names(["task_complete"]), config=AgentConfig(enable_verifier=False,
                                                                     max_turns=3),
        events=events)
    assert result.success
    records = list(ledger.records())
    assert len(records) == 1 and records[0]["input_tokens"] == 40 and records[0]["cost_usd"] == 0.01
    metrics = aggregate_model_metrics(events.get_all())
    assert totals(records)["native_calls"] == sum(
        v["calls"] for v in metrics["by_call_purpose"].values())
    assert totals(records)["total_tokens"] == sum(
        v["total_tokens"] for v in metrics["by_call_purpose"].values())


def test_unknown_cost_is_never_zero_filled(ledger):
    ledger.append(call("a", cost_usd=0.5))
    ledger.append(call("b"))  # an unpriced call
    summary = totals(list(ledger.records()))
    assert summary["cost_usd"] == 0.5 and summary["cost_unknown_records"] == 1


def test_a_subagents_calls_are_recorded_under_the_root_task(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    parent = EventStore("33333333-3333-3333-3333-333333333333")
    child = EventStore("44444444-4444-4444-4444-444444444444")
    for s in (parent, child):
        store.begin(s.session_id, task="t", model="m", agent="a", workspace=str(tmp_path))
    ledger = Ledger(tmp_path / "usage")
    usage.attach(parent, store, ledger=ledger)
    from garuda.core.subagent import persist_child_events

    persist_child_events(parent, child)
    for s in (parent, child):
        s.append(EventType.MODEL_RESPONSE, {"usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                       "total_tokens": 2}, "call_purpose": "controller"})
    by_session = {r["session_id"]: r for r in ledger.records()}
    assert by_session[parent.session_id]["origin"] == "run"
    assert by_session[child.session_id]["origin"] == "subagent"
    assert {r["root_id"] for r in by_session.values()} == {parent.session_id}


def test_reconcile_completes_a_crashed_sessions_accounting_exactly_once(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    sid = "55555555-5555-5555-5555-555555555555"
    store.begin(sid, task="t", model="m", agent="a", workspace=str(tmp_path))
    events = EventStore(sid, persist_path=store.events_path(sid))  # no ledger observer: it crashed
    for i in range(3):
        events.append(EventType.MODEL_RESPONSE, {"usage": {"prompt_tokens": 10 + i,
                                                           "completion_tokens": 1, "total_tokens": 11 + i},
                                                 "call_purpose": "controller"})
    ledger = Ledger(tmp_path / "usage")
    assert usage.reconcile(store, sid, ledger=ledger) == 3
    assert usage.reconcile(store, sid, ledger=ledger) == 0
    assert totals(list(ledger.records()))["input_tokens"] == 10 + 11 + 12


def test_a_fallback_is_recorded_once_and_is_not_a_call(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    sid = "66666666-6666-6666-6666-666666666666"
    store.begin(sid, task="t", model="m", agent="a", workspace=str(tmp_path))
    store.update_meta(sid, {"role": {"role": "reviewer", "fallback": {
        "primary": {"harness": "claude"}, "taken": {"harness": "codex", "index": 1},
        "skipped": [{"harness": "claude", "reason": "harness.logged_out"}]}}})
    ledger = Ledger(tmp_path / "usage")
    assert usage.record_fallback_start(store, sid, ledger=ledger) is True
    assert usage.record_fallback_start(store, sid, ledger=ledger) is False
    (record,) = ledger.records()
    assert (record["kind"], record["from_harness"], record["to_harness"], record["reason"]) == (
        "fallback_start", "claude", "codex", "harness.logged_out")
    assert totals([record])["native_calls"] == 0 and totals([record])["fallback_starts"] == 1
    # a session that started on its primary records nothing
    store.update_meta(sid, {"role": {"role": "reviewer", "fallback": {"taken": {"index": 0}}}})
    assert usage.record_fallback_start(store, "66666666-6666-6666-6666-666666666666",
                                       ledger=Ledger(tmp_path / "other")) is False


def test_the_clock_is_not_a_source_of_records(ledger):
    # a record without a time is refused rather than stamped with "now"
    with pytest.raises(LedgerRefused):
        ledger.append({"kind": "native_model_call", "key": "x"})
    assert time.time() > NOW
