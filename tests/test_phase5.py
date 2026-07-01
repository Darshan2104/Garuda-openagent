import json
from pathlib import Path

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.eval.atif_export import events_to_atif, save_atif_trajectory
from garuda.eval.harbor_environment import HarborEnvironmentAdapter
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment

harbor = pytest.importorskip("harbor")
from harbor.models.trajectories import Trajectory  # noqa: E402
from harbor.utils.trajectory_validator import TrajectoryValidator  # noqa: E402


class MockHarborExecResult:
    def __init__(self, stdout: str = "", stderr: str = "", return_code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code


class MockHarborEnvironment:
    def __init__(self, workdir: str = "/workspace"):
        self.task_env_config = type("Cfg", (), {"workdir": workdir})()
        self._files: dict[str, str] = {}
        self.uploaded: list[tuple[str, str]] = []

    async def exec(self, command: str, cwd: str | None = None, timeout_sec: int | None = None, **kwargs):
        if command == "pwd":
            return MockHarborExecResult(stdout=self.task_env_config.workdir + "\n", return_code=0)
        if command.startswith("cat "):
            path = command[4:].strip().strip("'\"")
            if path in self._files:
                return MockHarborExecResult(stdout=self._files[path], return_code=0)
            return MockHarborExecResult(stderr="No such file", return_code=1)
        if command.startswith("mkdir -p "):
            return MockHarborExecResult(return_code=0)
        return MockHarborExecResult(stdout="", return_code=0)

    async def upload_file(self, source_path: str, target_path: str) -> None:
        content = Path(source_path).read_text(encoding="utf-8")
        self._files[target_path] = content
        self.uploaded.append((source_path, target_path))


def test_events_to_atif_validates():
    events = [
        {
            "type": "session_start",
            "timestamp": "2026-07-01T00:00:00+00:00",
            "session_id": "sess-1",
            "payload": {"task": "List files", "model": "script/test"},
        },
        {
            "type": "user_message",
            "timestamp": "2026-07-01T00:00:01+00:00",
            "session_id": "sess-1",
            "payload": {"content": "List files"},
        },
        {
            "type": "model_response",
            "timestamp": "2026-07-01T00:00:02+00:00",
            "session_id": "sess-1",
            "payload": {
                "content": "I'll list files.",
                "tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}],
            },
        },
        {
            "type": "tool_result",
            "timestamp": "2026-07-01T00:00:03+00:00",
            "session_id": "sess-1",
            "payload": {"name": "bash", "content": "a.txt\n", "is_error": False},
        },
        {
            "type": "session_end",
            "timestamp": "2026-07-01T00:00:04+00:00",
            "session_id": "sess-1",
            "payload": {"success": True, "turns": 1},
        },
    ]

    trajectory_dict = events_to_atif(
        events,
        session_id="sess-1",
        model_name="script/test",
        prompt_tokens=10,
        completion_tokens=5,
    )
    trajectory = Trajectory.model_validate(trajectory_dict)
    validator = TrajectoryValidator()
    assert validator.validate(trajectory.to_json_dict())
    assert trajectory.steps[0].source == "user"
    assert any(step.tool_calls for step in trajectory.steps if step.source == "agent")


def test_save_atif_trajectory(tmp_path):
    trajectory = events_to_atif([], session_id="empty", instruction="noop")
    path = tmp_path / "traj.json"
    save_atif_trajectory(path, trajectory)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == "ATIF-v1.7"


@pytest.mark.asyncio
async def test_harbor_environment_adapter_read_write():
    harbor_env = MockHarborEnvironment(workdir="/workspace")
    adapter = HarborEnvironmentAdapter(harbor_env)
    await adapter.resolve_workspace_root()
    assert adapter.workspace_root == "/workspace"

    await adapter.write_file("notes.txt", "hello harbor")
    content = await adapter.read_file("notes.txt")
    assert content == "hello harbor"

    result = await adapter.execute("echo ok")
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_default_agent_exports_atif_compatible_events(tmp_path):
    events = EventStore(session_id="integration-1")
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="write_file", arguments={"path": "out.txt", "content": "done"})
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="2", name="task_complete", arguments={"summary": "Wrote out.txt"})
                ],
            ),
        ]
    )
    agent = DefaultAgent(profile_name="harbor")
    result = await agent.run(
        task="write a file",
        model=model,
        env=env,
        tools=tools_for_names(["write_file", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=True, permission_mode="yolo"),
        events=events,
    )
    assert result.success

    trajectory_dict = events_to_atif(
        events.get_all(),
        session_id=events.session_id,
        instruction="write a file",
    )
    trajectory = Trajectory.model_validate(trajectory_dict)
    assert len(trajectory.steps) >= 2


@pytest.mark.asyncio
async def test_garuda_harbor_agent_run(tmp_path):
    from garuda.eval.harbor_adapter import GarudaHarborAgent

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    agent = GarudaHarborAgent(
        logs_dir=logs_dir,
        model_name="script/test",
        agent_profile="harbor",
        permission_mode="yolo",
        max_turns=5,
    )

    harbor_env = MockHarborEnvironment(workdir=str(tmp_path))
    context = harbor.models.agent.context.AgentContext()

    class ScriptHarborModel:
        def __init__(self):
            self._script = ScriptModel(
                responses=[
                    ModelResponse(
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="1",
                                name="write_file",
                                arguments={"path": "harbor.txt", "content": "from garuda"},
                            )
                        ],
                    ),
                    ModelResponse(
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="2",
                                name="task_complete",
                                arguments={"summary": "Created harbor.txt"},
                            )
                        ],
                    ),
                ]
            )
            self.prompt_tokens = 0
            self.completion_tokens = 0

        @property
        def model_name(self):
            return "script/test"

        @property
        def supports_tool_calling(self):
            return True

        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            response = await self._script.complete(messages, tools=tools)
            self.prompt_tokens += response.usage.get("prompt_tokens", 0)
            self.completion_tokens += response.usage.get("completion_tokens", 0)
            return response

        def count_tokens(self, messages):
            return self._script.count_tokens(messages)

    import garuda.eval.harbor_adapter as adapter_module

    original = adapter_module.LitellmModel
    adapter_module.LitellmModel = lambda model_name, **kwargs: ScriptHarborModel()  # type: ignore[assignment, return-value]
    try:
        await agent.run("write harbor.txt", harbor_env, context)
    finally:
        adapter_module.LitellmModel = original

    trajectory_path = logs_dir / "trajectory.json"
    assert trajectory_path.exists()
    trajectory = Trajectory.model_validate_json(trajectory_path.read_text(encoding="utf-8"))
    assert trajectory.agent.name == "garuda"
    assert harbor_env._files[f"{tmp_path}/harbor.txt"] == "from garuda"
    assert context.metadata and context.metadata.get("success") is True
