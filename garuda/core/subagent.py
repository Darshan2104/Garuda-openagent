import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garuda.agents.loader import load_profile, resolve_system_prompt
from garuda.context.manager import (
    FORK_BRIEF,
    FORK_NONE,
    ContextManager,
    normalize_handoff,
)
from garuda.core.events import SUBAGENT_LOG_DIR, EventStore, EventType
from garuda.core.permissions import DelegatedPermissionEngine, PermissionEngine
from garuda.core.run_state import reserved_output_tokens
from garuda.model.protocol import Model
from garuda.tools import build_toolkit
from garuda.types import DEFAULT_SYSTEM_PROMPT, AgentResult, Message, Role
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

def persist_child_events(parent: EventStore, child: EventStore) -> None:
    """Give a subagent's event store a log file next to its parent's.

    A subagent runs on its **own** ``EventStore`` on purpose: its turns must not interleave
    into the parent's log, because the parent's turn segmentation is derived from that log
    and an inner turn 1 landing between an outer turn's call and result would corrupt it.
    The consequence, until now, was that the subagent's work was thrown away — the parent
    kept only the one-line handoff, so "what did the subagent actually do" had no answer
    anywhere on disk.

    A sibling file resolves both: separate logs keep both segmentations honest, and the
    parent's handoff event names the child's session id, so the two are joinable. Derived
    from the parent's own ``persist_path`` rather than a re-guessed sessions root, per
    ``docs/ARCHITECTURE.md`` — a subagent invoked in a run nobody persisted stays
    unpersisted, which is the right answer rather than inventing a directory for it.
    """
    parent_path = parent.persist_path
    if parent_path is None:
        return
    try:
        child.attach_persistence(
            parent_path.parent / SUBAGENT_LOG_DIR / f"{child.session_id}.jsonl"
        )
    except OSError:
        # `attach_persistence` makes the directory, and a read-only or full disk must not
        # stop the subagent from running. Losing its trace is a strictly smaller failure.
        logger.warning("Could not persist subagent events beside %s", parent_path, exc_info=True)


# Historical private name retained for callers and tests. Collection workers use
# the public helper because their traces share the same sibling-log contract but
# are deliberately not general-purpose subagents.
_persist_beside_parent = persist_child_events


def _drop_incomplete_tail(messages: list[Message]) -> list[Message]:
    """Trim a trailing turn whose assistant tool_calls aren't all answered.

    At invoke_subagent time the parent has appended its assistant tool_calls
    message but not yet the tool results (invoke_subagent is mid-execution), so
    the snapshot ends on an assistant turn with unanswered tool_calls. Seeding
    that verbatim makes the subagent's first request an invalid sequence (a 400).
    """
    msgs = list(messages)
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].role == Role.ASSISTANT and msgs[i].tool_calls:
            answered = {m.tool_call_id for m in msgs[i + 1 :] if m.role == Role.TOOL}
            needed = {c.id for c in msgs[i].tool_calls}
            if not needed.issubset(answered):
                return msgs[:i]
            break
    return msgs


@dataclass
class DelegationBudget:
    """One root run's delegation allowance, shared down its subagent tree (H.6).

    A child cannot reset it by delegating again: every runner in the tree
    draws from this one object.
    """

    max_depth: int = 2
    max_launches: int = 8
    launches: int = 0


def _refused(code: str, message: str) -> AgentResult:
    return AgentResult(success=False, final_message=f"{code}: {message}", messages=[], turns=0,
                       metadata={"refused": code})


