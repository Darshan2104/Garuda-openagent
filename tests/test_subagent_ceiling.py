"""A subagent never runs with more authority than the run that started it (#142).

``invoke_subagent`` takes a free profile name from the model. Before this, the child's
``PermissionEngine`` was built from the child profile alone, so a ``smart`` parent could
delegate to the packaged ``harbor`` profile (``permission_mode: yolo``) and run bash and
file writes with no approval. These tests drive the real tool runner with a scripted
model and observe filesystem effects and the parent's approval handler — not which
private helper was called.
"""

from pathlib import Path

import pytest

from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import DelegatedPermissionEngine, PermissionDecision, PermissionEngine
from garuda.core.subagent import SubagentRunner
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment


def _call(name: str, call_id: str = "c", **arguments) -> ModelResponse:
    return ModelResponse(content=None, tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def _done(call_id: str = "d") -> ModelResponse:
    return _call("task_complete", call_id, summary="A fully detailed completion summary of the work done.")


def _delegate(profile: str, call_id: str = "s1") -> ModelResponse:
    return _call("invoke_subagent", call_id, profile=profile, task="do the delegated step", handoff="none")


class _Approvals:
    def __init__(self, answer: bool):
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, action: str) -> bool:
        self.asked.append(action)
        return self.answer


async def _run_parent(tmp_path: Path, script: list[ModelResponse], permissions, agents_dir=None):
    env = LocalEnvironment(workspace_root=tmp_path)
    return await DefaultAgent().run(
        task="delegate then finish",
        model=ScriptModel(script),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=6, enable_verifier=False),
        permissions=permissions,
        agents_dir=agents_dir,
    )


def _touch_child_script(marker: str) -> list[ModelResponse]:
    return [
        _delegate("harbor"),
        _call("bash", "b1", command=f"touch {marker}"),
        _done("d1"),
        _done("d2"),
    ]


@pytest.mark.parametrize("answer", [True, False])
async def test_a_yolo_child_still_asks_through_the_parents_handler(tmp_path, answer):
    approvals = _Approvals(answer)
    parent = PermissionEngine(mode="smart", bash_rules={"ask": [r"^touch "]}, approval_handler=approvals)

    await _run_parent(tmp_path, _touch_child_script("marker"), parent)

    assert len(approvals.asked) == 1
    assert "touch marker" in approvals.asked[0]
    assert (tmp_path / "marker").exists() is answer


async def test_a_readonly_parent_denies_a_childs_write(tmp_path):
    parent = PermissionEngine(mode="readonly")
    script = [
        _delegate("harbor"),
        _call("write_file", "w1", path="sentinel.txt", content="changed"),
        _done("d1"),
        _done("d2"),
    ]
    (tmp_path / "sentinel.txt").write_text("original")

    await _run_parent(tmp_path, script, parent)

    assert (tmp_path / "sentinel.txt").read_text() == "original"


async def test_a_parent_path_deny_and_tool_deny_hold_in_the_child(tmp_path):
    parent = PermissionEngine(
        mode="smart",
        path_rules={"deny": ["secret.txt"]},
        tool_rules={"multi_edit": "deny"},
    )
    (tmp_path / "keep.txt").write_text("a\n")
    script = [
        _delegate("harbor"),
        _call("write_file", "w1", path="secret.txt", content="leak"),
        _call(
            "multi_edit",
            "m1",
            path="keep.txt",
            edits=[{"old_string": "a", "new_string": "b"}],
        ),
        _done("d1"),
        _done("d2"),
    ]

    await _run_parent(tmp_path, script, parent)

    assert not (tmp_path / "secret.txt").exists()
    assert (tmp_path / "keep.txt").read_text() == "a\n"


