"""Per step attempt cost and context measurement (#832): every usage report of a
session summarized (peak kept), a receipt's ``measure`` and how `flow show`,
`flow show --json` and `flow export` present it."""

import json
import uuid

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import engine, measure
from garuda.flows.engine import StepResult
from garuda.flows.trajectory import export_records, steps_reported_usage, trajectory
from garuda.observability import usage
from garuda.runtime.events import RuntimeEvent, RuntimeEventKind
from tests.test_flows import Fake, _block, _plan, _runner, repo  # noqa: F401  (fixture)


def _report(seq, **fields):
    return RuntimeEvent(RuntimeEventKind.MESSAGE, "s", 1, seq,
                        {"acp_update": "usage_update", "usage": fields})


def test_every_usage_report_is_summarized_and_the_peak_kept(tmp_path):
    store = SessionStore()
    session_id = str(uuid.uuid4())
    store.begin(session_id, task="t", model="m", agent="a", workspace=str(tmp_path))
    events = [_report(0, context_used=21000, context_size=200000, cost_usd=0.01, model="m-1"),
              _report(1, context_used=26000, context_size=200000, cost_usd=0.02, model="m-1"),
              _report(2, context_used=24000, context_size=200000, cost_usd=0.03, model="m-1")]

    usage.record_turn_acp_usage(store, session_id, events, runtime_id="fakeacp",
                                adapter_version="1", segment="native-1")

    summary = usage.usage_summary(store.load_meta(session_id))
    assert summary["reports"] == 3
    assert summary["first_context_used"] == 21000
    assert summary["peak_context_used"] == 26000  # not the last report's 24000
    assert summary["last_context_used"] == 24000
    assert summary["context_size"] == 200000
    assert summary["cost_usd"] == 0.03 and summary["cost_complete"] is True
    assert summary["models"] == ["m-1"]
    # A second turn of the same segment folds in: counts add, the peak stays.
    usage.record_turn_acp_usage(store, session_id, [_report(3, context_used=25000,
                                                             cost_usd=0.05)],
                                runtime_id="fakeacp", adapter_version="1", segment="native-1")
    summary = usage.usage_summary(store.load_meta(session_id))
    assert (summary["reports"], summary["peak_context_used"], summary["cost_usd"],
            summary["first_context_used"], summary["segments"]) == (4, 26000, 0.05, 21000, 1)
    # Bounded: the summary keeps figures per segment, never the reports.
    raw = store.load_meta(session_id)["usage_summary"]
    assert len(json.dumps(raw)) < 1000
    # The ledger keeps its latest-report snapshot as before.
    assert usage.reported_usage(session_id)["context_used"] == 25000


def test_a_session_without_a_summary_reads_as_unknown():
    assert usage.usage_summary({}) is None
    assert usage.usage_summary({"usage_summary": "garbage"}) is None
    assert usage.usage_summary({"usage_summary": {"segments": [1, None]}}) is None


async def test_a_receipt_measures_its_prompt_by_part(repo):  # noqa: F811
    async def build(launch):
        return StepResult(str(uuid.uuid4()), True, _block("patch", "a.txt: a -> b"))

    fake = Fake(repo, {"plan": _plan, "build": build})
    result = await _runner(repo, fake).run()

    assert result.completed
    for receipt, launch in zip(result.receipts, fake.launches, strict=True):
        record = receipt["measure"]
        prompt = record["prompt"]
        assert prompt["chars"] == len(launch.prompt)
        assert prompt["est_tokens"] == (len(launch.prompt) + 3) // 4
        assert sum(part["chars"] for part in prompt["parts"]) == len(launch.prompt)
        assert record["sessions"] == 1 and record["duration_ms"] == receipt["duration_ms"]
        # A stub launcher's session has no record: unknown, not zero.
        assert record["usage"] is None and record["turns"] is None
        assert "estimate" in record["estimate"]
    names = [part["part"] for part in result.receipts[1]["measure"]["prompt"]["parts"]]
    assert names[0] == "task" and "preamble" in names and "output_instructions" in names
    (fed,) = [p for p in result.receipts[1]["measure"]["prompt"]["parts"] if p["part"] == "input"]
    assert (fed["type"], fed["from"]) == ("plan", "plan")
    # Two journal entries per attempt, as before.
    entries = engine.journal(SessionStore(), result.flow_session)
    assert [e["event"] for e in entries] == ["intent", "receipt", "intent", "receipt"]


def test_an_old_receipt_without_measure_still_renders(repo, monkeypatch, capsys):  # noqa: F811
    import asyncio

    async def build(launch):
        return StepResult(str(uuid.uuid4()), True, _block("patch", "p"))

    runner = _runner(repo, Fake(repo, {"plan": _plan, "build": build}))
    result = asyncio.run(runner.run())
    store = SessionStore()
    folder = runner.dir / "receipts"
    for path in folder.iterdir():
        receipt = json.loads(path.read_text())
        receipt.pop("measure")
        path.chmod(0o600)
        path.write_text(json.dumps(receipt))
    shown = trajectory(store, result.flow_session)
    assert [a["measure"] for a in shown["attempts"]] == [None, None]
    total = shown["summary"]["measure"]
    assert total["measured_attempts"] == 0 and total["peak_context_used"] is None
    assert steps_reported_usage(shown) is not None
    from tests.test_runtime_cli import _main

    code, out = _main(monkeypatch, capsys, "flow", "show", result.flow_session)
    assert code == 0, out
    assert "plan (attempt 1): done" in out and "prompt ~" not in out


