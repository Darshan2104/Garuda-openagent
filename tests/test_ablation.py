"""F3: ablation runner — variant matrix, ground-truth grading, summary table."""

from pathlib import Path

import garuda.eval.ablation as ablation
from garuda.eval.ablation import (
    AblationTask,
    VariantResult,
    render_table,
    run_ablation,
    summarize,
)
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.types import ToolCall


def test_builtin_suite_graders_roundtrip(tmp_path: Path):
    """Each built-in task's setup+check agrees with a correct/incorrect artifact."""
    from garuda.eval.ablation import BUILTIN_TASKS

    by_id = {t.id: t for t in BUILTIN_TASKS}
    assert {"find_value", "edit_greeting", "sum_numbers"} <= set(by_id)

    # find_value: correct value passes, wrong value fails.
    ws = tmp_path / "fv"
    ws.mkdir()
    by_id["find_value"].setup(ws)
    assert not by_id["find_value"].check(ws)  # nothing written yet
    (ws / "answer.txt").write_text("sk-abc123xyz\n")
    assert by_id["find_value"].check(ws)
    (ws / "answer.txt").write_text("wrong\n")
    assert not by_id["find_value"].check(ws)

    # edit_greeting: must swap hello->goodbye with no leftover 'hello'.
    ws = tmp_path / "ed"
    ws.mkdir()
    by_id["edit_greeting"].setup(ws)
    assert not by_id["edit_greeting"].check(ws)  # still says hello
    (ws / "greeting.py").write_text('print("goodbye")\n')
    assert by_id["edit_greeting"].check(ws)

    # sum_numbers: sum(1..20) == 210.
    ws = tmp_path / "sm"
    ws.mkdir()
    by_id["sum_numbers"].setup(ws)
    (ws / "sum.txt").write_text("210\n")
    assert by_id["sum_numbers"].check(ws)
    (ws / "sum.txt").write_text("209\n")
    assert not by_id["sum_numbers"].check(ws)


def test_summarize_and_table():
    results = [
        VariantResult("baseline", "t1", True, True, 3, 100, 20, 120, 500),
        VariantResult("baseline", "t2", True, False, 5, 200, 40, 240, 800),
        VariantResult("no_verifier", "t1", True, True, 2, 90, 10, 100, 400),
        VariantResult("no_verifier", "t2", True, True, 2, 90, 10, 100, 400),
    ]
    summary = summarize(results)
    assert summary["baseline"]["pass_rate"] == 0.5
    assert summary["no_verifier"]["pass_rate"] == 1.0
    table = render_table(results)
    assert "Pass rate" in table
    assert "baseline" in table and "no_verifier" in table


async def test_run_ablation_with_script_model(tmp_path: Path, monkeypatch):
    # A scripted agent that writes hello.txt then completes.
    def fresh_model(model_name):
        return ScriptModel(
            responses=[
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="w", name="write_file", arguments={"path": "hello.txt", "content": "hi"})
                    ],
                ),
                ModelResponse(
                    content=None,
                    tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "Wrote hello.txt with hi."})],
                ),
            ]
        )

    monkeypatch.setattr(ablation, "LitellmModel", fresh_model)

    task = AblationTask(
        id="create_file",
        prompt="Create hello.txt containing hi",
        check=lambda ws: (ws / "hello.txt").read_text().strip() == "hi",
    )
    results = await run_ablation(
        [task], {"baseline": {}, "no_verifier": {"enable_verifier": False}}, "script/test", base_dir=tmp_path
    )
    assert len(results) == 2
    assert all(r.graded_pass for r in results)
    # Ground-truth grading is independent of agent self-report.
    for r in results:
        assert (tmp_path / f"create_file__{r.variant}" / "hello.txt").exists()


async def test_grading_catches_agent_that_lies(tmp_path: Path, monkeypatch):
    """Ground-truth grading stays independent of what the agent claims.

    This is the whole point of grading: the agent asserts success having written
    nothing, and the verdict must come from the workspace rather than the claim.

    ``agent_success`` deliberately is not the assertion. It tracks whichever gates
    the variant runs — the ablation baseline is the default `interactive` posture,
    which accepts an evidence-free claim, while `require_discriminating_evidence`
    (on under `eval`) would refuse it. Both are correct for their posture; grading
    must say False either way, and that is what is pinned here.
    """
    def fresh_model(model_name):
        return ScriptModel(
            responses=[
                ModelResponse(
                    content=None,
                    tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "All done, file created (not really)."})],
                ),
            ]
        )

    monkeypatch.setattr(ablation, "LitellmModel", fresh_model)
    task = AblationTask(
        id="create_file",
        prompt="Create hello.txt containing hi",
        check=lambda ws: (ws / "hello.txt").exists(),
    )
    results = await run_ablation([task], {"baseline": {}}, "script/test", base_dir=tmp_path)
    # Ground truth is what counts, and it disagrees with the agent: nothing written.
    assert results[0].graded_pass is False
    assert not (tmp_path / "create_file__baseline" / "hello.txt").exists()


async def test_eval_gates_variant_refuses_the_same_lie(tmp_path: Path, monkeypatch):
    """The posture difference is measurable, which is why it is a variant.

    Same evidence-free claim as above; under `eval` the completion gate refuses it,
    so the run does not even report success. This is the cost/strictness trade the
    `interactive` default makes explicit.
    """
    def fresh_model(model_name):
        return ScriptModel(
            responses=[
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="d",
                            name="task_complete",
                            arguments={"summary": "All done, file created (not really)."},
                        )
                    ],
                ),
            ]
            * 4  # rejected attempts are re-tried, so the script must outlast the gate
        )

    monkeypatch.setattr(ablation, "LitellmModel", fresh_model)
    task = AblationTask(
        id="create_file",
        prompt="Create hello.txt containing hi",
        check=lambda ws: (ws / "hello.txt").exists(),
    )
    results = await run_ablation(
        [task],
        {"eval_gates": {"mode": "eval", "enable_llm_verifier": False}},
        "script/test",
        base_dir=tmp_path,
    )
    assert results[0].agent_success is False, "eval posture must refuse an evidence-free claim"
    assert results[0].graded_pass is False
