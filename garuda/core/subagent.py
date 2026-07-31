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
from garuda.core.events import EventStore, EventType
from garuda.core.permissions import PermissionEngine
from garuda.core.run_state import reserved_output_tokens
from garuda.model.protocol import Model
from garuda.tools import build_toolkit
from garuda.types import DEFAULT_SYSTEM_PROMPT, AgentResult, Message, Role
from garuda.workspace.protocol import Environment


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
        from garuda.core.loop import DefaultAgent

        profile = load_profile(profile_name, extra_dir=self.agents_dir)
        config = profile.to_agent_config()
        config.max_turns = min(config.max_turns, self.max_turns)
        config.enable_three_step_summary = False
        config.system_prompt = resolve_system_prompt(profile, self.workspace_root)

        permissions = PermissionEngine(
            mode=profile.permission_mode,
            tool_rules=profile.tool_rules,
            path_rules=profile.path_rules,
            bash_rules=profile.bash_rules,
            approval_handler=self.approval_handler,
        )
        tools, mcp_manager = await build_toolkit(
            profile.tools,
            profile.mcp_config_path,
        )
        sub_events = EventStore()
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
            if mcp_manager is not None:
                await mcp_manager.close()

        self.events.append(
            EventType.USER_MESSAGE,
            {
                "content": f"[subagent:{profile_name}] {result.final_message}",
                "subagent": profile_name,
                "success": result.success,
            },
        )
        result.metadata["subagent_session_id"] = sub_events.session_id
        result.metadata["subagent_profile"] = profile_name
        return result


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
