"""Run setup and the state a run carries.

Split out of ``core/loop.py``, where this was ~140 lines of preamble followed by
twenty loose locals threaded through helper methods by hand. ``prepare_run`` does
the assembly; ``RunState`` is what the turn loop reads and writes.

Nothing here decides anything about a turn. If a change belongs to *driving* the
loop it goes in ``loop.py``; if it belongs to *setting up* the run, here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from garuda.context.manager import ContextManager
from garuda.core.action_memo import ActionMemo
from garuda.core.completion import CompletionGate
from garuda.core.events import EventStore, EventType
from garuda.core.permissions import PermissionEngine
from garuda.core.side_effects import SideEffectLedger
from garuda.core.steering import Steering
from garuda.core.tool_runner import ToolRunner
from garuda.plugins.hooks import HookRegistry
from garuda.tools.protocol import Tool, ToolContext
from garuda.types import (
    DEFAULT_SYSTEM_PROMPT,
    AgentConfig,
    AgentResult,
    Message,
    Role,
)
from garuda.workspace.health import EnvironmentUnavailableError, monitored
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)


@dataclass
class RunState:
    """Everything one run of the loop needs, assembled once by :func:`prepare_run`."""

    task: str
    config: AgentConfig
    events: EventStore
    context: ContextManager
    tools: list[Tool]
    tool_map: dict[str, Tool]
    runner: ToolRunner
    completion: CompletionGate
    steering: Steering
    memo: ActionMemo
    ledger: SideEffectLedger
    emit_session_events: bool
    checkpoint: object | None = None

    usage_totals: dict[str, int] = field(default_factory=dict)
    final_message: str = ""
    # The last substantive thing the model said. After a forced final turn this
    # usually holds the best answer the run produced, so an exhausted run reports
    # it rather than a bare "max turns exceeded" that discards real work.
    last_assistant_text: str = ""

    def accumulate_usage(self, usage: dict[str, int]) -> None:
        for key, value in (usage or {}).items():
            if key == "cost_usd":
                # Provider-reported spend: a float, and summed as one. Coercing it to
                # int here would silently round every sub-dollar call to zero.
                self.usage_totals[key] = self.usage_totals.get(key, 0.0) + float(value)
                continue
            self.usage_totals[key] = self.usage_totals.get(key, 0) + int(value)

    def result(self, success: bool, final_message: str, turns: int) -> AgentResult:
        """Build the AgentResult, surfacing how the run behaved in metadata.

        Repetition, cleanup, and which stated requirements were actually
        established are on the result so a caller can see them without replaying
        the event log.
        """
        metadata: dict = {
            "session_id": self.events.session_id,
            "events": self.events.get_all(),
            "usage": dict(self.usage_totals),
            "action_memo": self.memo.stats(),
            "side_effects": self.ledger.summary(),
        }
        contract = self.completion.contract
        if contract is not None:
            metadata["acceptance"] = {
                **contract.stats(),
                "criteria": contract.to_dict()["criteria"],
            }
        return AgentResult(
            success=success,
            final_message=final_message,
            messages=self.context.get_messages(),
            turns=turns,
            metadata=metadata,
        )

    def bare_result(self, success: bool, final_message: str, turns: int) -> AgentResult:
        """Result without run-behaviour metadata, for failures that happened before
        the run produced any: a model error or a dead workspace."""
        return AgentResult(
            success=success,
            final_message=final_message,
            messages=self.context.get_messages(),
            turns=turns,
            metadata={
                "session_id": self.events.session_id,
                "events": self.events.get_all(),
                "usage": dict(self.usage_totals),
            },
        )

    def abort_environment_dead(
        self, exc: EnvironmentUnavailableError, turn: int
    ) -> AgentResult:
        """End the run immediately when the workspace is gone.

        Deliberately reports failure rather than letting the model summarise: a
        run that cannot observe its own workspace has no basis for claiming the
        task succeeded, however much work happened before the environment died.
        """
        logger.error("Aborting run at turn %d: %s", turn, exc)
        self.events.append(
            EventType.ENVIRONMENT_UNAVAILABLE,
            {"turn": turn, "reason": exc.reason, "detail": exc.detail},
        )
        if self.emit_session_events:
            self.events.append(
                EventType.SESSION_END,
                {"success": False, "reason": "environment_unavailable", "detail": exc.reason},
            )
        return self.bare_result(
            False,
            (
                f"Run aborted at turn {turn}: the workspace became unavailable "
                f"({exc.reason}). Work completed before this point could not be verified."
            ),
            turn,
        )

    def save_checkpoint(self) -> None:
        """Persist the conversation so a crash/kill mid-run is still resumable from
        the last completed turn. Best-effort; never breaks the run."""
        if self.checkpoint is None:
            return
        try:
            self.checkpoint(self.context.get_messages())
        except Exception:
            logger.warning("Session checkpoint failed", exc_info=True)

    def reinject_pinned_state(self) -> None:
        """Re-pin this run's goal, todos and acceptance criteria after a compaction."""
        reinject_pinned_state(
            self.context,
            self.tool_map,
            self.events.session_id,
            contract=self.completion.contract,
        )


