"""The conversation read model: models used, lanes and links (#169, plan task F.1)."""

import json

import pytest

from garuda.core import conversation
from garuda.core.events import EventStore, EventType
from garuda.core.sessions import SessionStore
from garuda.observability import usage
from garuda.observability.acp_usage import AcpUsageNormalizer, UsageReport
from garuda.observability.ledger import Ledger, totals

NOW = 1_790_000_000.0


def ids(*names):
    return {n: f"00000000-0000-0000-0000-00000000000{i}" for i, n in enumerate(names, start=1)}


@pytest.fixture
def world(tmp_path):
    return SessionStore(tmp_path / "sessions"), Ledger(tmp_path / "usage"), tmp_path


def begin(store, sid, tmp_path, **meta):
    store.begin(sid, task=meta.pop("task", "t"), model=meta.pop("model", "openrouter/x/y"),
                agent="build", workspace=str(tmp_path))
    if meta:
        store.update_meta(sid, meta)


def call(ledger, session, purpose, *, tokens=10, cost=0.01, model="openrouter/x/y", n=0, **extra):
    ledger.append({"kind": "native_model_call", "key": f"{session}:{purpose}:{n}", "time": NOW,
                   "session_id": session, "call_purpose": purpose, "model": model,
                   "input_tokens": tokens, "output_tokens": 1, "total_tokens": tokens + 1,
                   "cost_usd": cost, **extra})


def test_one_row_per_work_type_and_totals_equal_the_ledger(world):
    store, ledger, tmp = world
    sid = ids("a")["a"]
    begin(store, sid, tmp)
    for n in range(3):
        call(ledger, sid, "controller", n=n)
    call(ledger, sid, "collector", model="cheap/model", tokens=100, cost=0.002)
    call(ledger, sid, "classifier", model="cheap/model", tokens=7, cost=None)
    for n in range(2):
        call(ledger, sid, "summarizer", n=n, tokens=50)

    used = conversation.conversation(store, sid, ledger=ledger)["models_used"]
    assert used["source"] == "ledger" and used["label"] is None
    rows = {r["work_type"]: r for r in used["rows"]}
    assert sorted(rows) == ["classifier", "collector", "controller", "summarizer"]
    assert rows["controller"]["calls"] == 3 and rows["controller"]["total_tokens"] == 33
    assert rows["summarizer"]["calls"] == 2 and rows["summarizer"]["total_tokens"] == 102
    assert rows["classifier"]["cost_unknown"] == 1 and rows["classifier"]["cost_usd"] == 0.0
    assert rows["collector"]["model"] == "cheap/model"
    ledger_totals = totals(list(ledger.records()))
    assert sum(r["calls"] for r in rows.values()) == ledger_totals["native_calls"] == 7
    assert sum(r["total_tokens"] for r in rows.values()) == ledger_totals["total_tokens"]
    assert used["totals"]["native_calls"] == 7


def test_a_flows_step_roles_are_its_work_types(world):
    from garuda.flows import engine

    store, ledger, tmp = world
    n = ids("flow", "code", "review")
    for sid in n.values():
        begin(store, sid, tmp)
    store.update_meta(n["flow"], {"kind": "flow", "flow": {"name": "f", "steps": ["code", "review"]}})
    directory = engine.flow_dir(store, n["flow"])
    (directory / "receipts").mkdir(parents=True, exist_ok=True)
    for index, (step, role) in enumerate((("code", "coder"), ("review", "reviewer"))):
        engine._write_once(directory / "receipts" / f"{step}-1.json", {
            "index": index, "step": step, "role": role, "attempt": 1, "session_id": n[step],
            "success": True, "inputs": [], "outputs": [], "status": "done"})
    call(ledger, n["code"], "controller", model="big/model")
    call(ledger, n["code"], "controller", model="big/model", n=1)
    call(ledger, n["review"], "controller", model="other/model")

    rows = {(r["work_type"], r["model"]): r["calls"] for r in conversation.conversation(
        store, n["flow"], ledger=ledger)["models_used"]["rows"]}
    assert rows == {("flow step: coder", "big/model"): 2, ("flow step: reviewer", "other/model"): 1}


def test_an_acp_session_keeps_selected_reported_snapshots_and_turns_apart(world):
    store, ledger, tmp = world
    sid = ids("acp")["acp"]
    begin(store, sid, tmp, role={"role": "reviewer", "runtime_id": "claude",
                                 "model_id": "claude-sonnet-x"})
    normalizer = AcpUsageNormalizer(ledger, policy=lambda a, v: "per_turn")
    for turn in ("t1", "t2"):
        normalizer.ingest(UsageReport(
            source="claude:0.85.0:s", adapter="claude", adapter_version="0.85.0", kind="per_turn",
            report_id=f"r-{turn}", turn_id=turn, counters={"input_tokens": 100, "output_tokens": 10},
            observed_at=NOW, session_id=sid, harness="claude", context_used=900, context_size=2000))

    c = conversation.conversation(store, sid, ledger=ledger)
    assert c["selected"]["model_id"] == "claude-sonnet-x"  # what was asked for
    (row,) = c["models_used"]["rows"]
    assert row["model"] == "not reported" and row["selected_model"] == "claude-sonnet-x"
    assert row["model"] != row["selected_model"]  # never borrowed from the selection
    assert (row["calls"], row["turns"], row["total_tokens"], row["input_tokens"]) == (0, 2, 220, 200)  # 2 x (100 in + 10 out)
    assert c["models_used"]["snapshots"] and c["models_used"]["snapshots"][0]["context_used"] == 900
    # the snapshots add nothing to the rows or the totals
    assert c["models_used"]["totals"]["snapshots_excluded"] == 2


