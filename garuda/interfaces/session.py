"""Shared agent session state for multi-turn CLI and SDK conversations."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from garuda.agents.loader import AgentProfile
from garuda.agents.setup import PreparedNativeRun, prepare_agent_run
from garuda.context.manager import ContextManager
from garuda.core.events import EventStore
from garuda.core.run_state import reserved_output_tokens
from garuda.model.config import CollectionPolicy, ModelBindings, ResolvedField
from garuda.types import AgentConfig, Message, Role

ApprovalHandler = Callable[[str], Awaitable[bool]]


@dataclass
class AgentSession:
    """Holds tools, permissions, events, and conversation context across turns."""

    profile: AgentProfile
    config: AgentConfig
    model: Any
    permissions: Any
    tools: list
    agent: object
    events: EventStore = field(default_factory=EventStore)
    mcp_manager: object | None = None
    agents_dir: Path | list[Path] | None = None
    context: ContextManager | None = None
    bindings: ModelBindings | None = None
    provenance: dict[str, ResolvedField] = field(default_factory=dict)
    collection: Any | None = None
    collection_policy: CollectionPolicy | None = None

    @classmethod
    async def create(
        cls,
        *,
        agent_name: str,
        model: str | Any | None = None,
        workspace: str,
        agents_dir: Path | list[Path] | None = None,
        mcp_config_path: str | None = None,
        mode: str | None = None,
        permission_mode: str | None = None,
        approval_handler: ApprovalHandler | None = None,
        workspace_kind: str = "local",
        docker_image: str = "ubuntu:22.04",
        docker_host: str | None = None,
        reasoning_model: str | Any | None = None,
        collection_model: str | Any | None = None,
        no_collection: bool = False,
        model_binding: str | None = None,
    ) -> "AgentSession":
        from garuda.config.agent_home import resolve_agents_dirs

        # Resolved (not just stored) so forked subagents search the same
        # `.agent/agents` then `.garuda/agents` roots; idempotent with setup.
        agents_dir = resolve_agents_dirs(workspace, agents_dir)
        prepared: PreparedNativeRun = await prepare_agent_run(
            agent_name,
            workspace=workspace,
            agents_dir=agents_dir,
            mcp_config_path=mcp_config_path,
            mode=mode,
            permission_mode=permission_mode,
            approval_handler=approval_handler,
            model=model,
            reasoning_model=reasoning_model,
            collection_model=collection_model,
            no_collection=no_collection,
            model_binding=model_binding,
        )
        prepared.config.workspace_kind = workspace_kind
        prepared.config.docker_image = docker_image
        prepared.config.docker_host = docker_host
        return cls(
            profile=prepared.profile,
            config=prepared.config,
            model=prepared.reasoning,
            permissions=prepared.permissions,
            tools=prepared.tools,
            mcp_manager=prepared.mcp_manager,
            agents_dir=agents_dir,
            agent=prepared.agent,
            bindings=prepared.bindings,
            provenance=prepared.provenance,
            collection=prepared.collection,
            collection_policy=prepared.collection_policy,
        )

    def prepare_context(self, task: str) -> ContextManager:
        """Seed or extend the shared LLM context for a new user turn."""
        if self.context is None:
            self.context = ContextManager(
                model=self.model,
                max_output_bytes=self.config.max_output_bytes,
                proactive_threshold=self.config.proactive_summarize_threshold,
                max_context_tokens=self.config.max_context_tokens,
                enable_three_step_summary=self.config.enable_three_step_summary,
                task=task,
                reserved_output_tokens=reserved_output_tokens(self.config),
                safety_margin_tokens=self.config.context_safety_margin_tokens,
                adaptive_output=self.config.enable_adaptive_output,
                min_output_bytes=self.config.min_output_bytes,
            )
            self.context.seed(
                [
                    Message(role=Role.SYSTEM, content=self.config.system_prompt or ""),
                    Message(role=Role.USER, content=task),
                ]
            )
        else:
            self.context.append(Message(role=Role.USER, content=task))
        return self.context

    async def close(self) -> None:
        if self.mcp_manager is not None:
            await self.mcp_manager.close()
            self.mcp_manager = None
