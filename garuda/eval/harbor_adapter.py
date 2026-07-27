"""Harbor ``BaseAgent`` implementation that runs Garuda's DefaultAgent."""

import logging
import tempfile
from pathlib import Path
from typing import Any, override

import yaml

from garuda.agents.setup import prepare_agent_run
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.eval.atif_export import events_to_atif, save_atif_trajectory
from garuda.eval.harbor_environment import HarborEnvironmentAdapter
from garuda.model.litellm_model import LitellmModel
from garuda.model.protocol import ModelResponse
from garuda.tools import build_toolkit
from garuda.types import Message

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

logger = logging.getLogger(__name__)

# Fraction of the harness timeout held back so the agent's own wind-down (final
# verification, writing the answer) happens before the external kill, not after.
DEFAULT_DEADLINE_MARGIN = 0.1


class _UsageTrackingModel:
    """Accumulate LiteLLM usage across a Harbor trial run."""

    def __init__(self, inner: LitellmModel):
        self._inner = inner
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        # Only meaningful when the provider reports per-call cost; stays None
        # otherwise so the trajectory falls back to an estimate rather than
        # publishing a partial sum as if it were the bill.
        self.provider_cost_usd: float | None = None

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
        self.cached_tokens += response.usage.get("cache_read_tokens", 0)
        call_cost = response.usage.get("cost_usd")
        if isinstance(call_cost, (int, float)):
            self.provider_cost_usd = (self.provider_cost_usd or 0.0) + float(call_cost)
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
        agent_timeout_sec: float | None = None,
        deadline_margin: float = DEFAULT_DEADLINE_MARGIN,
        system_prompt: str | None = None,
        system_prompt_path: str | None = None,
        append_system_prompt: str | None = None,
        agents_dir: str | list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if not HARBOR_AVAILABLE:
            raise ImportError(
                "Harbor is required for GarudaHarborAgent. Install with: pip install 'garuda-openagent[eval]'"
            )
        if system_prompt is not None and system_prompt_path is not None:
            raise ValueError(
                "Pass system_prompt or system_prompt_path, not both — one would silently "
                "shadow the other."
            )
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self._agent_profile = agent_profile
        self._max_turns = max_turns
        self._permission_mode = permission_mode
        self._agent_timeout_sec = agent_timeout_sec
        self._deadline_margin = deadline_margin
        self._system_prompt = system_prompt
        self._system_prompt_path = system_prompt_path
        self._append_system_prompt = append_system_prompt
        self._agents_dir = agents_dir

    def _resolved_agents_dirs(self) -> list[Path] | None:
        """Extra profile directories, so `agent_profile` can name a custom YAML."""
        if not self._agents_dir:
            return None
        raw = [self._agents_dir] if isinstance(self._agents_dir, str) else list(self._agents_dir)
        return [Path(d) for d in raw]

    def _base_system_prompt(self, profile: Any) -> str | None:
        """The profile's base prompt with this run's overrides applied, or None.

        Returns None when nothing was configured, so the caller leaves the prompt
        `prepare_agent_run` already resolved untouched.

        Overrides replace the *base* prompt rather than the resolved one:
        `resolve_system_prompt` appends the discovered-skills block and the project
        memory block, and overwriting `config.system_prompt` afterwards would throw
        both away without saying so.
        """
        base: str | None = None
        if self._system_prompt is not None:
            base = self._system_prompt
        elif self._system_prompt_path is not None:
            path = Path(self._system_prompt_path).expanduser()
            try:
                base = path.read_text(encoding="utf-8")
            except OSError as exc:
                # Silently falling back to the profile's prompt would mean scoring a
                # run that did not use the prompt under test.
                raise ValueError(f"Cannot read system_prompt_path {path}: {exc}") from exc

        if self._append_system_prompt:
            from garuda.types import DEFAULT_SYSTEM_PROMPT

            current = base if base is not None else (profile.system_prompt or DEFAULT_SYSTEM_PROMPT)
            base = f"{current.rstrip()}\n\n{self._append_system_prompt.strip()}"

        return base

    def _resolved_deadline_sec(self) -> float | None:
        """The agent's own wall-clock budget, or None to stay turn-bounded.

        Harbor's ``override_timeout_sec`` lives in the job config and never
        reaches the agent, so the value is supplied through the agent's own
        ``kwargs`` (``agent_timeout_sec``). Set it to the same number as
        ``override_timeout_sec``; a margin is subtracted here.
        """
        timeout = self._agent_timeout_sec
        if timeout is None:
            return None
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            logger.warning("Ignoring non-numeric agent_timeout_sec=%r", self._agent_timeout_sec)
            return None
        if timeout <= 0:
            logger.warning("Ignoring non-positive agent_timeout_sec=%r", timeout)
            return None
        margin = self._deadline_margin
        if not isinstance(margin, (int, float)) or not 0 <= margin < 1:
            logger.warning("Ignoring out-of-range deadline_margin=%r", margin)
            margin = DEFAULT_DEADLINE_MARGIN
        return timeout * (1.0 - margin)

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
        # Checked before any resource is acquired, so a misconfigured run can't
        # leak the MCP manager prepare_agent_run would have started.
        if not self.model_name:
            raise ValueError("GarudaHarborAgent requires --model (provider/model_name)")

        adapter = HarborEnvironmentAdapter(environment)
        workspace_root = await adapter.resolve_workspace_root()

        # `eval` mode: the full completion-gate stack (LLM judge, acceptance
        # contract, discriminating + stable evidence, side-effect sweep) on the
        # single-agent loop. Pinned explicitly rather than inherited, because the
        # default posture is `interactive` — a benchmark that silently ran without
        # its gates would report numbers no one could reproduce. The plan→execute→
        # critic (`rigorous`) mode is still deliberately *not* used: it multiplies
        # turns and cost and isn't what we're scoring, so a `mode: rigorous` line
        # in the profile is overridden here.
        profile, config, permissions, tools, agent, mcp_manager = await prepare_agent_run(
            self._agent_profile,
            workspace=workspace_root,
            agents_dir=self._resolved_agents_dirs(),
            mode="eval",
        )
        # Prompt is a benchmark variable, not an agent property: measuring how much
        # of a score is scaffold vs instruction needs the prompt swappable per job,
        # without editing a profile the rest of the system shares.
        base_prompt = self._base_system_prompt(profile)
        if base_prompt is not None:
            from garuda.agents.loader import resolve_system_prompt

            profile.system_prompt = base_prompt
            config.system_prompt = resolve_system_prompt(profile, workspace_root)
        config.enable_verifier = True
        config.workspace_kind = "local"
        if self._max_turns is not None:
            config.max_turns = self._max_turns
        # Harbor enforces `override_timeout_sec` by killing the agent from the
        # outside and tells it nothing (AgentContext is output-only), so without
        # this the agent has no wall-clock awareness: it cannot pace itself, wind
        # down, or write a partial answer before the kill lands. The margin leaves
        # room for its own wind-down to run first.
        deadline = self._resolved_deadline_sec()
        if deadline is not None:
            config.deadline_sec = deadline
        if self._permission_mode:
            config.permission_mode = self._permission_mode
            permissions = PermissionEngine(
                mode=config.permission_mode,
                tool_rules=profile.tool_rules,
                path_rules=profile.path_rules,
                bash_rules=profile.bash_rules,
            )

        model = _UsageTrackingModel(LitellmModel(model_name=self.model_name))
        events = EventStore()

        result = None
        mcp_path: str | None = None
        try:
            mcp_path = await self._write_mcp_config()
            if mcp_path:
                # build_toolkit spawns its own manager; close the one
                # prepare_agent_run already started or its stdio subprocesses leak
                # for the rest of the eval run.
                if mcp_manager is not None:
                    await mcp_manager.close()
                    mcp_manager = None
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
        finally:
            # Teardown and trajectory persistence must survive a failed run: a
            # crashed task is exactly when the transcript is most valuable, and
            # previously any exception here left no trajectory.json at all.
            if mcp_manager is not None:
                try:
                    await mcp_manager.close()
                except Exception:
                    logger.warning("MCP manager close failed", exc_info=True)
            if mcp_path:
                Path(mcp_path).unlink(missing_ok=True)
            try:
                self._persist_trajectory(events, model, instruction)
            except Exception:
                logger.warning("Trajectory persistence failed", exc_info=True)

            context.n_input_tokens = model.prompt_tokens
            context.n_output_tokens = model.completion_tokens
            context.n_cache_tokens = model.cached_tokens or None
            if model.provider_cost_usd is not None:
                context.cost_usd = round(model.provider_cost_usd, 8)
            context.metadata = {
                "success": result.success if result is not None else False,
                "turns": result.turns if result is not None else 0,
                "session_id": events.session_id,
            }

    def _persist_trajectory(
        self,
        events: EventStore,
        model: "_UsageTrackingModel",
        instruction: str,
    ) -> None:
        """Write trajectory.json + events.jsonl for this trial."""
        trajectory_dict = events_to_atif(
            events.get_all(),
            session_id=events.session_id,
            agent_name=self.name(),
            agent_version=self.version() or "unknown",
            model_name=self.model_name,
            instruction=instruction,
            prompt_tokens=model.prompt_tokens or None,
            completion_tokens=model.completion_tokens or None,
            cost_usd=model.provider_cost_usd,
        )
        trajectory = Trajectory.model_validate(trajectory_dict)
        (self.logs_dir / "trajectory.json").write_text(
            format_trajectory_json(trajectory.to_json_dict()),
            encoding="utf-8",
        )
        events.save(self.logs_dir / "events.jsonl")

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
