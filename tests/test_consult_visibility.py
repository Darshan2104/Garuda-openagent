"""Consult visibility: rows, rollups, setup and review annotations (#170, plan task G.4).

Every fixture is written by the production consult service and ledger.
"""
# ruff: noqa: F811  (the shared ``world`` fixture is imported and used as a test argument)

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from garuda.consult import view
from garuda.consult.errors import ConsultRefused
from garuda.consult.service import ChildOutcome, ConsultService
from garuda.core import conversation, read_model, usage_stats
from garuda.observability.ledger import Ledger, totals
from tests.test_consult import (
    DOC,
    Recorder,
    make_resolved,
    request,
    service,
    world,  # noqa: F401  (the shared consult fixture)
)

NOW = 1_790_000_000.0
QUESTION = "Is the retry loop in notes.txt safe to ship?"


def tamper(child):
    (Path(child.workspace) / "notes.txt").write_text("the child edited the snapshot\n")


async def failing(child):
    return ChildOutcome(child.child_id, False, "", denied_operations=3)


async def make_history(world):
    """An answered, a withheld, a failed and a timed-out consult under one root."""
    runs = [("r-answer", Recorder("Add a jitter; the loop is otherwise fine.")),
            ("r-withheld", Recorder("trust me", action=tamper)),
            ("r-failed", failing)]
    for request_id, runner in runs:
        await service(world, runner).consult(request(world, request_id=request_id,
                                                     question=QUESTION))

    async def slow(child):
        await asyncio.sleep(30)

    timeout = ConsultService(world.store, make_resolved({**DOC, "consults": {"timeout_sec": 1}}),
                             runner=slow)
    with pytest.raises(ConsultRefused):
        await timeout.consult(request(world, request_id="r-timeout", question=QUESTION))


async def test_each_outcome_is_a_row_with_evidence_and_no_text(world):
    await make_history(world)
    rows = {r["request_id"]: r for r in view.entries(world.store, world.asker)}
    assert {k: v["status"] for k, v in rows.items()} == {
        "r-answer": "answered", "r-withheld": "withheld", "r-failed": "failed",
        "r-timeout": "timeout"}
    answered = rows["r-answer"]
    assert answered["target"] == "reviewer" and answered["asker_role"] == "coder"
    assert answered["identity"] == {"runtime": "native", "kind": "native", "model_id": "m2",
                                    "effort": None}
    assert answered["changes"] == {"unchanged": True, "changed": 0, "evidence": "receipt"}
    assert answered["elapsed_ms"] >= 0 and answered["denied_operations"] == 0
    assert rows["r-withheld"]["changes"]["unchanged"] is False
    assert rows["r-withheld"]["code"] == "consult.unexpected_changes"
    assert rows["r-failed"]["denied_operations"] == 3
    assert rows["r-timeout"]["code"] == "consult.timeout"
    assert view.summary(list(rows.values())) == {"count": 4, "by_status": {
        "answered": 1, "failed": 1, "timeout": 1, "withheld": 1}}
    assert view.line(list(rows.values())) == \
        "consults: 4 (1 answered, 1 failed, 1 timeout, 1 withheld)"
    blob = json.dumps(list(rows.values()))
    for text in (QUESTION, "Add a jitter", "trust me"):
        assert text not in blob