@dataclass
class SubagentRunner:
    model: Model
    env: Environment
    events: EventStore
    agents_dir: Path | list[Path] | None = None
    skills_dirs: list[str] | None = None
    workspace_root: str | None = None
    max_turns: int = 50
    # Default handoff when the caller doesn't name one. Accepts a bool for
    # back-compat: True is "full" (the whole transcript), False is "none".
    fork_parent_context: bool | str = FORK_NONE
    parent_messages: list[Message] | None = None
    parent_context: ContextManager | None = None
    # Inherited from the parent so ASK decisions and lifecycle hooks behave the
    # same inside a subagent (otherwise ASK auto-denies and hooks are dropped).
    approval_handler: Any = None
    hooks: Any = None
    # Parent's tool-output buffer, shared into a forked subagent so inherited
    # [buffer:...] stubs resolve (they live under the parent's session dir).
    parent_buffer: Any = None
    # The parent's *effective* permission engine (itself delegated when the parent
    # is a subagent). Every child call must also pass it, so a child profile
    # declaring a wider mode (e.g. `harbor`'s yolo) cannot escalate past the run
    # that started it. None only for direct programmatic construction.
    parent_permissions: Any = None
    # Tools the parent run was admitted with. A child selects among these rather
    # than building a fresh toolkit, so constructing a child never connects a new
    # MCP server or imports code the parent was not already running with.
    parent_tools: list | None = None
    # Delegation bounds (H.6). ``allowed``: the agents this run may start (None:
    # any). ``depth``: the depth a child started here runs at. The budget and
    # deadline are the root's, shared; ``turns_left`` reports the parent's
    # remaining turns so a child never outruns them.
    allowed: list[str] | None = None
    depth: int = 1
    budget: DelegationBudget | None = None
    deadline_monotonic: float | None = None
    turns_left: Any = None
    busy: bool = False

    def _parent_snapshot(self) -> list[Message] | None:
        """Live view of the parent conversation at invoke time, not construction time."""
        if self.parent_context is not None:
            return self.parent_context.get_messages()
        return self.parent_messages

    def _handoff_context(
        self, mode: str, profile_name: str, task: str, config
    ) -> ContextManager | None:
        """Build the subagent's starting context for a ``brief`` or ``full`` handoff.

        Returns None when there is nothing to hand over, so the caller falls back to
        a cold start rather than seeding an empty conversation.

        A ``brief`` handoff goes through the parent's ``ContextManager`` because only
        it can render the working-state card. Without a live parent context (a caller
        holding raw ``parent_messages``) there is no card to render, and brief then
        degrades to ``none`` — never to ``full``. Falling back to full would hand the
        entire transcript to a caller who explicitly asked for the cheap handoff,
        turning the default into the most expensive option available.
        """
        if mode == FORK_BRIEF:
            if self.parent_context is None:
                return None
            context = self.parent_context.fork(mode=FORK_BRIEF)
            context.set_task(task)
        else:
            snapshot = self._parent_snapshot()
            if not snapshot:
                return None
            context = ContextManager(
                model=self.model,
                max_output_bytes=config.max_output_bytes,
                proactive_threshold=config.proactive_summarize_threshold,
                max_context_tokens=config.max_context_tokens,
                enable_three_step_summary=False,
                task=task,
                reserved_output_tokens=reserved_output_tokens(config),
                safety_margin_tokens=config.context_safety_margin_tokens,
                adaptive_output=config.enable_adaptive_output,
                min_output_bytes=config.min_output_bytes,
            )
            context.seed(_drop_incomplete_tail(deepcopy(snapshot)))

        # Run under the subagent's OWN persona, not the parent's leading system msg.
        context.replace_system_message(config.system_prompt or DEFAULT_SYSTEM_PROMPT)
        context.append(
            Message(role=Role.USER, content=f"[subagent:{profile_name}] {task}")
        )
        return context

    async def run(
        self,
        profile_name: str,
        task: str,
        *,
        fork_parent_context: bool | str | None = None,
    ) -> AgentResult:
        import time

        from garuda.core.loop import DefaultAgent

        # The allowlist is enforced here, not only in the tool schema: a model
        # (or a caller) naming another agent directly is refused all the same.
        if self.allowed is not None and profile_name not in self.allowed:
            return _refused("agent.subagent_not_allowed",
                            f"{profile_name!r} is not one of {', '.join(self.allowed) or 'none'}")
        budget = self.budget if self.budget is not None else DelegationBudget()
        self.budget = budget
        if self.depth > budget.max_depth:
            return _refused("agent.delegation_too_deep",
                            f"subagents may nest {budget.max_depth} deep")
        if budget.launches >= budget.max_launches:
            return _refused("agent.delegation_exhausted",
                            f"this run has started its {budget.max_launches} subagents")
        if self.busy:
            return _refused("agent.delegation_busy", "a subagent is already running")
        remaining = None
        if self.deadline_monotonic is not None:
            remaining = self.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                return _refused("agent.delegation_deadline", "the run's deadline has passed")
        budget.launches += 1

        profile = load_profile(profile_name, extra_dir=self.agents_dir)
        config = profile.to_agent_config()
        parent_left = self.turns_left() if callable(self.turns_left) else self.max_turns
        config.max_turns = max(1, min(config.max_turns, self.max_turns, parent_left))
        if remaining is not None:
            config.deadline_sec = min(config.deadline_sec or remaining, remaining)
        config.delegation_depth = self.depth
        config.delegation_budget = budget
        if self.allowed is not None:
            child = config.subagents if config.subagents is not None else self.allowed
            config.subagents = [name for name in child if name in self.allowed]
        config.enable_three_step_summary = False
        config.system_prompt = resolve_system_prompt(profile, self.workspace_root)

        child_permissions = PermissionEngine(
            mode=profile.permission_mode,
            tool_rules=profile.tool_rules,
            path_rules=profile.path_rules,
            bash_rules=profile.bash_rules,
            approval_handler=self.approval_handler,
        )
        permissions = (
            DelegatedPermissionEngine(child_permissions, self.parent_permissions)
            if self.parent_permissions is not None
            else child_permissions
        )
        if profile.mcp_config_path or profile.mcp_servers:
            logger.warning(
                "Subagent %r declares MCP servers; a subagent never opens its own MCP "
                "connections and can only use MCP tools its parent already has",
                profile.name,
            )
        mcp_manager = None
        if self.parent_tools is not None:
            tools = delegated_tools(profile.tools, self.parent_tools, profile.name)
        else:
            tools, mcp_manager = await build_toolkit(profile.tools, None)
        sub_events = EventStore()
        persist_child_events(self.events, sub_events)
        agent = DefaultAgent(profile_name=profile.name)

        mode = normalize_handoff(
            self.fork_parent_context if fork_parent_context is None else fork_parent_context
        )
        context: ContextManager | None = None
        shared_buffer = None
        if mode != FORK_NONE:
            context = self._handoff_context(mode, profile_name, task, config)
            if context is not None:
                # Share the parent buffer so inherited stubs are retrievable.
                shared_buffer = self.parent_buffer

        self.busy = True
        try:
            result = await agent.run(
                task=task,
                model=self.model,
                env=self.env,
                tools=tools,
                config=config,
                events=sub_events,
                permissions=permissions,
                hooks=self.hooks,
                context=context,
                buffer=shared_buffer,
            )
        finally:
            self.busy = False
            if mcp_manager is not None:
                await mcp_manager.close()

        self.events.append(
            EventType.USER_MESSAGE,
            {
                "content": f"[subagent:{profile_name}] {result.final_message}",
                "subagent": profile_name,
                "success": result.success,
                # The parent log records the *handoff*, not the work. These three fields are
                # what makes the work findable afterwards: the id names the sibling log
                # `_persist_beside_parent` just wrote, and the counts let a reader say how
                # much happened inside without opening it.
                "session_id": sub_events.session_id,
                "turns": result.turns,
                "task": task,
            },
        )
        result.metadata["subagent_session_id"] = sub_events.session_id
        result.metadata["subagent_profile"] = profile_name
        return result


