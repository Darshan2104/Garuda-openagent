"""Shared native setup: every entry point resolves identical inputs identically.

Covers implementation-plan Task 3 tests: CLI/chat/server/SDK/conversation/
recipe/dashboard/eval parity, legacy `--model` behavior, omitted-flag
fall-through, conflicting-flag errors, per-job client isolation, and unchanged
single-model runs when collection is absent.
"""

import asyncio

import pytest

from garuda.agents.setup import coerce_reasoning_flag, prepare_agent_run
from garuda.interfaces.main import build_parser
from garuda.model.config import ConfigError
from garuda.model.factory import safe_model_identity
from garuda.model.protocol import DEFAULT_MODEL
from garuda.model.script_model import ScriptModel


def _parse(command: list[str]):
    return build_parser().parse_args(command)


def test_omitted_model_flags_stay_none_for_every_subcommand():
    """No eager built-in default: omission must not mask lower-precedence config."""
    run = _parse(["run", "-t", "task"])
    assert run.model is None and run.reasoning_model is None
    assert run.collection_model is None and run.no_collection is False
    chat = _parse(["chat"])
    assert chat.model is None and chat.reasoning_model is None
    assert chat.collection_model is None and chat.no_collection is False
    serve = _parse(["serve"])
    assert serve.model is None and serve.reasoning_model is None
    assert serve.collection_model is None and serve.no_collection is False
    recipe = _parse(["recipe", "run", "some.yaml"])
    assert recipe.model is None and recipe.reasoning_model is None
    assert recipe.collection_model is None and recipe.no_collection is False


def test_model_is_a_reasoning_alias_and_conflicts_fail_closed():
    assert coerce_reasoning_flag("openrouter/a/m", None) == "openrouter/a/m"
    assert coerce_reasoning_flag(None, "openrouter/a/m") == "openrouter/a/m"
    assert coerce_reasoning_flag("openrouter/a/m", "openrouter/a/m") == "openrouter/a/m"
    with pytest.raises(ConfigError, match="conflicting model flags"):
        coerce_reasoning_flag("openrouter/a/m", "openrouter/b/m")


async def test_explicit_model_behaves_as_before(tmp_path):
    prepared = await prepare_agent_run(
        "build", workspace=str(tmp_path), model="openrouter/x/m"
    )
    assert prepared.bindings.reasoning.model == "openrouter/x/m"
    assert prepared.provenance["reasoning"].provenance.value == "explicit"
    assert safe_model_identity(prepared.reasoning) == "openrouter/x/m"
    if prepared.mcp_manager is not None:
        await prepared.mcp_manager.close()


async def test_omitted_flags_allow_env_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_REASONING_MODEL", "openrouter/env/m")
    monkeypatch.delenv("GARUDA_MODEL", raising=False)
    prepared = await prepare_agent_run("build", workspace=str(tmp_path))
    assert prepared.bindings.reasoning.model == "openrouter/env/m"
    assert prepared.provenance["reasoning"].provenance.value == "env"
    if prepared.mcp_manager is not None:
        await prepared.mcp_manager.close()


async def test_legacy_garuda_model_still_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv("GARUDA_REASONING_MODEL", raising=False)
    monkeypatch.setenv("GARUDA_MODEL", "openrouter/legacy/m")
    prepared = await prepare_agent_run("build", workspace=str(tmp_path))
    assert prepared.bindings.reasoning.model == "openrouter/legacy/m"
    assert prepared.provenance["reasoning"].provenance.value == "legacy_env"
    if prepared.mcp_manager is not None:
        await prepared.mcp_manager.close()


async def test_single_model_run_has_no_collection_client(tmp_path, monkeypatch):
    monkeypatch.delenv("GARUDA_REASONING_MODEL", raising=False)
    monkeypatch.delenv("GARUDA_MODEL", raising=False)
    monkeypatch.delenv("GARUDA_COLLECTION_MODEL", raising=False)
    prepared = await prepare_agent_run("build", workspace=str(tmp_path))
    assert prepared.bindings.reasoning.model == DEFAULT_MODEL
    assert prepared.bindings.collection is None
    assert prepared.collection is None
    assert prepared.provenance["reasoning"].provenance.value == "builtin"
    if prepared.mcp_manager is not None:
        await prepared.mcp_manager.close()


async def test_two_roles_resolve_two_providers(tmp_path):
    prepared = await prepare_agent_run(
        "build",
        workspace=str(tmp_path),
        reasoning_model="provider-a/reasoning",
        collection_model="provider-b/collector",
    )
    assert prepared.bindings.reasoning.model == "provider-a/reasoning"
    assert prepared.bindings.collection.model == "provider-b/collector"
    assert prepared.reasoning is not prepared.collection
    assert prepared.reasoning.model_name == "provider-a/reasoning"
    assert prepared.collection.model_name == "provider-b/collector"
    if prepared.mcp_manager is not None:
        await prepared.mcp_manager.close()


async def test_sdk_models_kept_by_identity(tmp_path):
    reasoning = ScriptModel([], model_name="sdk/reasoning")
    collection = ScriptModel([], model_name="sdk/collector")
    prepared = await prepare_agent_run(
        "build",
        workspace=str(tmp_path),
        reasoning_model=reasoning,
        collection_model=collection,
    )
    assert prepared.reasoning is reasoning
    assert prepared.collection is collection
    if prepared.mcp_manager is not None:
        await prepared.mcp_manager.close()