def reinject_pinned_state(
    context: ContextManager,
    tool_map: dict[str, Tool],
    session_id: str,
    contract=None,
) -> None:
    """Re-surface the current goal, todo list and acceptance criteria as compact
    messages after a compaction, so they survive summarization. Safe at the top
    of a turn (the prior turn's tool-result block is already complete). No-op
    when unset.

    A free function, not just a ``RunState`` method: it depends on nothing but its
    arguments, and keeping it callable without a whole run assembled is what makes
    it directly testable.
    """
    if contract is not None and contract.criteria:
        # The criteria are what the completion gate checks; losing them to
        # compaction is how a long run forgets what it was asked for.
        context.append(
            Message(
                role=Role.USER,
                content=(
                    f"[acceptance criteria — retained across compaction]\n{contract.render()}"
                ),
            )
        )
    goal_tool = tool_map.get("update_goal")
    if goal_tool is not None:
        goal = goal_tool.get_goal(session_id)
        if goal:
            context.append(
                Message(
                    role=Role.USER,
                    content=f"[current goal — retained across compaction]\n{goal}",
                )
            )
    todo_tool = tool_map.get("todo")
    if todo_tool is not None:
        todos = todo_tool.get_todos(session_id)
        if todos:
            from garuda.tools.todo import render_todos

            context.append(
                Message(
                    role=Role.USER,
                    content=f"[todo list — retained across compaction]\n{render_todos(todos)}",
                )
            )


def _filter_tools(tools: list[Tool], allowed_names: list[str]) -> list[Tool]:
    """Restrict the toolset to the profile's list, keeping what a run cannot work without."""
    allowed = set(allowed_names)
    # Always keep task_complete (a verifier-gated run can never finish without
    # it), the lazy-discovery meta-tools (they replace the MCP tool list), and
    # MCP tools (their names aren't in the static profile list).
    allowed.add("task_complete")
    allowed.add("search_tool")
    allowed.add("use_tool")
    for tool in tools:
        if tool.name.startswith("mcp__"):
            allowed.add(tool.name)
    # Filter in the original order so the tool-schema sequence is deterministic
    # across runs (a stable prefix keeps prompt caching warm).
    return [t for t in tools if t.name in allowed]


