"""Offline synthetic regressions, driven by the native loop and existing owners.

These are contract observations, not model quality trials or paired uplift.
Fixtures contain only invented tasks, source files and scripted responses.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path

from garuda.config import garuda_yaml
from garuda.core.acceptance import verification_from
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.eval.evidence import EvaluationEvidence
from garuda.eval.harness_matrix import HarnessTrial, record_trial, write_matrix
from garuda.eval.minimal import (
    MinimalConfig,
    authority_identity,
    digest,
    environment_identity,
    runtime_identity,
    source_identity,
    tool_identity,
)
from garuda.flows.engine import flow_dir, receipts, recover
from garuda.flows.quota_recovery import RecoveryStore
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import ToolCall
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.no_edits import NoEditsGuard


def incident_cases() -> list[dict]:
    data = json.loads(files("garuda.eval").joinpath("fixtures/incidents-v1.json").read_text())
    if data["version"] != 1:
        raise ValueError("unsupported incident corpus version")
    return data["cases"]


async def run_incident(case: dict, root: Path) -> HarnessTrial:
    """Use a fresh fixture repository; never run against the caller's project."""
    # The native loop imports provider exception types; keep that import offline
    # for library callers as well as the contract-matrix command.
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    from garuda.core.rigorous import create_agent

    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    env = LocalEnvironment(workspace_root=workspace)
    for path, content in case["files"].items():
        await env.write_file(path, content)
    initialized = await env.execute("git init -q && git -c user.name=Fixture -c user.email=fixture@example.invalid "
                                    "-c core.hooksPath=/dev/null add . && "
                                    "git -c user.name=Fixture -c user.email=fixture@example.invalid "
                                    "-c core.hooksPath=/dev/null commit -qm fixture --allow-empty")
    if initialized.exit_code != 0:
        raise RuntimeError("synthetic fixture repository could not be initialized")
    minimal = MinimalConfig.load()
    config = minimal.config()
    events = EventStore()
    tools = tools_for_names(config.allowed_tools)
    permissions = PermissionEngine(workspace_root=workspace,
                                   **case.get("permissions", {"mode": config.permission_mode}))
    model_completed = None
    passed = False
    expected_observed = False
    responses = [ModelResponse(content=item.get("content"), raw=item.get("raw", {}),
                               tool_calls=[ToolCall(**call) for call in item["tool_calls"]])
                 for item in case["responses"]]

    async def run():
        result = await create_agent("build", mode=config.mode).run(
            task=case["prompt"], model=ScriptModel(responses, model_name="script/incident-v1"),
            env=env, tools=tools, config=config, events=events, permissions=permissions)
        return result

    incident = case["id"]
    if incident == "ignored_config":
        try:
            garuda_yaml.load_text(await env.read_file("garuda.yaml"))
        except garuda_yaml.GarudaConfigError as exc:
            passed = exc.code == "config.invalid" and exc.path == "roles.coder.efort"
        else:
            model_completed = (await run()).success
    elif incident == "unmatched_intent":
        store = SessionStore(root / "sessions")
        session_id = str(uuid.uuid4())
        store.begin(session_id, case["prompt"], "script/incident-v1", "build", str(workspace))
        directory = flow_dir(store, session_id)
        directory.mkdir(mode=0o700)
        (directory / "journal.jsonl").write_text(json.dumps(
            {"event": "intent", "index": 0, "step": "build", "attempt": 1}) + "\n")
        quarantined = recover(store, session_id)
        rows = receipts(store, session_id)
        passed = (quarantined == ["build"] and len(rows) == 1 and rows[0]["status"] == "quarantined"
                  and store.load_meta(session_id)["flow_state"] == "quarantined")
    elif incident == "quota_race":
        records = RecoveryStore(root / "recovery")
        session_id = str(uuid.uuid4())
        records.update(session_id, {"state": "waiting-for-quota"})
        # Both contenders see waiting before either attempts its atomic claim.
        observed = [records.get(session_id)["state"] for _ in range(2)]

        async def contender():
            try:
                await asyncio.to_thread(records.claim, session_id)
            except ValueError as exc:
                if "already claimed" not in str(exc):
                    raise
                return None
            return (await run()).success

        outcomes = await asyncio.gather(contender(), contender())
        dispatched = [value for value in outcomes if value is not None]
        model_completed = all(dispatched) if dispatched else None
        passed = (observed == ["waiting-for-quota"] * 2 and dispatched == [True]
                  and records.get(session_id)["state"] == "dispatch-reserved")
    else:
        guard = NoEditsGuard(workspace) if incident == "no_edits_startup" else None
        for path, content in case.get("startup", {}).items():
            await env.write_file(path, content)
        result = await run()
        model_completed = result.success
        if incident == "denied_reviewer_command":
            denied = [e["payload"] for e in events.get_all() if e["type"] == "permission_ask"
                      and e["payload"].get("id") == "attempt" and e["payload"].get("name") == "bash"
                      and e["payload"].get("approved") is False]
            passed = (result.success and not (workspace / "reviewer-marker.txt").exists()
                      and bool(denied))
        elif incident == "no_edits_startup":
            change = guard.check()
            passed = not change.unchanged and "adapter-state.txt" in change.changed
        elif incident == "stale_edits":
            messages = [m for m in result.messages if m.tool_call_id == "attempt"]
            passed = (await env.read_file("source.txt") == "collaborator-current-value\n"
                      and any("old_string not found" in m.content for m in messages))
        elif incident == "partial_output":
            passed = not (workspace / "answer.txt").exists()
            expected_observed = (not passed and result.success
                                 and await env.read_file("answer.txt") == "complete-"
                                 and any(e["payload"].get("truncated") is True for e in events.get_all()))
        else:
            raise ValueError(f"unknown incident {incident!r}")

    expected = case.get("expected_failure")
    coverage = "pass" if passed else "expected-fail" if expected and expected_observed else "fail"
    prompt_ids = [e["payload"]["digest"] for e in events.get_all() if e["type"] == "system_prompt"]
    evidence = EvaluationEvidence(
        task=digest(case), model=digest({"source": source_identity("model/script_model.py"),
                                        "responses": case["responses"]}),
        runtime=runtime_identity(),
        prompt=digest(prompt_ids) if prompt_ids else None,
        tools=tool_identity(tools),
        environment=environment_identity(), checks=source_identity("eval/incidents.py"),
        config=digest({"version": minimal.version, "settings": asdict(config)}),
        authority=authority_identity(permissions), mechanism=case["mechanism"],
        model_completed=model_completed, authorized_acceptance=verification_from([])["status"],
        coverage=coverage, cost_reason="offline ScriptModel; no provider calls; monetary cost unmeasured",
        expected_failure=expected,
        minimal_config=minimal.identity,
    )
    return record_trial(incident, case["prompt"], model="script/incident-v1", harness="native",
                        harness_version=evidence.runtime or "unknown", completed=model_completed,
                        capabilities=("prompt", "cancel"), evaluation=evidence)


async def run_incidents(out: str | Path, *, work_root: str | Path | None = None) -> list[HarnessTrial]:
    trials = []
    if work_root is not None:
        Path(work_root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="garuda-incidents-", dir=work_root) as temporary:
        for case in incident_cases():
            try:
                trials.append(await run_incident(case, Path(temporary) / case["id"]))
            except Exception as exc:  # noqa: BLE001 - retain other cells without persisting raw errors
                evidence = EvaluationEvidence(
                    task=digest(case), model=None, runtime=source_identity(case["owner"]), prompt=None,
                    tools=None, environment=environment_identity(), checks=source_identity("eval/incidents.py"),
                    config=None, authority=None, mechanism=case["mechanism"], model_completed=None,
                    authorized_acceptance="unavailable", coverage="unknown", cost_reason="trial did not settle",
                )
                trials.append(record_trial(case["id"], case["prompt"], model="script/incident-v1",
                                           harness="native", error=type(exc).__name__, evaluation=evidence))
    write_matrix(str(Path(out) / "incidents-v1.json"), trials)
    return trials