async def test_a_parent_ask_beats_a_childs_allow_prefix(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "fast.yaml").write_text(
        "name: fast\npermission_mode: smart\ntools: [bash, task_complete]\n"
        "bash_rules:\n  allow_prefixes: [touch]\n"
    )
    approvals = _Approvals(False)
    parent = PermissionEngine(mode="smart", bash_rules={"ask": [r"^touch "]}, approval_handler=approvals)
    script = [
        _delegate("fast"),
        _call("bash", "b1", command="touch marker"),
        _done("d1"),
        _done("d2"),
    ]

    await _run_parent(tmp_path, script, parent, agents_dir=agents)

    assert len(approvals.asked) == 1
    assert not (tmp_path / "marker").exists()


async def test_the_root_ceiling_holds_for_a_grandchild(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "relay.yaml").write_text(
        "name: relay\npermission_mode: yolo\ntools: [invoke_subagent, bash, task_complete]\n"
    )
    approvals = _Approvals(False)
    parent = PermissionEngine(mode="smart", bash_rules={"ask": [r"^touch "]}, approval_handler=approvals)
    script = [
        _delegate("relay"),
        _delegate("harbor", "s2"),
        _call("bash", "b1", command="touch marker"),
        _done("d1"),
        _done("d2"),
        _done("d3"),
    ]

    await _run_parent(tmp_path, script, parent, agents_dir=agents)

    assert len(approvals.asked) == 1
    assert not (tmp_path / "marker").exists()


async def test_the_runner_enforces_the_ceiling_without_the_tool_schema(tmp_path):
    """Entering the runner directly, not through ``invoke_subagent``, is still capped."""
    approvals = _Approvals(False)
    parent = PermissionEngine(mode="smart", bash_rules={"ask": [r"^touch "]}, approval_handler=approvals)
    runner = SubagentRunner(
        model=ScriptModel([_call("bash", "b1", command="touch marker"), _done()]),
        env=LocalEnvironment(workspace_root=tmp_path),
        events=EventStore(),
        workspace_root=str(tmp_path),
        approval_handler=approvals,
        parent_permissions=parent,
        parent_tools=default_tools(),
    )

    await runner.run("harbor", "do it")

    assert len(approvals.asked) == 1
    assert not (tmp_path / "marker").exists()


async def test_a_permitted_child_call_still_runs(tmp_path):
    """The ceiling narrows; it does not refuse an otherwise usable child."""
    parent = PermissionEngine(mode="smart")

    await _run_parent(tmp_path, _touch_child_script("marker"), parent)

    assert (tmp_path / "marker").exists()


async def test_constructing_a_child_never_starts_its_own_mcp_server(tmp_path):
    marker = tmp_path / "mcp-started"
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers": {"sentinel": {"command": "sh", "args": ["-c", "touch %s"]}}}' % marker
    )
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "withmcp.yaml").write_text(
        f"name: withmcp\npermission_mode: smart\nmcp_config_path: {config}\n"
        "tools: [bash, task_complete]\n"
    )
    script = [_delegate("withmcp"), _done("d1"), _done("d2")]

    await _run_parent(tmp_path, script, PermissionEngine(mode="smart"), agents_dir=agents)

    assert not marker.exists()


async def test_a_child_cannot_gain_a_tool_its_parent_lacks(tmp_path):
    """``harbor`` lists write tools; a parent admitted without them cannot hand them on."""
    from garuda.tools import tools_for_names

    runner = SubagentRunner(
        model=ScriptModel(
            [_call("write_file", "w1", path="new.txt", content="x"), _done()]
        ),
        env=LocalEnvironment(workspace_root=tmp_path),
        events=EventStore(),
        workspace_root=str(tmp_path),
        parent_permissions=PermissionEngine(mode="yolo"),
        parent_tools=tools_for_names(["read_file", "bash", "task_complete"]),
    )

    await runner.run("harbor", "write a file")

    assert not (tmp_path / "new.txt").exists()


def test_a_delegated_engine_reports_the_stricter_mode():
    engine = DelegatedPermissionEngine(PermissionEngine(mode="yolo"), PermissionEngine(mode="readonly"))
    assert engine.mode == "readonly"
    assert engine.check_tool("write_file") is PermissionDecision.DENY
