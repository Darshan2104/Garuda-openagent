import json
from importlib import resources
from pathlib import Path

import pytest

from garuda.config.recipes import load_recipe, render_template, resolve_recipe_params, run_recipe
from garuda.core.events import EventStore
from garuda.core.rigorous import RigorousAgent, create_agent
from garuda.interfaces.server import JsonRpcServer, ServerConfig
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.remote import RemoteWorkspace
from garuda.workspace.sandbox import SandboxEnvironment


def test_render_template():
    assert render_template("Fix {{issue}} with {{test_command}}", {"issue": "bug", "test_command": "pytest"}) == (
        "Fix bug with pytest"
    )


def test_load_default_recipe():
    recipe_path = Path(resources.files("garuda.config")) / "defaults" / "fix-and-test.yaml"
    recipe = load_recipe(recipe_path)
    assert recipe.name == "fix-and-test"
    assert len(recipe.steps) == 3
    params = resolve_recipe_params(recipe, {"issue": "login fails"})
    assert params["test_command"] == "pytest"


@pytest.mark.asyncio
async def test_run_recipe_steps(tmp_path):
    recipe_path = Path(resources.files("garuda.config")) / "defaults" / "fix-and-test.yaml"
    recipe = load_recipe(recipe_path)
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        responses=[
            ModelResponse(content="Task complete: inspect auth module and patch login flow", tool_calls=[]),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="write_file", arguments={"path": "fix.txt", "content": "patched"})
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="2", name="task_complete", arguments={"summary": "Applied auth patch and verified tests."}),
                ],
            ),
            # LLM verifier verdict for the step-2 completion.
            ModelResponse(content="APPROVED: patch applied and verified.", tool_calls=[]),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="3", name="task_complete", arguments={"summary": "Ran pytest successfully after auth fix."}),
                ],
            ),
            # LLM verifier verdict for the step-3 completion.
            ModelResponse(content="APPROVED: tests ran successfully.", tool_calls=[]),
        ]
    )
    results = await run_recipe(
        recipe,
        {"issue": "login fails"},
        model=model,
        env=env,
        workspace=str(tmp_path),
    )
    assert len(results) == 3
    assert results[-1].success
    assert (tmp_path / "fix.txt").read_text(encoding="utf-8") == "patched"


@pytest.mark.asyncio
async def test_rigorous_agent_plan_execute_critic(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        responses=[
            ModelResponse(content="Task complete: 1. Read code\n2. Patch bug", tool_calls=[]),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="write_file", arguments={"path": "done.txt", "content": "ok"})
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="2",
                        name="task_complete",
                        arguments={"summary": "Implemented fix and validated behavior."},
                    )
                ],
            ),
            ModelResponse(content="APPROVED: requirements met.", tool_calls=[]),
        ]
    )
    agent = RigorousAgent(profile_name="build")
    result = await agent.run(
        task="fix the bug",
        model=model,
        env=env,
        tools=tools_for_names(["write_file", "task_complete"]),
        config=AgentConfig(
            max_turns=10, enable_verifier=True, enable_llm_verifier=False, permission_mode="yolo"
        ),
        events=EventStore(),
    )
    assert result.success
    assert (tmp_path / "done.txt").exists()


def test_create_agent_modes():
    assert isinstance(create_agent("build", "standard").__class__.__name__, str)
    assert isinstance(create_agent("build", "rigorous"), RigorousAgent)


@pytest.mark.asyncio
async def test_sandbox_environment_execute(tmp_path):
    env = SandboxEnvironment(workspace_root=tmp_path)
    result = await env.execute("echo sandbox-ok")
    assert result.exit_code == 0
    assert "sandbox-ok" in result.stdout


def test_remote_workspace_docker_base(monkeypatch, tmp_path):
    workspace = RemoteWorkspace(workspace_root=tmp_path, docker_host="tcp://127.0.0.1:2375")
    assert workspace.docker_host == "tcp://127.0.0.1:2375"
    assert workspace._docker_base() == ["docker", "-H", "tcp://127.0.0.1:2375"]


@pytest.mark.asyncio
async def test_jsonrpc_health():
    server = JsonRpcServer(ServerConfig())
    response = await server.handle({"jsonrpc": "2.0", "method": "health", "id": 1})
    assert response["result"]["status"] == "ok"
    assert "version" in response["result"]


@pytest.mark.asyncio
async def test_jsonrpc_run(tmp_path):
    server = JsonRpcServer(ServerConfig(workspace=str(tmp_path), workspace_kind="local"))
    model_patch = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": "Completed RPC smoke test run."})
                ],
            )
        ]
    )

    import garuda.interfaces.server as server_module

    original = server_module.LitellmModel
    server_module.LitellmModel = lambda model_name, **kwargs: model_patch  # type: ignore[assignment]
    try:
        response = await server.handle(
            {
                "jsonrpc": "2.0",
                "method": "run",
                "params": {"task": "smoke test"},
                "id": 2,
            }
        )
    finally:
        server_module.LitellmModel = original

    assert "result" in response
    assert response["result"]["success"] is True
    assert response["result"]["session_id"]


@pytest.mark.asyncio
async def test_jsonrpc_list_agents():
    server = JsonRpcServer(ServerConfig())
    response = await server.handle({"jsonrpc": "2.0", "method": "list_agents", "id": 3})
    assert "build" in response["result"]["agents"]
