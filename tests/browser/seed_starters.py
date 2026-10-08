"""Produce starter history with the real flow, ACP, role and acceptance owners."""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows.packaged import FLOWS
from garuda.scenarios.compile import compile_scenario
from garuda.scenarios.service import StarterService
from tests.test_runtime_cli import _git_workspace, _install_shim


def seed(workdir):
    workdir = workdir.resolve()
    workspace = _git_workspace(workdir)
    (workspace / "notes.md").write_text("Supplied browser context café\n")
    second = workdir / "second workspace"
    second.mkdir()
    (second / "notes.md").write_text("Second supplied context\n")
    bin_dir = workdir / "bin"
    marker = workdir / "launches.txt"
    _install_shim(bin_dir, "artifacts", marker=marker)
    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True)
    settings.write_text(gy.dump({"runtimes": [{"runtime_id": "producer", "kind": "acp", "version": "1",
                                               "command": [str(bin_dir / "fake-acp-shim")]}]}))
    gy.user_path().write_text(gy.dump({"version": 1, "roles": {
        role: {"harness": "producer"} for role in ("scout", "planner", "coder", "reviewer")}}))
    service = StarterService(SessionStore())
    inputs = {"goal": "Status 'client' <handoff>", "requirements": "All status transitions",
              "exclude": "Do not rewrite transport", "constraints": "Keep retry delay and protocol"}
    plan = compile_scenario("plan-change", inputs, workspace, store=service.store)
    planned = asyncio.run(service.start(plan))
    result = service.result(planned["session_id"])
    assert result["coverage"]["complete"] and result["next_action"]["id"] == "implement-plan", result
    # Execute the exact equivalent's arguments through the ordinary public CLI.
    # Its record intentionally has no structured starter metadata.
    copied = subprocess.run([sys.executable, "-m", "garuda.interfaces.main",
                             *shlex.split(plan.equivalent_command)[1:]], cwd=workspace,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
                            capture_output=True, text=True, check=True)
    ordinary = json.loads(copied.stdout.splitlines()[-1])["flow_session"]
    assert service.result(ordinary)["starter_metadata"] == "legacy"
    role = asyncio.run(service.start(compile_scenario("run-with-role", {
        "goal": "Check the existing client", "options": {"checks": ["true"]}}, workspace, store=service.store)))
    checked = service.result(role["session_id"])
    assert checked["verification"]["status"] == "passed", checked
    # Only native sessions have the existing events.jsonl trajectory. Exercise
    # the real role bridge with ScriptModel, as the chat browser fixture does.
    from garuda.agents.resolve import user_agents_dir
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel

    agents = user_agents_dir()
    agents.mkdir(parents=True)
    (agents / "browser-starter.yaml").write_text(gy.dump({
        "version": 1, "completion": {"verifier": False}, "tools": {"preset": "none"},
        "memory": {"user": False, "context_pack": False}, "context": {"three_step_summary": False},
        "limits": {"max_turns": 1}, "permissions": {"mode": "readonly"}}))
    gy.user_path().write_text(gy.dump({"version": 1, "roles": {"coder": {
        "harness": "native", "model_id": "fixture/model", "profile": "browser-starter"}}}))
    with patch("garuda.model.factory.ModelFactory.build_spec", return_value=ScriptModel([
            ModelResponse(content="Native starter answer", tool_calls=[])])):
        native = asyncio.run(service.start(compile_scenario("run-with-role", {
            "goal": "Native trace check", "options": {"checks": ["true"]}}, workspace, store=service.store)))
    assert service.result(native["session_id"])["verification"]["status"] == "passed"
    profiles = {}
    for profile in ("ready", "single", "missing", "waived"):
        folder = workdir / profile
        folder.mkdir()
        (folder / "settings.yaml").write_text("{}\n")
        roles = {name: {"harness": "native", "model_id": name + "/model"}
                 for name in ("scout", "planner", "coder", "reviewer")}
        if profile == "missing":
            roles.pop("reviewer")
        if profile in {"single", "waived"}:
            for value in roles.values():
                value.pop("model_id")
        doc = {"version": 1, "roles": roles}
        if profile == "waived":
            flow = deepcopy(FLOWS["plan-build-review"])
            flow["steps"][1]["review"]["independent"] = False
            doc["flows"] = {"plan-build-review": flow}
        (folder / "garuda.yaml").write_text(gy.dump(doc))
        profiles[profile] = str(folder / "settings.yaml")
    manifest = {"workspace": str(workspace), "second": str(second), "plan": planned["session_id"], "ordinary": ordinary,
                "role": role["session_id"], "native": native["session_id"], "reference": result["next_action"]["plan_artifact"],
                "artifact": str(service.store.root / planned["session_id"] / "flow" / result["artifacts"][-1]["path"]),
                "profiles": profiles, "marker": str(marker), "sessions": str(service.store.root)}
    (workdir / "manifest.json").write_text(json.dumps(manifest))


if __name__ == "__main__":
    seed(Path(sys.argv[1]))