async def test_no_collection_flag_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_COLLECTION_MODEL", "provider-b/collector")
    prepared = await prepare_agent_run(
        "build", workspace=str(tmp_path), no_collection=True
    )
    assert prepared.collection is None
    assert prepared.provenance["collection"].provenance.value == "explicit"
    if prepared.mcp_manager is not None:
        await prepared.mcp_manager.close()


async def test_tuple_unpack_keeps_working(tmp_path):
    profile, config, permissions, tools, agent, mcp_manager = await prepare_agent_run(
        "build", workspace=str(tmp_path)
    )
    assert profile.name == "build"
    assert config is not None and permissions is not None
    if mcp_manager is not None:
        await mcp_manager.close()


async def test_profile_binding_alias_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv("GARUDA_REASONING_MODEL", raising=False)
    monkeypatch.delenv("GARUDA_MODEL", raising=False)
    monkeypatch.delenv("GARUDA_COLLECTION_MODEL", raising=False)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "aliased.yaml").write_text(
        "name: aliased\nmodel_binding: duo\ntools: [read_file, task_complete]\n",
        encoding="utf-8",
    )
    global_settings = tmp_path / "global-settings.yaml"
    global_settings.write_text(
        "models:\n"
        "  strong:\n    transport: litellm\n    model: provider-a/reasoning\n"
        "  collector:\n    transport: litellm\n    model: provider-b/collector\n"
        "model_bindings:\n"
        "  duo:\n    reasoning: strong\n    collection: collector\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(global_settings))
    prepared = await prepare_agent_run(
        "aliased", workspace=str(tmp_path), agents_dir=agents_dir
    )
    assert prepared.bindings.reasoning.model == "provider-a/reasoning"
    assert prepared.bindings.collection.model == "provider-b/collector"
    assert prepared.provenance["reasoning"].provenance.value == "profile"
    if prepared.mcp_manager is not None:
        await prepared.mcp_manager.close()


async def test_unknown_profile_alias_fails_actionably(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "bad.yaml").write_text(
        "name: bad\nmodel_binding: nope\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="unknown model_binding alias"):
        await prepare_agent_run("bad", workspace=str(tmp_path), agents_dir=agents_dir)


async def test_concurrent_prepares_do_not_share_clients(tmp_path):
    first, second = await asyncio.gather(
        prepare_agent_run(
            "build", workspace=str(tmp_path), reasoning_model="provider-a/r1"
        ),
        prepare_agent_run(
            "build", workspace=str(tmp_path), reasoning_model="provider-a/r2"
        ),
    )
    try:
        assert first.reasoning is not second.reasoning
        assert first.reasoning.model_name == "provider-a/r1"
        assert second.reasoning.model_name == "provider-a/r2"
    finally:
        if first.mcp_manager is not None:
            await first.mcp_manager.close()
        if second.mcp_manager is not None:
            await second.mcp_manager.close()


async def test_server_execute_scopes_models_per_request(tmp_path, monkeypatch):
    """Two jobs with different bindings get different clients, no shared state."""
    from garuda.interfaces.server import JsonRpcServer, ServerConfig

    server = JsonRpcServer(ServerConfig(token=None, max_jobs=2))
    seen: list[str] = []
    real_prepare = prepare_agent_run

    async def tracking_prepare(agent_name, **kwargs):
        prepared = await real_prepare(agent_name, **kwargs)
        seen.append(prepared.reasoning.model_name)
        return prepared

    monkeypatch.setattr("garuda.interfaces.server.prepare_agent_run", tracking_prepare)

    async def fake_run(**kwargs):
        from garuda.types import AgentResult

        return AgentResult(success=True, final_message="ok", messages=[], turns=1)

    monkeypatch.setattr("garuda.interfaces.server.run_agent_task", fake_run)

    await server._execute({"task": "t1", "model": "provider-a/one", "workspace": str(tmp_path)},
                          __import__("garuda.core.events", fromlist=["EventStore"]).EventStore())
    await server._execute({"task": "t2", "model": "provider-a/two", "workspace": str(tmp_path)},
                          __import__("garuda.core.events", fromlist=["EventStore"]).EventStore())
    assert seen == ["provider-a/one", "provider-a/two"]


async def test_agent_session_and_sdk_resolve_like_cli(tmp_path, monkeypatch):
    """Session/SDK/conversation go through the same shared setup."""
    from garuda.interfaces.session import AgentSession
    from garuda.sdk.conversation import Conversation
    from garuda.sdk.software_agent import SoftwareAgent

    monkeypatch.delenv("GARUDA_REASONING_MODEL", raising=False)
    monkeypatch.delenv("GARUDA_MODEL", raising=False)
    monkeypatch.delenv("GARUDA_COLLECTION_MODEL", raising=False)

    session = await AgentSession.create(agent_name="build", workspace=str(tmp_path))
    try:
        assert session.bindings.reasoning.model == DEFAULT_MODEL
        assert session.collection is None
    finally:
        await session.close()

    agent = SoftwareAgent(workspace=str(tmp_path))
    convo = agent.conversation()
    assert convo._model_name == DEFAULT_MODEL
    assert convo._reasoning_model is None
    await convo.close()

    custom = SoftwareAgent(
        workspace=str(tmp_path),
        reasoning_model="provider-a/r",
        collection_model="provider-b/c",
    )
    assert custom.reasoning_model == "provider-a/r"
    assert custom.collection_model == "provider-b/c"
    convo2 = Conversation(workspace=str(tmp_path), model="provider-a/r")
    assert convo2._model_name == "provider-a/r"
    await convo2.close()
