"""A flow's whole trajectory for study (#726): step timings, the runtime identity
of each attempt, `flow show --json` and `flow export`."""

import asyncio
import datetime as dt
import json
import os
import stat
import uuid

from garuda.config import garuda_yaml as gy
from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from garuda.flows.engine import StepResult
from garuda.flows.trajectory import trajectory
from tests.test_flows import Fake, _block, _plan, _runner, repo  # noqa: F401  (fixture)


async def test_receipts_record_when_each_attempt_started_and_finished(repo):  # noqa: F811
    async def build(launch):
        await asyncio.sleep(0.05)
        (repo / "a.txt").write_text("b\n")
        return StepResult(str(uuid.uuid4()), True, _block("patch", "a.txt: a -> b"))

    result = await _runner(repo, Fake(repo, {"plan": _plan, "build": build})).run()

    assert result.completed
    for receipt in result.receipts:
        started = dt.datetime.fromisoformat(receipt["started_at"])
        finished = dt.datetime.fromisoformat(receipt["finished_at"])
        assert started <= finished
        assert type(receipt["duration_ms"]) is int and receipt["duration_ms"] >= 0
    build_receipt = result.receipts[1]
    assert build_receipt["duration_ms"] >= 40
    assert result.receipts[0]["finished_at"] <= build_receipt["started_at"]

    view = read_model.flow(SessionStore(), result.flow_session)
    plan_attempt = view["steps"][0]["attempts"][0]
    # What ran, from the receipt: never `None` for an attempt whose role resolved.
    assert plan_attempt["runtime"] == "native" and plan_attempt["runtime_kind"] == "native"
    assert plan_attempt["duration_ms"] == result.receipts[0]["duration_ms"]
    assert plan_attempt["started_at"] == result.receipts[0]["started_at"]


async def test_a_refused_attempt_has_no_start(repo, monkeypatch):  # noqa: F811
    from garuda.workspace.confined_acp import ConfinementRefused

    async def refused(launch):
        raise ConfinementRefused("no image")

    runner = _runner(repo, Fake(repo, {"plan": refused}))
    try:
        await runner.run()
    except engine.FlowStopped:
        pass
    (receipt,) = engine.receipts(SessionStore(), runner.flow_session)
    assert receipt["status"] == "stopped"
    # The intent was journaled and the launcher called: it started, then refused.
    assert receipt["started_at"] and type(receipt["duration_ms"]) is int


async def test_trajectory_orders_attempts_as_they_launched(repo):  # noqa: F811
    async def build(launch):
        return StepResult(str(uuid.uuid4()), True, _block("patch", "p"))

    result = await _runner(repo, Fake(repo, {"plan": _plan, "build": build})).run()
    store = SessionStore()
    shown = trajectory(store, result.flow_session)
    assert shown["schema"] == "garuda.flow-trajectory"
    assert [(a["step"], a["attempt"]) for a in shown["attempts"]] == [("plan", 1), ("build", 1)]
    assert [e["event"] for e in shown["journal"]] == ["intent", "receipt", "intent", "receipt"]
    assert shown["summary"]["attempts"] == 2 and shown["summary"]["timed_attempts"] == 2
    first = shown["attempts"][0]
    assert first["session"]["session_id"] == first["session_id"]
    # No ACP trail for a native step: nothing is exported in its place.
    assert first["session"]["events"]["source"] is None


def test_flow_show_json_and_export_cover_every_step_session(repo, tmp_path, monkeypatch, capsys,  # noqa: F811
                                                           fake_acp_confinement):
    from tests.test_runtime_cli import _install_shim, _main, _on_path, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="success")
    _trusted_settings(tmp_path, monkeypatch)
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(
        "version: 1\nroles:\n  planner: {harness: fakeacp, write_policy: no-edits}\n"
        "  coder: {harness: fakeacp}\n"
        "flows:\n  pb:\n    steps:\n      - {id: plan, role: planner, outputs: [plan]}\n"
        "      - {id: build, role: coder, inputs: [plan]}\n")
    fake_acp_confinement("fakeacp")
    code, out = _main(monkeypatch, capsys, "flow", "run", "pb", "-t", "add a feature",
                      "--workspace", str(repo))
    assert code == 0, out
    flow_id = json.loads(out.strip().splitlines()[-1])["flow_session"]
    store = SessionStore()
    steps = {m["flow_step"]["step"]: m["session_id"]
             for m in store.list_sessions() if m.get("flow_step")}
    assert set(steps) == {"plan", "build"}

    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id)
    assert code == 0, out
    assert "plan (attempt 1): done; outputs: plan; fakeacp" in out

    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--json")
    assert code == 0, out
    shown = json.loads(out)
    assert [(a["step"], a["session_id"]) for a in shown["attempts"]] == [
        ("plan", steps["plan"]), ("build", steps["build"])]
    for attempt in shown["attempts"]:
        assert attempt["runtime"] == "fakeacp" and attempt["status"] == "done"
        assert attempt["started_at"] and type(attempt["duration_ms"]) is int
        session = attempt["session"]
        assert session["readable"] and session["runtime"] == "fakeacp"
        assert session["status"] == "completed"
        assert session["events"]["source"] == "acp-events.jsonl" and session["events"]["count"] > 0
        assert "usage" in session and "events_records" not in attempt
    assert shown["attempts"][0]["outputs"][0]["type"] == "plan"

    out_file = tmp_path / "trace.jsonl"
    code, out = _main(monkeypatch, capsys, "flow", "export", flow_id[:13], "--out", str(out_file),
                      "--workspace", str(repo))
    assert code == 0, out
    assert stat.S_IMODE(os.stat(out_file).st_mode) == 0o600
    records = [json.loads(line) for line in out_file.read_text().splitlines()]
    kinds = [r["record"] for r in records]
    assert kinds[0] == "flow" and kinds[-1] == "summary"
    assert kinds.count("journal") == 4 and kinds.count("attempt") == 2
    assert records[0]["flow_session"] == flow_id and records[0]["name"] == "pb"
    for step, session_id in steps.items():
        exported = [r["event"] for r in records
                    if r["record"] == "event" and r["session_id"] == session_id]
        persisted = [json.loads(line) for line in
                     (store.session_dir(session_id) / "acp-events.jsonl").read_text().splitlines()]
        # Exactly the metadata-level trail Garuda persisted: nothing added.
        assert exported == persisted and exported
        assert all("title" not in (e.get("payload") or {}) for e in exported)
        assert all(r["step"] == step for r in records
                   if r["record"] == "event" and r["session_id"] == session_id)
    order = [(r["record"], r.get("step")) for r in records if r["record"] in ("attempt", "event")]
    assert order.index(("attempt", "build")) > max(
        i for i, item in enumerate(order) if item == ("event", "plan"))

    code, out = _main(monkeypatch, capsys, "sessions", "show", flow_id, "--json",
                      "--workspace", str(repo))
    assert code == 0, out
    attempts = [a for s in json.loads(out)["flow"]["steps"] for a in s["attempts"]]
    assert {a["runtime"] for a in attempts} == {"fakeacp"}