# Tools a run inserts for itself; a child gets its own copies from its own setup
# when its configuration enables them, never the parent's bound instances.
_RUN_SCOPED_TOOLS = frozenset({"delegate_collection", "submit_collection"})
_MCP_META_TOOLS = frozenset({"search_tool", "use_tool"})


def _is_mcp_tool(name: str) -> bool:
    return name.startswith("mcp__") or name in _MCP_META_TOOLS


def delegated_tools(requested: list[str] | None, parent_tools: list, profile_name: str) -> list:
    """Select a child's tools from the parent's admitted toolkit.

    A child can only narrow: a requested tool the parent does not have is dropped
    (with a warning), never resolved fresh. With no explicit list the child gets
    the parent's tools except MCP ones, which a child must name to receive.
    """
    available = {
        tool.name: tool for tool in parent_tools if tool.name not in _RUN_SCOPED_TOOLS
    }
    if requested is None:
        return [tool for name, tool in available.items() if not _is_mcp_tool(name)]
    selected = []
    missing = []
    for name in dict.fromkeys(requested):
        if name in available:
            selected.append(available[name])
        elif name not in _RUN_SCOPED_TOOLS:
            missing.append(name)
    if missing:
        logger.warning(
            "Subagent %r requested tools its parent run does not have; they are not "
            "available to it: %s",
            profile_name,
            ", ".join(missing),
        )
    return selected


_SUMMARY_BUFFER_RE = re.compile(r"\[buffer:([^\s|\]]+)")


def format_subagent_summary(profile_name: str, result: AgentResult) -> str:
    """Distill a subagent run into structured evidence for the parent.

    Returns the final message plus the files it changed and any retrievable buffer
    ids, so the parent can act on the subagent's work without re-discovering it.
    """
    files: list[str] = []
    buffers: list[str] = []
    for message in result.messages:
        for call in message.tool_calls or []:
            if call.name in ("write_file", "edit"):
                path = call.arguments.get("path")
                if path and path not in files:
                    files.append(path)
        if message.role == Role.TOOL and message.content:
            for bid in _SUMMARY_BUFFER_RE.findall(message.content):
                if bid not in buffers:
                    buffers.append(bid)

    parts = [f"Subagent @{profile_name} finished (success={result.success}, turns={result.turns})."]
    if files:
        parts.append("Files changed: " + ", ".join(files[:30]))
    if buffers:
        parts.append(
            "Retrievable buffers (buffer_grep/buffer_slice): " + ", ".join(buffers[:10])
        )
    parts.append("Summary:\n" + (result.final_message or "(no summary)"))
    return "\n".join(parts)