def test_a_session_from_before_the_ledger_falls_back_to_its_metrics_and_says_so(world):
    store, ledger, tmp = world
    sid = ids("old")["old"]
    begin(store, sid, tmp, model="legacy/model")
    events = EventStore(sid, persist_path=store.events_path(sid))
    events.append(EventType.MODEL_RESPONSE, {"turn": 1, "call_purpose": "controller", "usage": {
        "prompt_tokens": 40, "completion_tokens": 2, "total_tokens": 42}})
    events.append(EventType.MODEL_RESPONSE, {"call_purpose": "summarizer", "usage": {
        "prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11, "cost_usd": 0.5}})
    used = conversation.conversation(store, sid, ledger=ledger)["models_used"]
    assert used["source"] == "session_metrics" and used["label"] == "from session metrics"
    rows = {r["work_type"]: r for r in used["rows"]}
    assert rows["controller"]["total_tokens"] == 42 and rows["controller"]["cost_unknown"] == 1
    assert rows["summarizer"]["cost_usd"] == 0.5 and rows["summarizer"]["model"] == "legacy/model"
    # once the ledger knows the session, the ledger wins
    usage.reconcile(store, sid, ledger=ledger)
    assert conversation.conversation(store, sid, ledger=ledger)["models_used"]["source"] == "ledger"


def test_tag_and_resume_links_resolve_in_both_directions(world):
    store, ledger, tmp = world
    n = ids("a", "b", "c")
    for sid in n.values():
        begin(store, sid, tmp)
    # b tagged a (the shape tags.record_links writes on both sides)
    store.mutate_meta(n["b"], lambda m: {"context_from": [{"session_id": n["a"], "name": "alpha",
                                                           "provenance": "name", "cross_project": False}]})
    store.mutate_meta(n["a"], lambda m: {"context_to": [{"session_id": n["b"], "at": "t"}]})
    store.update_meta(n["c"], {"resumed_from": n["b"]})

    a, b, c = (conversation.conversation(store, n[k], ledger=ledger)["links"] for k in "abc")
    assert [s["session_id"] for s in b["tagged"]] == [n["a"]]
    assert [s["session_id"] for s in a["tagged_by"]] == [n["b"]]
    assert [s["session_id"] for s in c["resumed_from"]] == [n["b"]]
    assert [s["session_id"] for s in b["resumed_into"]] == [n["c"]]


def test_lanes_and_the_fallback_story_ride_one_session(world):
    store, ledger, tmp = world
    sid = ids("x")["x"]
    begin(store, sid, tmp, role={"role": "reviewer", "runtime_id": "codex", "model_id": "m1",
                                 "fallback": {"primary": {"harness": "claude"},
                                              "taken": {"harness": "codex", "index": 1},
                                              "skipped": [{"harness": "claude",
                                                           "reason": "harness.logged_out"}]}})
    store.ensure_unified(sid)
    c = conversation.conversation(store, sid, ledger=ledger)
    assert c["selected"]["fallback"]["taken"]["harness"] == "codex"
    assert isinstance(c["lanes"], list)
    json.dumps(c)  # everything is plain data


def test_a_cross_project_tag_shows_its_receipt_and_never_its_content(world):
    from garuda.context import brief as briefs
    from garuda.context import tags

    store, ledger, tmp = world
    n = ids("tagger", "elsewhere", "local")
    other = tmp / "other"
    other.mkdir()
    store.begin(n["tagger"], task="t", model="m", agent="a", workspace=str(tmp))
    store.begin(n["elsewhere"], task="the secret plan", model="m", agent="a", workspace=str(other))
    store.begin(n["local"], task="t", model="m", agent="a", workspace=str(tmp))
    elsewhere = store.load_meta(n["elsewhere"]).get("project_id")
    pairs = [
        (tags.Tag(n["elsewhere"], "far", elsewhere, True, "cross-project-flag"),
         briefs.Brief(session_id=n["elsewhere"], name="far", project_id=elsewhere, runtime="native",
                      model="m", task="the secret plan", state="completed")),
        (tags.Tag(n["local"], "near", store.load_meta(n["local"]).get("project_id"), False, "flag"),
         briefs.Brief(session_id=n["local"], name="near", project_id=None, runtime="native",
                      model="m", task="t", state="completed"))]
    tags.record_links(store, n["tagger"], tags.Attached(
        tags=[p[0] for p in pairs], briefs=[p[1] for p in pairs],
        rendered=briefs.render([p[1] for p in pairs])))

    links = {k["session_id"]: k for k in conversation.conversation(
        store, n["tagger"], ledger=ledger)["links"]["tagged"]}
    far, near = links[n["elsewhere"]], links[n["local"]]
    assert far["cross_project"] and far["receipt"]["present"] and far["receipt"]["fingerprint"]
    assert near["cross_project"] is False and near["receipt"] is None
    assert "secret plan" not in json.dumps(far["receipt"])  # a fingerprint, never the content
    # a cross-project link whose receipt is missing says so rather than implying a grant
    (store.session_dir(n["tagger"]) / "receipts" / f"{n['elsewhere']}.json").chmod(0o600)
    (store.session_dir(n["tagger"]) / "receipts" / f"{n['elsewhere']}.json").unlink()
    gone = conversation.conversation(store, n["tagger"], ledger=ledger)["links"]["tagged"]
    assert {k["session_id"]: k["receipt"] for k in gone}[n["elsewhere"]] == {"present": False}