async def prepare_run(
    *,
    task: str,
    profile_name: str,
    model,
    env: Environment,
    tools: list[Tool],
    config: AgentConfig | None,
    events: EventStore | None,
    permissions: PermissionEngine | None,
    hooks: HookRegistry | None,
    subagent_runner,
    agents_dir,
    context: ContextManager | None,
    checkpoint,
    buffer,
    emit_session_events: bool,
) -> RunState:
    """Assemble everything a run needs and return the state the loop drives."""
    config = config or AgentConfig()
    events = events or EventStore()
    permissions = permissions or PermissionEngine(mode=config.permission_mode)
    hooks = hooks or HookRegistry()
    # Every environment touch from here on is screened for transport-level
    # death, so a vanished container aborts the run instead of producing a
    # long tail of failing calls that the model mistakes for task failures.
    env = monitored(env)
    # Inner runs (rigorous plan/executor attempts, subagents) share the parent's
    # event store; suppressing their session_start/end avoids nested/duplicate
    # SESSION_END success events that confuse trajectory consumers.
    if emit_session_events:
        events.append(EventType.SESSION_START, {"task": task, "model": model.model_name})

    if config.allowed_tools:
        tools = _filter_tools(tools, config.allowed_tools)
    tool_map = {tool.name: tool for tool in tools}

    # A caller (e.g. a forked subagent) may pass its parent's buffer so inherited
    # [buffer:...] stubs resolve; otherwise create one for this session. Created
    # before the context manager so condensation can demote pruned/dropped
    # history into it instead of destroying it.
    if buffer is None and config.buffer_tool_output:
        from garuda.core.buffer import ToolOutputBuffer

        buffer = ToolOutputBuffer(
            session_id=events.session_id,
            threshold_bytes=config.buffer_threshold_bytes,
        )

    if context is None:
        system_prompt = config.system_prompt or DEFAULT_SYSTEM_PROMPT
        # Probe the environment once and fold the snapshot into the first-turn
        # system prompt so the model skips 2-5 turns of "what am I working with"
        # discovery. Cached on env, so rigorous plan/execute probe at most once.
        if config.bootstrap_environment:
            from garuda.core.bootstrap import environment_snapshot

            snapshot = await environment_snapshot(env)
            if snapshot:
                system_prompt = f"{system_prompt}\n\n{snapshot}"
                events.append(EventType.ENVIRONMENT_SNAPSHOT, {"chars": len(snapshot)})
        context = ContextManager(
            model=model,
            max_output_bytes=config.max_output_bytes,
            proactive_threshold=config.proactive_summarize_threshold,
            max_context_tokens=config.max_context_tokens,
            enable_three_step_summary=config.enable_three_step_summary,
            task=task,
            condenser=config.condenser,
            buffer=buffer,
        )
        context.seed(
            [
                Message(role=Role.SYSTEM, content=system_prompt),
                Message(role=Role.USER, content=task),
            ]
        )
    else:
        context.attach_buffer(buffer)
    events.append(EventType.USER_MESSAGE, {"content": task})

    if subagent_runner is None and "invoke_subagent" in tool_map:
        from garuda.core.subagent import SubagentRunner

        subagent_runner = SubagentRunner(
            model=model,
            env=env,
            events=events,
            agents_dir=agents_dir,
            skills_dirs=config.skills_dirs,
            workspace_root=getattr(env, "workspace_root", None),
            parent_context=context,
            parent_buffer=buffer,
            approval_handler=permissions.approval_handler,
            hooks=hooks,
        )

    # Wall-clock budget. Turn count alone cannot express "80% of my time is
    # gone", which is the condition that actually matters when a single
    # command can block for minutes.
    started_at = time.monotonic()
    deadline_at = started_at + config.deadline_sec if config.deadline_sec else None

    ctx = ToolContext(
        session_id=events.session_id,
        agent_profile=profile_name,
        model=model,
        subagent_runner=subagent_runner,
        buffer=buffer,
        post_edit_diagnostics=config.post_edit_diagnostics,
        post_edit_lint=config.post_edit_lint,
        persistent_shell=config.persistent_shell,
        permissions=permissions,
    )
    ctx.deadline_monotonic = deadline_at
    ledger = SideEffectLedger()
    ctx.side_effects = ledger
    # Session-wide record of what has already been asked, so a read repeated
    # 30 turns later is answered from memory rather than re-executed.
    memo = ActionMemo()

    return RunState(
        task=task,
        config=config,
        events=events,
        context=context,
        tools=tools,
        tool_map=tool_map,
        runner=ToolRunner(
            tool_map=tool_map,
            env=env,
            ctx=ctx,
            context=context,
            permissions=permissions,
            hooks=hooks,
            events=events,
            memo=memo,
            ledger=ledger,
        ),
        completion=CompletionGate(
            task=task,
            config=config,
            context=context,
            env=env,
            events=events,
            tool_map=tool_map,
            model=model,
            permissions=permissions,
            ledger=ledger,
        ),
        steering=Steering(
            max_turns=config.max_turns,
            started_at=started_at,
            deadline_at=deadline_at,
        ),
        memo=memo,
        ledger=ledger,
        emit_session_events=emit_session_events,
        checkpoint=checkpoint,
    )