def test_flow_show_json_and_export_carry_the_measure(repo, tmp_path, monkeypatch, capsys,  # noqa: F811
                                                    fake_acp_confinement):
    from tests.test_runtime_cli import _install_shim, _main, _on_path, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="usage-rising")
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

    receipts = engine.receipts(store, flow_id)
    for receipt in receipts:
        record = receipt["measure"]
        assert record["sessions"] == 1 and record["turns"] == 1 and record["tool_calls"] == 1
        used = record["usage"]
        assert used["reports"] == 3 and used["first_context_used"] == 21000
        assert used["peak_context_used"] == 26000 and used["context_size"] == 200000
        assert used["cost_usd"] == 0.03 and used["cost_complete"] is True
        (session,) = record["per_session"]
        assert session["session_id"] == receipt["session_id"] and session["trail"] == "acp"

    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id)
    assert code == 0, out
    assert out.count("peak context 26000/200000 (3 reports); USD 0.03 reported") == 2
    assert "sessions 1, turns 1, tool calls 1" in out and "; stop end_turn" in out
    assert "measured 2 of 2 attempts: 2 session(s)" in out and "USD 0.06 reported" in out

    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--json")
    assert code == 0, out
    shown = json.loads(out)
    assert [a["measure"] for a in shown["attempts"]] == [r["measure"] for r in receipts]
    total = shown["summary"]["measure"]
    assert (total["sessions"], total["peak_context_used"], total["reported_cost_usd"],
            total["cost_complete"]) == (2, 26000, 0.06, True)
    # The adapter-reported totals keep working beside it.
    assert shown["summary"]["reported_cost_usd"] == 0.06

    records = list(export_records(store, flow_id))
    attempts = [r for r in records if r["record"] == "attempt"]
    assert all(a["measure"]["usage"]["peak_context_used"] == 26000 for a in attempts)
    (summary,) = [r for r in records if r["record"] == "summary"]
    assert summary["measure"]["sessions"] == 2


def test_measure_lines_and_totals():
    assert measure.line({}) is None
    record = measure.attempt(SessionStore(), prompt="x" * 10, parts=[{"part": "task", "chars": 10}],
                             session_ids=[], duration_ms=5)
    assert record["sessions"] == 0 and record["prompt"]["est_tokens"] == 3
    assert measure.line({"measure": record}) == (
        "prompt ~3 tok (10 chars); sessions 0, turns 0, tool calls 0; usage not reported")
    # A later turn loop's receipt counts slot in when present.
    later = {"measure": record, "sessions": ["a", "b"], "rotations": 1, "repairs": 0}
    assert "sessions 2, turns 0, tool calls 0, rotations 1, repairs 0;" in measure.line(later)
    total = measure.flow_total([{"measure": record}, {"measure": None}])
    assert total["measured_attempts"] == 1 and total["cost_complete"] is False


def test_measure_keeps_the_stop_reason_approval_denial_and_liveness(tmp_path):
    store = SessionStore()
    session_id = str(uuid.uuid4())
    store.begin(session_id, task="t", model="m", agent="a", workspace=str(tmp_path))
    trail = [{"kind": "outbound_prompt", "seq": 0, "payload": {"chars": 3}},
             {"kind": "tool_call", "seq": 1, "payload": {"tool_call_id": "c1"}},
             {"kind": "tool_call", "seq": 2, "payload": {"tool_call_id": "c1"}},
             {"kind": "lifecycle", "seq": 3, "payload": {
                 "state": "completed", "stop_reason": "end_turn", "raw_stop_reason": "end_turn",
                 "approval_denied": True}}]
    (store.session_dir(session_id) / "acp-events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in trail))
    outcome = StepResult(session_id, False, "", liveness="silence")
    record = measure.attempt(store, prompt="abc", parts=None, session_ids=[session_id],
                             duration_ms=None, outcomes=[outcome])
    assert (record["turns"], record["tool_calls"], record["stop_reason"],
            record["approval_denied"], record["liveness"]) == (1, 1, "end_turn", True, "silence")
    text = measure.line({"measure": record})
    assert text.endswith("; stop end_turn; approval denied; liveness silence")
    # The launcher's own stop reason wins over the trail's.
    record = measure.attempt(store, prompt="abc", parts=None, session_ids=[session_id],
                             duration_ms=None,
                             outcomes=[StepResult(session_id, False, "", stop_reason="refusal")])
    assert record["stop_reason"] == "refusal" and record["liveness"] is None


def test_prompt_part_sizes_add_up_for_every_shape():
    from garuda.flows import artifacts as art
    from garuda.flows import prompting

    ref = art.ArtifactRef(type="patch", digest="d" * 64, producer_step="build",
                          producer_session="s", attempt=1, size=13, workspace_version=None,
                          path="artifacts/x")
    step = {"id": "review", "outputs": ["review"]}
    packet = {"digest": "sha256:x", "binding": {"enforced": True}, "candidate": "c"}
    for read_only, run, extra in [(True, False, prompting.Packet(packet)),
                                  (False, False, prompting.Findings("- [major] add a test")),
                                  (False, False, "[garuda] a note"), (False, True, None),
                                  (True, False, None)]:
        sizes: list = []
        text = prompting.assemble(task="do it\nwell", flow="f", step=step, role="r",
                                  inputs=[(ref, "line 1\nline 2")], read_only=read_only,
                                  run=run, extra=extra, sizes=sizes)
        assert sum(part["chars"] for part in sizes) == len(text)
        names = [part["part"] for part in sizes]
        assert "task" in names
        if isinstance(extra, prompting.Packet):
            assert "packet" in names
        if isinstance(extra, prompting.Findings):
            assert "findings" in names
