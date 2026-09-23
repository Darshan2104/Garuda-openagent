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
from garuda.context.state_card import WorkingState
from garuda.core.action_memo import ActionMemo
from garuda.core.completion import CompletionGate
from garuda.core.events import EventStore, EventType
from garuda.core.metrics import RunMetrics, stopwatch
from garuda.core.modes import describe_config
from garuda.core.permissions import PermissionEngine
from garuda.core.side_effects import SideEffectLedger
from garuda.core.steering import Steering
from garuda.core.tool_runner import ToolRunner
from garuda.plugins.hooks import HookRegistry
from garuda.tools.protocol import EXPLICIT_TOOL_ATTR, Tool, ToolContext
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
    # Persists the working state next to the messages. Separate from ``checkpoint``
    # because the transcript is not a superset of it: after a compaction the card is
    # the only remaining record of which files were touched and which checks passed.
    state_checkpoint: object | None = None
    metrics: RunMetrics = field(default_factory=RunMetrics)
    # The tool schemas sent with every model call, built once (see
    # ``build_tools_schema``). Held here so the loop sends the same object each
    # turn and the context budget can cache its cost against it.
    tools_schema: list[dict] = field(default_factory=list)
    # The run's deterministic self-knowledge: goal, todos, files touched, checks
    # run, criteria. Re-pinned after compaction and fed to the summarizer as given
    # facts, so the model is not asked to remember what the harness already knows.
    state: WorkingState = field(default_factory=WorkingState)

    usage_totals: dict[str, int] = field(default_factory=dict)
    final_message: str = ""
    # The last substantive thing the model said. After a forced final turn this
    # usually holds the best answer the run produced, so an exhausted run reports
    # it rather than a bare "max turns exceeded" that discards real work.
    last_assistant_text: str = ""

    def accumulate_usage(self, usage: dict[str, int]) -> None:
        self.metrics.note_usage(usage)
        for key, value in (usage or {}).items():
            if key == "cost_usd":
                # Provider-reported spend: a float, and summed as one. Coercing it to
                # int here would silently round every sub-dollar call to zero.
                self.usage_totals[key] = self.usage_totals.get(key, 0.0) + float(value)
                continue
            self.usage_totals[key] = self.usage_totals.get(key, 0) + int(value)

    def flush_turn_metrics(self) -> None:
        """Append the open turn's metrics event, once.

        Called both from the loop after each turn and from the result builders below,
        because a turn that ends the run builds its result from inside the tool walk
        and that result snapshots the event list. Emitting only from the loop left the
        final turn's metrics out of every consumer reading ``metadata["events"]``.
        """
        record = self.metrics.current
        if record is None or record.emitted:
            return
        record.emitted = True
        self.events.append(EventType.TURN_METRICS, record.to_dict())

    def answer_open_calls(self, calls: list, accepted, reason: str | None = None) -> None:
        """Close out a response's tool calls when an accepted completion ends the run.

        Providers require every ``tool_calls`` entry to have a matching tool result,
        and an accepted ``task_complete`` used to return straight out of the tool walk
        — leaving its own call unanswered along with any sibling calls the model made
        in the same response, which the run never got to. The transcript that lands in
        ``AgentResult.messages`` was therefore malformed at exactly the point it is
        most likely to be reused: resume it, hand it to a judge, or replay it from a
        checkpoint, and the next provider call is a 400.

        Only fills gaps: calls that already produced a result keep it. ``accepted``
        may be None on the paths that end the run without an accepted completion
        (the final submission turn declining to submit, or its submission being
        rejected), where ``reason`` supplies the text for every open call.
        """
        answered = {
            message.tool_call_id
            for message in self.context.get_messages()
            if message.role == Role.TOOL and message.tool_call_id
        }
        default_reason = reason or "Not executed: the run ended when task_complete was accepted."
        for call in calls:
            if call.id in answered:
                continue
            content = (
                "task_complete accepted — the run ended here."
                if accepted is not None
                and (call is accepted or call.id == getattr(accepted, "id", None))
                else default_reason
            )
            self.context.append(
                Message(
                    role=Role.TOOL,
                    content=content,
                    name=call.name,
                    tool_call_id=call.id,
                )
            )

    def result(self, success: bool, final_message: str, turns: int) -> AgentResult:
        """Build the AgentResult, surfacing how the run behaved in metadata.

        Repetition, cleanup, and which stated requirements were actually
        established are on the result so a caller can see them without replaying
        the event log.
        """
        self.flush_turn_metrics()
        metadata: dict = {
            "session_id": self.events.session_id,
            "events": self.events.get_all(),
            "usage": dict(self.usage_totals),
            "mode": self.config.mode,
            "metrics": self.metrics.summary(),
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
        self.flush_turn_metrics()
        return AgentResult(
            success=success,
            final_message=final_message,
            messages=self.context.get_messages(),
            turns=turns,
            metadata={
                "session_id": self.events.session_id,
                "events": self.events.get_all(),
                "usage": dict(self.usage_totals),
                "mode": self.config.mode,
                # Included even here: how long a run spent before dying on a model
                # error or a dead workspace is exactly what you want to see.
                "metrics": self.metrics.summary(),
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
        if self.checkpoint is None and self.state_checkpoint is None:
            return
        try:
            with stopwatch() as elapsed:
                if self.checkpoint is not None:
                    self.checkpoint(self.context.get_messages())
                if self.state_checkpoint is not None:
                    self.state_checkpoint(self.refresh_state().to_dict())
        except Exception:
            logger.warning("Session checkpoint failed", exc_info=True)
        # Recorded because this write is O(transcript) every turn, so it grows with
        # the run. If it ever shows up against model and tool time, that is the
        # number that justifies replacing it with an append-only delta log.
        record = self.metrics.current
        if record is not None:
            record.checkpoint_ms = round(record.checkpoint_ms + elapsed[0], 3)

    async def compact_if_needed(self, turn: int) -> bool:
        """Compact if the next request would not fit, and re-pin what compaction drops.

        Called twice per turn: once at the top of the loop, and once immediately
        before the model call. The second check is what makes the budget honest —
        steering nudges, re-pinned state and the previous turn's tool results all
        land between the two, so the first check measures a prompt that is never
        the one sent. Cheap when there is nothing to do: the condenser's first gate
        is a compare against an already-anchored gauge.
        """
        with stopwatch() as elapsed:
            summarized = await self.context.maybe_summarize()
        if not summarized:
            return False
        record = self.metrics.current
        if record is not None:
            record.compaction_ms = round(record.compaction_ms + elapsed[0], 3)
        # Turn and duration alone could not distinguish a cheap in-place prune from a
        # full summarize-and-rebuild that cost three model calls and dropped half the
        # history — the two most different things compaction does. The context reports
        # what it did; `or {}` keeps the event's original shape if it reported nothing.
        self.events.append(
            EventType.SUMMARIZATION,
            {
                "turn": turn,
                "duration_ms": elapsed[0],
                **(self.context.last_compaction or {}),
            },
        )
        # Compaction can summarize away the goal and todo list; re-pin them so long
        # tasks keep their north star and don't re-derive their plan (which would
        # waste turns and tokens).
        self.reinject_pinned_state()
        return True

    def note_context_budget(self, turn: int) -> None:
        """Record what the request about to be sent will cost.

        Every compaction decision is made on the harness's own view of the budget,
        and until now that view appeared nowhere in a trajectory — only the
        provider's after-the-fact count did. Best-effort: a gauge must never be
        able to end a run.
        """
        try:
            payload = self.context.budget_snapshot()
        except Exception:
            logger.debug("Context budget snapshot failed", exc_info=True)
            return
        self.events.append(
            EventType.BUDGET, {"stage": "context", "turn": turn, **payload}
        )

    def refresh_state(self) -> WorkingState:
        """Pull the run's live facts into the working state and return it.

        Reads the sources rather than mirroring them: the goal tool, the todo tool,
        the side-effect ledger and the contract are each already the authority on
        their own field, and a second copy kept in sync by hand is a second copy
        that can be wrong.
        """
        state = self.state
        state.task = self.task
        state.acceptance = ""
        contract = self.completion.contract
        if contract is not None and contract.criteria:
            state.acceptance = contract.render()
        goal_tool = self.tool_map.get("update_goal")
        if goal_tool is not None:
            state.goal = goal_tool.get_goal(self.events.session_id) or ""
        todo_tool = self.tool_map.get("todo")
        if todo_tool is not None:
            state.todos = todo_tool.get_todos(self.events.session_id)
        state.files_modified = sorted(self.ledger.files_written)
        return state

    def render_state_card(self) -> str:
        """The current working state as it would appear in context."""
        return self.refresh_state().render()

    def reinject_pinned_state(self) -> None:
        """Re-pin this run's goal, todos and acceptance criteria after a compaction."""
        reinject_pinned_state(
            self.context,
            self.tool_map,
            self.events.session_id,
            contract=self.completion.contract,
            state=self.refresh_state() if self.config.enable_working_state_card else None,
        )


def _already_pinned(context: ContextManager, rendered: str) -> bool:
    """True when this exact card text is already somewhere in the live context.

    Deliberately the whole history, not a tail window. The summary rebuild embeds
    the card near the front of the rebuilt list, and the re-pin that follows would
    append the same text again — a tail-only check cannot see the copy it is about
    to duplicate. Position does not matter for the decision anyway: if the text is
    byte-identical then no observed fact has changed since it was written, and
    there is nothing for a second copy to tell the model. The moment anything does
    change, ``render()`` differs and the card is pinned again.
    """
    try:
        messages = context.get_messages()
    except Exception:
        return False
    return any(rendered in (message.content or "") for message in messages)


def reinject_pinned_state(
    context: ContextManager,
    tool_map: dict[str, Tool],
    session_id: str,
    contract=None,
    state: WorkingState | None = None,
) -> None:
    """Re-surface what compaction is allowed to drop, as compact messages. Safe at
    the top of a turn (the prior turn's tool-result block is already complete).
    No-op when there is nothing pinned.

    Given a ``WorkingState``, this is one message: the card already carries goal,
    todos and criteria alongside the files and checks the run has to its name, and
    three separate messages saying pieces of the same thing cost more and read as
    three unrelated interruptions. Without one, it falls back to the original
    per-source messages so a caller that never built a card still gets its state back.

    A free function, not just a ``RunState`` method: it depends on nothing but its
    arguments, and keeping it callable without a whole run assembled is what makes
    it directly testable.
    """
    if state is not None:
        if state.is_empty():
            return
        rendered = state.render()
        # Idempotent: the budget is now checked twice a turn, and a condenser that
        # still has prunable content reports "changed" both times, so this can be
        # reached twice with nothing having happened in between. It is also reached
        # right after a summary rebuild, which already embeds the card. Re-pinning
        # text that is verbatim present is pure cost — and two identical cards back
        # to back read as two separate updates that happen to agree.
        if _already_pinned(context, rendered):
            return
        context.append(Message(role=Role.USER, content=rendered))
        return
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


def reserved_output_tokens(config: AgentConfig) -> int:
    """Window to hold back for the model's own response.

    Prefers a figure the run actually commits to over the configured guess:

    * a thinking budget implies at least ``budget + 4096``, because
      ``LitellmModel._apply_reasoning`` forces ``max_tokens`` there — reserving less
      would leave the budget wrong on exactly the runs with least room to spare;
    * an explicit ``max_tokens`` is the real ceiling on the response, so reserve
      exactly that rather than a default that is probably too large;
    * otherwise fall back to the configured reserve.

    A reserve that is too big is not free: it moves every compaction trigger
    earlier, and compactions cost summary calls and latency.
    """
    budget = getattr(config, "thinking_budget_tokens", None)
    if budget:
        return max(config.reserved_output_tokens, int(budget) + 4096)
    max_tokens = getattr(config, "max_tokens", None)
    if max_tokens:
        return int(max_tokens)
    return config.reserved_output_tokens


def build_tools_schema(tools: list[Tool]) -> list[dict]:
    """The tool schemas sent with every model call.

    Built once per run rather than per turn. Two reasons beyond the obvious: the
    context budget needs a stable object to cache its token cost against, and an
    identical list object turn after turn keeps the request's cached prefix
    byte-identical, which is the same reason ``_filter_tools`` preserves order.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


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
        # And a tool the caller supplied explicitly. This allowlist selects among
        # discovered tools; a registered one is not a candidate to be selected, it
        # is a decision already made. Dropping it here is what made
        # `register_tool` and `--load-project-tools` silent no-ops on `build`,
        # whose list names 27 tools and so matched nothing the user added.
        elif getattr(tool, EXPLICIT_TOOL_ATTR, False):
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
    state_checkpoint=None,
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
        events.append(
            EventType.SESSION_START,
            {
                "task": task,
                "model": model.model_name,
                "agent": profile_name,
                "mode": config.mode,
                # The engine's mode, not the config's. They can differ: a caller may
                # hand in a PermissionEngine built separately (the harbor adapter
                # rebuilds one from its own kwarg), and what gated the tools is the
                # engine. The config's copy stays inside `config` below, so a
                # divergence between the two is visible rather than averaged away.
                "permission_mode": permissions.mode,
                "config": describe_config(config),
            },
        )

    if config.allowed_tools:
        tools = _filter_tools(tools, config.allowed_tools)
    tool_map = {tool.name: tool for tool in tools}
    tools_schema = build_tools_schema(tools)

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
            reserved_output_tokens=reserved_output_tokens(config),
            safety_margin_tokens=config.context_safety_margin_tokens,
            adaptive_output=config.enable_adaptive_output,
            min_output_bytes=config.min_output_bytes,
        )
        context.seed(
            [
                Message(role=Role.SYSTEM, content=system_prompt),
                Message(role=Role.USER, content=task),
            ]
        )
    else:
        context.attach_buffer(buffer)
    # Told to the context in both branches: a reused context (resume, subagent) has
    # the caller's toolkit, not the one it was built with, and a budget that counts
    # the wrong schemas is worse than one that counts none.
    context.set_tools(tools_schema)
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
    # Shared by the loop (turn boundaries, model and compaction time) and the tool
    # runner (per-call and per-segment time), so neither has to thread numbers back
    # through return values to reach the other.
    metrics = RunMetrics()
    # Assembled here rather than inside RunState so the tool runner can be handed
    # the same object: the runner is what sees every command and every error, and
    # threading those back out through return values would put a state argument on
    # four signatures to save one attribute.
    state = WorkingState(task=task)

    run_state = RunState(
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
            metrics=metrics,
            max_parallel_reads=config.max_parallel_reads,
            state=state if config.enable_working_state_card else None,
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
        state_checkpoint=state_checkpoint if config.enable_working_state_card else None,
        metrics=metrics,
        tools_schema=tools_schema,
        state=state,
    )
    if config.enable_working_state_card:
        # A callable, not the object: the card needs the contract and the tool map,
        # which only exist once the run is assembled — and passing a callable keeps
        # garuda/context from having to import garuda/core to read them.
        context.set_state_provider(run_state.render_state_card)
    return run_state
