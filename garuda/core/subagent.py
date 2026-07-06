import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garuda.agents.loader import load_profile, resolve_system_prompt
from garuda.context.manager import ContextManager
from garuda.core.events import EventStore, EventType
from garuda.core.permissions import PermissionEngine
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
    agents_dir: Path | None = None
    skills_dirs: list[str] | None = None
    workspace_root: str | None = None
    max_turns: int = 50
    fork_parent_context: bool = False
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

    async def run(
        self,
        profile_name: str,
        task: str,
        *,
        fork_parent_context: bool | None = None,
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

        use_fork = self.fork_parent_context if fork_parent_context is None else fork_parent_context
        parent_snapshot = self._parent_snapshot()
        context: ContextManager | None = None
        shared_buffer = None
        if use_fork and parent_snapshot:
            context = ContextManager(
                model=self.model,
                max_output_bytes=config.max_output_bytes,
                proactive_threshold=config.proactive_summarize_threshold,
                max_context_tokens=config.max_context_tokens,
                enable_three_step_summary=False,
                task=task,
            )
            snapshot = _drop_incomplete_tail(deepcopy(parent_snapshot))
            # Run under the subagent's OWN persona, not the parent's leading system msg.
            sub_system = Message(
                role=Role.SYSTEM, content=config.system_prompt or DEFAULT_SYSTEM_PROMPT
            )
            if snapshot and snapshot[0].role == Role.SYSTEM:
                snapshot[0] = sub_system
            else:
                snapshot.insert(0, sub_system)
            context.seed(snapshot)
            context.append(
                Message(
                    role=Role.USER,
                    content=f"[subagent:{profile_name}] {task}",
                )
            )
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
