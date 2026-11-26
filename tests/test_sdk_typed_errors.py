"""SDK refusals carry the CLI's codes (#798).

An unknown agent raises ``AgentNotFound`` (``agent.not_found``, still a
``FileNotFoundError``); a local workspace that is not an existing directory
raises ``WorkspaceMissing`` / ``WorkspaceNotDirectory`` (``workspace.missing`` /
``workspace.not_directory``) before any baseline, session or lease. An
unknown runtime raises ``UnknownRuntime`` (``harness.unknown``, still a
``ValueError``) with the configured runtime ids (#805).
"""

import asyncio

import pytest

from garuda.agents.spec_api import AgentSpec
from garuda.sdk import (
    AgentNotFound,
    SoftwareAgent,
    UnknownRuntime,
    WorkspaceMissing,
    WorkspaceNotDirectory,
    WorkspaceUnavailable,
)
from garuda.workspace.paths import workspace_refusal


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_HOME", str(tmp_path / "home"))


def test_agent_spec_load_unknown_name_is_agent_not_found(tmp_path):
    with pytest.raises(AgentNotFound) as caught:
        AgentSpec.load("nonexist", workspace=tmp_path)
    assert caught.value.code == "agent.not_found"
    assert isinstance(caught.value, FileNotFoundError)
    assert "nonexist" in str(caught.value)


def test_agent_spec_from_missing_file_is_agent_not_found(tmp_path):
    with pytest.raises(AgentNotFound) as caught:
        AgentSpec.from_file(tmp_path / "missing.md", workspace=tmp_path)
    assert caught.value.code == "agent.not_found"


def test_existing_file_not_found_handlers_still_match(tmp_path):
    try:
        AgentSpec.load("nonexist", workspace=tmp_path)
    except FileNotFoundError as exc:
        assert exc.code == "agent.not_found"
    else:  # pragma: no cover
        pytest.fail("no error raised")


def test_software_agent_run_unknown_agent_is_agent_not_found(tmp_path):
    agent = SoftwareAgent(workspace=tmp_path, agent="nonexist")
    with pytest.raises(AgentNotFound) as caught:
        asyncio.run(agent.run("hi"))
    assert caught.value.code == "agent.not_found"


@pytest.mark.parametrize("kind", ["agent", "conversation"])
def test_missing_workspace_is_workspace_missing(tmp_path, kind):
    missing = tmp_path / "nonexistent"
    agent = SoftwareAgent(workspace=missing)
    run = agent.run if kind == "agent" else agent.conversation().run
    with pytest.raises(WorkspaceMissing) as caught:
        asyncio.run(run("hi"))
    assert caught.value.code == "workspace.missing"
    assert isinstance(caught.value, FileNotFoundError)
    assert isinstance(caught.value, WorkspaceUnavailable)
    assert not missing.exists()


def test_file_workspace_is_workspace_not_directory(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("x")
    with pytest.raises(WorkspaceNotDirectory) as caught:
        asyncio.run(SoftwareAgent(workspace=target).run("hi"))
    assert caught.value.code == "workspace.not_directory"
    assert isinstance(caught.value, NotADirectoryError)


def test_sdk_codes_match_cli_refusal(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("x")
    assert workspace_refusal(tmp_path / "nope").startswith(WorkspaceMissing.code + ":")
    assert workspace_refusal(target).startswith(WorkspaceNotDirectory.code + ":")
    assert workspace_refusal(tmp_path) is None


def test_cli_still_exports_workspace_refusal():
    from garuda.interfaces import main

    assert main.workspace_refusal is workspace_refusal


@pytest.mark.parametrize("kind", ["agent", "conversation"])
def test_unknown_runtime_is_unknown_runtime(tmp_path, kind):
    agent = SoftwareAgent(workspace=tmp_path, runtime="nope")
    run = agent.run if kind == "agent" else agent.conversation().run
    with pytest.raises(UnknownRuntime) as caught:
        asyncio.run(run("hi"))
    assert caught.value.code == "harness.unknown"
    assert isinstance(caught.value, ValueError)
    assert "unknown runtime 'nope'" in str(caught.value)
    assert "native" in caught.value.known and "nope" not in caught.value.known


def test_unknown_runtime_code_is_the_cli_diagnostic():
    from garuda.diagnostics import REGISTRY

    assert UnknownRuntime.code in REGISTRY
