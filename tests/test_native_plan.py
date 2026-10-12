"""Native configuration is resolved before activation, without live clients."""

import os
import subprocess
import sys
from pathlib import Path


def test_planning_needs_no_provider_import_or_child_process(tmp_path):
    # Run cold: another test importing a provider must not mask eager imports.
    script = r'''
import importlib.abc
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, sys.argv[1])
class NoProvider(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "litellm" or fullname.startswith("litellm."):
            raise RuntimeError("provider initialized during planning")
sys.meta_path.insert(0, NoProvider())
from garuda.agents.setup import plan_agent_run

def no_process(*args, **kwargs):
    raise RuntimeError("child launched during planning")
subprocess.Popen = no_process
plan = plan_agent_run("build", workspace=sys.argv[2], reasoning_model="openai/frozen", no_collection=True)
assert plan.bindings.reasoning.model == "openai/frozen"
assert plan.config.system_prompt
'''
    environment = {**os.environ, "GARUDA_GLOBAL_SETTINGS": str(tmp_path / "absent.yaml")}
    subprocess.run([sys.executable, "-I", "-c", script,
                    str(Path(__file__).resolve().parents[1]), str(tmp_path)],
                   env=environment, check=True, text=True, timeout=30)


async def test_activation_uses_resolved_profile_and_keeps_sdk_identity(tmp_path, monkeypatch):
    from garuda.agents.setup import activate_agent_plan, plan_agent_run
    from garuda.model.script_model import ScriptModel

    profiles = tmp_path / "agents"
    profiles.mkdir()
    profile = profiles / "frozen.yaml"
    profile.write_text("name: frozen\ntools: [read_file]\npermission_mode: readonly\n")
    client = ScriptModel([], model_name="sdk/owned")
    plan = plan_agent_run("frozen", workspace=str(tmp_path), agents_dir=profiles,
                          reasoning_model=client, no_collection=True)
    # A changed configuration must affect the next plan, not this activation.
    profile.write_text("invalid: [yaml")
    monkeypatch.setenv("GARUDA_REASONING_MODEL", "openai/replacement")
    first = await activate_agent_plan(plan, load_project_tools=False)
    second = await activate_agent_plan(plan, load_project_tools=False)
    try:
        assert first.reasoning is client and second.reasoning is client
        assert [tool.name for tool in first.tools] == ["read_file"]
        assert first.config.permission_mode == "readonly"
        first.config.system_prompt = "mutated running state"
        first.profile.tools.append("bash")
        assert second.config.system_prompt != first.config.system_prompt
        assert [tool.name for tool in second.tools] == ["read_file"]
        assert plan.profile.tools == ["read_file"]
    finally:
        for prepared in (first, second):
            if prepared.mcp_manager is not None:
                await prepared.mcp_manager.close()