async def test_a_quarantined_consult_and_an_unreadable_receipt_are_visible(world, monkeypatch):
    from garuda.consult import service as svc

    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("capacity: {native: 1}\n")
    monkeypatch.setattr(svc, "REAP_GRACE", 0.2)
    stop, entered = asyncio.Event(), asyncio.Event()
    children = []

    async def stubborn(child):
        children.append(asyncio.current_task())
        entered.set()
        while not stop.is_set():
            try:
                await stop.wait()
            except asyncio.CancelledError:
                pass

    consult = asyncio.create_task(ConsultService(world.store, make_resolved(), runner=stubborn).consult(
        request(world, request_id="stuck")))
    try:
        # Visibility needs a dispatched child. A setup deadline can instead refund
        # admission before launch, which correctly leaves no row to display.
        await asyncio.wait_for(entered.wait(), timeout=10)
        consult.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consult, timeout=5)
        (row,) = view.entries(world.store, world.asker)
        assert row["status"] == "quarantined" and row["code"] == "consult.quarantined"
        assert row["evidence"] == "state only" and row["changes"]["evidence"] == "unknown"
        assert row["changes"]["unchanged"] is None  # unknown, not "unchanged"

        receipts = Path(world.store.root) / ".consult" / world.asker / "receipts"
        receipts.mkdir(parents=True, exist_ok=True)
        (receipts / "bad.json").write_text("{not json")
        assert [r["status"] for r in view.entries(world.store, world.asker)] == ["quarantined"]
    finally:
        stop.set()
        if not consult.done():
            consult.cancel()
        await asyncio.gather(consult, *children, return_exceptions=True)


async def test_the_session_detail_and_the_cli_line_report_consults(world):
    await make_history(world)
    detail = read_model.session(world.store, world.asker)
    assert detail["consults"]["line"].startswith("consults: 4 (1 answered")
    assert detail["consults"]["summary"]["count"] == 4
    assert len(detail["consults"]["entries"]) == 4
    quiet = "00000000-0000-0000-0000-0000000000b9"
    world.store.begin(quiet, task="t", model="m", agent="build", workspace=str(world.ws))
    assert read_model.session(world.store, quiet)["consults"]["line"] == ""


def call(ledger, session, purpose, n, *, tokens, cost, origin=None, key=None):
    ledger.append({"kind": "native_model_call", "key": key or f"{session}:{purpose}:{n}",
                   "time": NOW, "session_id": session, "call_purpose": purpose, "model": "m1",
                   "input_tokens": tokens, "output_tokens": 1, "total_tokens": tokens + 1,
                   "cost_usd": cost, **({"origin": origin} if origin else {})})


async def test_parent_rollup_counts_each_event_once_and_matches_global_totals(world, tmp_path):
    runner = Recorder("Looks fine.")
    await service(world, runner).consult(request(world, request_id="r1"))
    child = runner.calls[0].child_id
    ledger = Ledger(tmp_path / "usage")
    for n in range(3):  # the parent's own work: 3 calls x 11 tokens, $0.01 each
        call(ledger, world.asker, "controller", n, tokens=10, cost=0.01)
    for n in range(2):  # the child's: 2 summarizer calls x 51 tokens, $0.02 each
        call(ledger, child, "summarizer", n, tokens=50, cost=0.02, origin="consult")
    # a replayed event and a second ledger read must not add anything
    call(ledger, child, "summarizer", 0, tokens=50, cost=0.02, origin="consult")

    used = conversation.conversation(world.store, world.asker, ledger=ledger)["models_used"]
    rows = {(r["origin"], r["work_type"]): r for r in used["rows"]}
    assert set(rows) == {("run", "controller"), ("consult", "summarizer")}  # purpose kept
    assert rows[("consult", "summarizer")]["calls"] == 2
    assert rows[("consult", "summarizer")]["total_tokens"] == 102
    assert rows[("run", "controller")]["total_tokens"] == 33
    # hand-calculated: 5 calls, 135 tokens, $0.07
    assert used["totals"]["native_calls"] == 5 and used["totals"]["total_tokens"] == 135
    assert used["totals"]["cost_usd"] == pytest.approx(0.07)
    by_origin = used["by_origin"]
    assert by_origin["run"]["native_calls"] + by_origin["consult"]["native_calls"] == 5
    assert by_origin["run"]["total_tokens"] + by_origin["consult"]["total_tokens"] == 135

    # the child's own conversation view shows only its own events
    own = conversation.models_used(world.store, child, {}, ledger)
    assert own["totals"]["native_calls"] == 2

    # global statistics sum the original events: same 5 calls, split by origin, no rollup double
    stats = usage_stats.stats(list(ledger.records()), "24h", NOW + 60)
    assert stats["measures"]["native_calls"] == 5 and stats["measures"]["total_tokens"] == 135
    origin_rows = {r["origin"]: r for r in stats["by_origin"]}
    assert origin_rows["run"]["native_calls"] == 3 and origin_rows["consult"]["native_calls"] == 2
    assert totals(list(ledger.records()))["native_calls"] == 5
    assert {r["work_type"] for r in stats["work_type_share"]["rows"]} == {
        "controller", "summarizer"}  # a consulted summarizer call is still a summarizer call


