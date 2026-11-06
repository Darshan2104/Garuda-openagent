"""Offline incident contracts at their existing production boundaries."""

import json
import os
import subprocess
import sys
from dataclasses import asdict

import pytest

from garuda.eval.ablation import AblationTask, run_minimal_pair, run_variant
from garuda.eval.evidence import EvaluationEvidence
from garuda.eval.harness_matrix import HarnessTrial, load_trials, trials_from_ablation, write_matrix
from garuda.eval.incidents import incident_cases, run_incident
from garuda.eval.minimal import MinimalConfig
from garuda.model.factory import ModelFactory
from garuda.model.protocol import ModelResponse
from garuda.types import ToolCall


@pytest.mark.parametrize("case", [
    pytest.param(case, id=case["id"], marks=pytest.mark.xfail(
        strict=True, reason=case["expected_failure"])) if case.get("expected_failure") else
    pytest.param(case, id=case["id"]) for case in incident_cases()
])
async def test_incident_owner_boundary(case, tmp_path, monkeypatch):
    def no_provider(*args, **kwargs):
        pytest.fail("offline incident attempted provider construction")

    monkeypatch.setattr(ModelFactory, "build", no_provider)
    trial = await run_incident(case, tmp_path)
    assert trial.evaluation.coverage == "pass", trial.to_dict()
    assert trial.evaluation.authorized_acceptance == "unavailable"
    assert trial.cost_usd is None


def test_incident_cli_persists_coverage_and_unknowns(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "garuda.eval.contract_matrix", "--incidents", "--out", str(tmp_path)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "False"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXPECTED-FAIL partial_output" in result.stdout
    assert "no model uplift" in result.stdout
    report = json.loads((tmp_path / "incidents-v1.json").read_text())
    assert report["evaluation"]["coverage"] == {"pass": 6, "fail": 0, "expected-fail": 1, "unknown": 0}
    trials = {t.task_id: t for t in load_trials(str(tmp_path / "incidents-v1.json"))}
    assert trials["ignored_config"].completed is None
    assert trials["ignored_config"].evaluation.prompt is None  # refused before inference
    assert trials["no_edits_startup"].completed is True
    assert trials["no_edits_startup"].evaluation.authorized_acceptance == "unavailable"
    assert report["evaluation"]["unknown_costs"] == 7
    assert "synthetic startup metadata" not in json.dumps(report)
    failed_out = tmp_path / "no-git"
    result = subprocess.run(
        [sys.executable, "-m", "garuda.eval.contract_matrix", "--incidents", "--out", str(failed_out)],
        capture_output=True, text=True, timeout=60, env={**os.environ, "PATH": ""},
    )
    assert result.returncode == 1, result.stdout + result.stderr
    failed = json.loads((failed_out / "incidents-v1.json").read_text())
    assert failed["evaluation"]["coverage"] == {"pass": 0, "fail": 0, "expected-fail": 0, "unknown": 7}
    assert all(t["completed"] is None and t["cost_usd"] is None for t in failed["trials"])


async def test_minimal_pair_changes_only_selected_mechanism(tmp_path):
    minimal = MinimalConfig.load()
    assert minimal.identity == "421755838783d55eb299c2def7b319716c115f3b4561fd778d466982d1d7f516"
    baseline = asdict(minimal.config(mechanism="require_discriminating_evidence", enabled=False))
    candidate = asdict(minimal.config(mechanism="require_discriminating_evidence", enabled=True))
    assert {k for k in baseline if baseline[k] != candidate[k]} == {"require_discriminating_evidence"}
    baseline["allowed_tools"].clear()
    assert minimal.config().allowed_tools  # callers cannot mutate the frozen inputs
    with pytest.raises(ValueError, match="supported boolean"):
        minimal.config(mechanism="permission_mode", enabled=True)
    responses = [ModelResponse(content=None, tool_calls=[ToolCall(
        id="done", name="task_complete", arguments={
            "summary": "Created the requested output and verified the synthetic task successfully."})])]
    task = AblationTask(id="missing-output", prompt="Create missing.txt", check=lambda ws: (ws / "missing.txt").exists())
    with pytest.raises(ValueError, match="offline ScriptModel"):
        await run_variant(task, "candidate", {}, "provider/unused", tmp_path, minimal=minimal)
    results = await run_minimal_pair(task, "require_discriminating_evidence", responses, tmp_path)
    assert [r.agent_success for r in results] == [True, False]
    assert [r.graded_pass for r in results] == [False, False]
    trials = trials_from_ablation([task], results, model="script/incident-v1")
    first, second = [t.evaluation for t in trials]
    for name in ("task", "model", "runtime", "prompt", "tools", "environment", "authority", "checks", "minimal_config"):
        assert getattr(first, name) == getattr(second, name), name
    assert first.config != second.config
    assert (first.variant, second.variant) == ("baseline", "candidate")
    write_matrix(str(tmp_path / "pair.json"), trials)
    loaded = load_trials(str(tmp_path / "pair.json"))
    assert [t.completed for t in loaded] == [True, False]
    assert all(t.evaluation.authorized_acceptance == "unavailable" for t in loaded)


def test_report_roundtrip_retains_evaluation_unknowns(tmp_path):
    evidence = EvaluationEvidence(
        task="fixture-v1", model=None, runtime=None, prompt="prompt-v1", tools=None,
        environment=None, checks=None, config="config-v1", authority=None, mechanism="schema",
        model_completed=True, authorized_acceptance="unavailable", coverage="unknown",
        cost_reason="not measured",
    )
    # Exercise the existing ingestion and report boundary: old ingestion silently
    # drops this evidence, which must survive even with every optional fact unknown.
    trial = HarnessTrial.from_dict({"task_id": "t", "prompt_hash": "abcdef", "harness": "native",
                                    "completed": True, "evaluation": evidence.to_dict()})
    write_matrix(str(tmp_path / "report.json"), [trial])
    stored = json.loads((tmp_path / "report.json").read_text())
    assert stored["trials"][0]["evaluation"] == evidence.to_dict()
    assert stored["evaluation"]["coverage"]["unknown"] == 1
    assert load_trials(str(tmp_path / "report.json"))[0].cost_usd is None
    for field in ("checks", "model", "runtime"):
        broken = evidence.to_dict()
        del broken[field]
        with pytest.raises(ValueError, match="every identity"):
            HarnessTrial.from_dict({"task_id": "t", "prompt_hash": "abcdef", "harness": "native",
                                    "evaluation": broken})
    with pytest.raises(ValueError, match="must agree"):
        HarnessTrial.from_dict({"task_id": "t", "prompt_hash": "abcdef", "harness": "native",
                                "completed": False, "evaluation": evidence.to_dict()})
