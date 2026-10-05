"""Agent diagnostics at the production HTTP boundary contain no source text."""

import json
import tempfile
from pathlib import Path

import pytest

from garuda.agents import inspect
from garuda.core.sessions import SessionStore
from garuda.diagnostics import REGISTRY
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request

CANARY = "PRIVATE-CANARY"


@pytest.fixture
def private_workspace():
    with tempfile.TemporaryDirectory(prefix="garuda-http-") as root:
        yield Path(root)


@pytest.mark.parametrize("failure", ["yaml", "markdown", "legacy-warning", "prompt", "setup"])
def test_setup_agent_diagnostics_never_serve_private_source(
    tmp_path, private_workspace, monkeypatch, failure
):
    workspace = private_workspace / "workspace"
    directory = workspace / ".agent" / "agents"
    directory.mkdir(parents=True)
    (directory / "fine.yaml").write_text("version: 1\ninstructions: {text: fine}\n")
    if failure == "yaml":
        (directory / "broken.yaml").write_text("version: 1\ninstructions: {text: " + CANARY + "\n")
    elif failure == "markdown":
        (directory / "broken.md").write_text("---\nversion: 1\ninstructions: {text: " + CANARY + "\n---\nprivate body\n")
    elif failure == "legacy-warning":
        (directory / "broken.yaml").write_text("system_prompt: private body\n" + CANARY + ": ignored\n")
    else:
        (directory / "broken.yaml").write_text("version: 1\ninstructions: {text: private body}\n")

    # Detailed source diagnostics remain available to authorized local callers.
    if failure in ("yaml", "markdown"):
        local = next(r for r in inspect.list_agents(workspace) if r["qualified"] == "project/broken")
        assert CANARY in local["error"]
    elif failure == "legacy-warning":
        assert CANARY in json.dumps(inspect.show("project/broken", workspace)["warnings"])

    if failure == "prompt":
        original = inspect.prompt_agent

        def failing_prompt(agent, *args, **kwargs):
            if agent.name == "broken":
                error = ValueError(CANARY)
                error.code = CANARY  # unregistered codes must not become another source channel
                raise error
            return original(agent, *args, **kwargs)

        monkeypatch.setattr(inspect, "prompt_agent", failing_prompt)
    elif failure == "setup":
        def failing_inspection(*args, **kwargs):
            error = OSError(CANARY)
            error.code = CANARY
            raise error

        monkeypatch.setattr(inspect, "dashboard_rows", failing_inspection)

    ctx = DashboardContext(port=8787, token="test-token", store=SessionStore(tmp_path / "sessions"),
                           workspace=workspace)
    response = dispatch(Request(method="GET", path="/api/setup", query={},
                                headers={"host": "127.0.0.1:8787", TOKEN_HEADER: "test-token"}), ctx)
    assert response.status == 200
    assert CANARY not in response.body.decode()
    data = json.loads(response.body)
    rows = {row["name"]: row for row in data["agents"]}
    row = rows["(agents)" if failure == "setup" else "broken"]
    if failure == "legacy-warning":
        assert row["warnings"]
    else:
        assert row["error"] and row["error_code"] in REGISTRY
    if failure != "setup":
        assert rows["fine"]["prompt_digest"] and not rows["fine"].get("error")


@pytest.mark.parametrize("inherited", [False, True])
def test_setup_lists_unsupported_structural_fields_without_private_values(
        tmp_path, private_workspace, inherited):
    workspace = private_workspace / "workspace"
    directory = workspace / ".agent" / "agents"
    directory.mkdir(parents=True)
    (directory / "fine.yaml").write_text("version: 1\ninstructions: {text: fine}\n")
    (directory / "parent.yaml").write_text(
        "version: 1\nhooks: {before_tool: " + CANARY + "}\n"
        "instructions: {text: " + CANARY + "}\n")
    name = "child" if inherited else "parent"
    if inherited:
        (directory / "child.yaml").write_text(
            "version: 1\nextends: parent\nhooks: {after_tool: " + CANARY + "}\n")
    ctx = DashboardContext(port=8787, token="test-token", store=SessionStore(tmp_path / "sessions"),
                           workspace=workspace)
    response = dispatch(Request(method="GET", path="/api/setup", query={},
                                headers={"host": "127.0.0.1:8787", TOKEN_HEADER: "test-token"}), ctx)
    assert response.status == 200 and CANARY not in response.body.decode()
    rows = {row["name"]: row for row in json.loads(response.body)["agents"]}
    row = rows[name]
    expected = [{"path": "hooks.before_tool", "source": "project"}]
    if inherited:
        expected = [{"path": "hooks.after_tool", "source": "project"},
                    {"path": "hooks.before_tool", "source": "extends:project/parent"}]
    assert row["unsupported"] == expected
    assert row["error_code"] == "agent.unsupported_field" and row["error"]
    assert "prompt_digest" not in row and "digest" not in row
    assert rows["fine"]["prompt_digest"] and not rows["fine"].get("error")