def test_setup_lists_grants_ceilings_and_each_adapters_transport_status():
    from garuda.core.setup_view import _consults

    resolved = make_resolved({**DOC, "consults": {"max_turns": 2}})
    info = _consults(resolved)
    (grant,) = info["grants"]
    assert grant["asker"] == "coder" and grant["targets"] == ["reviewer"]
    assert grant["transport"] == "native"
    assert info["limits"]["max_turns"] == 2 and info["limits"]["max_per_session"] == 5
    adapters = {a["package"]: a for a in info["adapters"]}
    claude = adapters["@agentclientprotocol/claude-agent-acp"]
    assert claude["forwarding"] == "supported" and claude["quiescence"] == "unknown"
    assert claude["exposed"] is False and "has not proved" in claude["reason"]


def stub_runner(world, config=None):
    from garuda.flows.engine import FlowRunner

    runner = SimpleNamespace(
        resolved=SimpleNamespace(config=(config or make_resolved().config)), store=world.store)
    return runner, FlowRunner._independence


def plan(runtime, model):
    return SimpleNamespace(runtime_id=runtime, model_id=model)


async def test_the_review_annotation_names_actual_identities_and_policy(world):
    await service(world, Recorder("fine")).consult(request(world, request_id="a"))
    config = make_resolved({"version": 1, "roles": {
        "coder": {"harness": "native", "model_id": "m1", "consult": ["reviewer"]},
        "reviewer": {"harness": "native", "model_id": "m2"},
        "auditor": {"harness": "native", "model_id": "m3"}}}).config
    runner, independence = stub_runner(world, config)
    step = {"id": "code", "role": "coder"}
    done = [{"step": "code", "session_id": world.asker,
             "role_plan": {"runtime_id": "native", "model_id": "m1"}}]

    ok = independence(runner, step, {"role": "auditor", "id": "audit"}, done, {},
                      plan("native", "m1"), plan("native", "m3"))
    assert ok["decision"] == "independent" and ok["policy"] == "required"
    assert ok["reviewer"] == {"role": "auditor", "runtime": "native", "model_id": "m3"}
    assert ok["reviewed"]["consulted"] == [{"runtime": "native", "model_id": "m2"}]
    assert ok["consults"] == 1

    # the reviewer is the role the coder actually consulted
    shared = independence(runner, step, {"role": "reviewer", "id": "review"}, done, {},
                          plan("native", "m1"), plan("native", "m2"))
    assert shared["decision"] == "not_independent"

    # a fallback that really launched as the reviewer's identity is caught after the fact
    fell_back = [{"step": "code", "session_id": "00000000-0000-0000-0000-0000000000f0",
                  "role_plan": {"runtime_id": "native", "model_id": "m3"}}]
    after = independence(runner, step, {"role": "auditor", "id": "audit"}, fell_back, {},
                         plan("native", "m1"), plan("native", "m3"))
    assert after["decision"] == "not_independent"
    assert after["reviewed"]["launched"] == [{"runtime": "native", "model_id": "m3"}]

    waived = independence(runner, step, {"role": "reviewer", "id": "review"}, done,
                          {"independent": False}, plan("native", "m1"), plan("native", "m2"))
    assert waived["policy"] == "waived" and waived["decision"] == "not_independent"
