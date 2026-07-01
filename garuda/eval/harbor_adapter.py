"""Harbor ``BaseAgent`` implementation that runs Garuda's DefaultAgent."""

import tempfile
from pathlib import Path
from typing import Any, override

import yaml

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.eval.atif_export import events_to_atif, save_atif_trajectory
from garuda.eval.harbor_environment import HarborEnvironmentAdapter
from garuda.model.litellm_model import LitellmModel
from garuda.model.protocol import ModelResponse
from garuda.tools import build_toolkit
from garuda.types import AgentConfig, Message

try:
    from harbor.agents.base import BaseAgent
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext
    from harbor.models.trajectories import Trajectory
    from harbor.utils.trajectory_utils import format_trajectory_json

    HARBOR_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when eval extra not installed
    BaseAgent = object  # type: ignore[misc, assignment]
    BaseEnvironment = object  # type: ignore[misc, assignment]
    AgentContext = object  # type: ignore[misc, assignment]
    HARBOR_AVAILABLE = False


class _UsageTrackingModel:
    """Accumulate LiteLLM usage across a Harbor trial run."""

    def __init__(self, inner: LitellmModel):
        self._inner = inner
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def supports_tool_calling(self) -> bool:
        return self._inner.supports_tool_calling

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        response = await self._inner.complete(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.prompt_tokens += response.usage.get("prompt_tokens", 0)
        self.completion_tokens += response.usage.get("completion_tokens", 0)
        return response

    def count_tokens(self, messages: list[Message]) -> int:
        return self._inner.count_tokens(messages)


class GarudaHarborAgent(BaseAgent):
    """Run Garuda inside Harbor benchmarks with ATIF trajectory export."""

    SUPPORTS_ATIF: bool = True

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        agent_profile: str = "harbor",
        max_turns: int | None = None,
        permission_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        if not HARBOR_AVAILABLE:
            raise ImportError(
                "Harbor is required for GarudaHarborAgent. Install with: pip install 'garuda-openagent[eval]'"
            )
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self._agent_profile = agent_profile
        self._max_turns = max_turns
        self._permission_mode = permission_mode

    @staticmethod
    @override
    def name() -> str:
        return "garuda"

    @override
    def version(self) -> str | None:
        from importlib.metadata import version

        try:
            return version("garuda-openagent")
        except Exception:
            return "0.5.0"

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        adapter = HarborEnvironmentAdapter(environment)
        await adapter.resolve_workspace_root()

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        adapter = HarborEnvironmentAdapter(environment)
        await adapter.resolve_workspace_root()

        profile = load_profile(self._agent_profile)
        config = profile.to_agent_config()
        config.enable_verifier = True
        config.workspace_kind = "local"
        if self._max_turns is not None:
            config.max_turns = self._max_turns
        if self._permission_mode:
            config.permission_mode = self._permission_mode

        if not self.model_name:
            raise ValueError("GarudaHarborAgent requires --model (provider/model_name)")

        model = _UsageTrackingModel(LitellmModel(model_name=self.model_name))
        permissions = PermissionEngine(mode=config.permission_mode, tool_rules=profile.tool_rules)
        events = EventStore()
        agent = DefaultAgent(profile_name=profile.name)

        mcp_path = await self._write_mcp_config()
        tools, mcp_manager = await build_toolkit(profile.tools, mcp_path)

        result = await agent.run(
            task=instruction,
            model=model,
            env=adapter,
            tools=tools,
            config=config,
            events=events,
            permissions=permissions,
        )

        if mcp_manager is not None:
            await mcp_manager.close()

        trajectory_dict = events_to_atif(
            events.get_all(),
            session_id=events.session_id,
            agent_name=self.name(),
            agent_version=self.version() or "unknown",
            model_name=self.model_name,
            instruction=instruction,
            prompt_tokens=model.prompt_tokens or None,
            completion_tokens=model.completion_tokens or None,
        )

        trajectory_path = self.logs_dir / "trajectory.json"
        trajectory = Trajectory.model_validate(trajectory_dict)
        trajectory_path.write_text(
            format_trajectory_json(trajectory.to_json_dict()),
            encoding="utf-8",
        )
        events.save(self.logs_dir / "events.jsonl")

        context.n_input_tokens = model.prompt_tokens
        context.n_output_tokens = model.completion_tokens
        context.metadata = {
            "success": result.success,
            "turns": result.turns,
            "session_id": events.session_id,
        }

    async def _write_mcp_config(self) -> str | None:
        if not self.mcp_servers:
            return None
        servers = []
        for server in self.mcp_servers:
            if server.transport != "stdio":
                self.logger.warning(
                    "Skipping non-stdio MCP server %s (transport=%s)",
                    server.name,
                    server.transport,
                )
                continue
            servers.append(
                {
                    "name": server.name,
                    "transport": "stdio",
                    "command": server.command,
                    "args": server.args,
                }
            )
        if not servers:
            return None
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
        yaml.safe_dump({"servers": servers}, handle)
        handle.close()
        return handle.name

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        trajectory_path = self.logs_dir / "trajectory.json"
        if not trajectory_path.exists():
            return
        trajectory = Trajectory.model_validate_json(trajectory_path.read_text(encoding="utf-8"))
        if trajectory.final_metrics:
            metrics = trajectory.final_metrics
            context.n_input_tokens = metrics.total_prompt_tokens or context.n_input_tokens
            context.n_output_tokens = metrics.total_completion_tokens or context.n_output_tokens
            context.n_cache_tokens = metrics.total_cached_tokens
            context.cost_usd = metrics.total_cost_usd


def export_atif_from_events(
    events: EventStore,
    output_path: str | Path,
    *,
    model_name: str | None = None,
    instruction: str | None = None,
) -> Path:
    """Helper for CLI/tests: export an EventStore to an ATIF JSON file."""
    trajectory = events_to_atif(
        events.get_all(),
        session_id=events.session_id,
        model_name=model_name,
        instruction=instruction,
    )
    target = Path(output_path)
    save_atif_trajectory(target, trajectory)
    return target
