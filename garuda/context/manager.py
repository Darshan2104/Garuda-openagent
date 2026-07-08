import hashlib
import json
import logging
from copy import deepcopy
from typing import TYPE_CHECKING

from garuda.context.condenser import (
    Condenser,
    CondenserContext,
    MicrocompactCondenser,
    make_condenser,
)
from garuda.context.shaper import shape_observation
from garuda.model.protocol import Model
from garuda.types import Message, Role

if TYPE_CHECKING:
    from garuda.core.buffer import ToolOutputBuffer

logger = logging.getLogger(__name__)


def _estimate_tokens(message: Message) -> int:
    """Cheap token estimate (~4 chars/token) for a single message, used only to
    keep the condensation trigger current between provider counts."""
    n = len(message.content or "") // 4
    for call in message.tool_calls or []:
        n += len(str(call.arguments)) // 4
    return n


def render_archive_transcript(messages: list[Message]) -> str:
    """Render dropped messages as a grep-friendly plain-text transcript.

    Line-oriented on purpose (one header per message, raw content below) so
    buffer_grep/buffer_slice work well against it. Image payloads are omitted.
    """
    lines = [
        "Conversation segment compacted out of the live context.",
        "Format: one '--- [n] <role> ---' header per message, full content below.",
        "",
    ]
    for index, message in enumerate(messages, start=1):
        header = f"--- [{index}] {message.role.value}"
        if message.name:
            header += f" tool={message.name}"
        if message.tool_call_id:
            header += f" tool_call_id={message.tool_call_id}"
        lines.append(header + " ---")
        for call in message.tool_calls or []:
            try:
                args = json.dumps(call.arguments, sort_keys=True, default=str)
            except (TypeError, ValueError):
                args = str(call.arguments)
            lines.append(f"[tool call] {call.name}({args[:2000]})")
        if message.images:
            lines.append(f"[{len(message.images)} image(s) omitted from archive]")
        if message.content:
            lines.append(message.content)
        lines.append("")
    return "\n".join(lines)


class ContextManager:
    def __init__(
        self,
        model: Model,
        max_output_bytes: int = 30_720,
        proactive_threshold: int = 8000,
        max_context_tokens: int = 128_000,
        enable_three_step_summary: bool = True,
        task: str = "",
        keep_recent_turns: int = 12,
        condenser: Condenser | str | None = None,
        buffer: "ToolOutputBuffer | None" = None,
    ):
        self._model = model
        self._max_output_bytes = max_output_bytes
        self._proactive_threshold = proactive_threshold
        self._max_context_tokens = max_context_tokens
        self._enable_three_step_summary = enable_three_step_summary
        self._task = task
        self._keep_recent_turns = keep_recent_turns
        self._messages: list[Message] = []
        self._buffer = buffer
        self._archive_seq = 0
        self._last_prompt_tokens: int | None = None
        # Estimated tokens appended since the last provider count, so the
        # condensation trigger reflects this turn's tool results instead of lagging
        # a turn behind (a big parallel-read batch could otherwise overflow before
        # the next check fires).
        self._pending_tokens = 0
        if isinstance(condenser, str):
            condenser = make_condenser(condenser)
        self._condenser: Condenser = condenser or MicrocompactCondenser()

    def seed(self, messages: list[Message]) -> None:
        self._messages = list(messages)
        # Only infer the task from history when one wasn't already set explicitly
        # (a forked subagent seeds the parent's history but has its OWN task — the
        # parent's first user message must not clobber it, or summaries anchor wrong).
        if not self._task:
            task_message = next((m for m in messages if m.role == Role.USER), None)
            if task_message:
                self._task = task_message.content

    def append(self, message: Message) -> None:
        self._messages.append(message)
        if self._last_prompt_tokens is not None:
            self._pending_tokens += _estimate_tokens(message)

    def attach_buffer(self, buffer: "ToolOutputBuffer | None") -> None:
        """Late-bind the session buffer (callers that reuse a context across runs
        create the buffer after the context exists). Never clobbers an existing one."""
        if buffer is not None and self._buffer is None:
            self._buffer = buffer

    def get_messages(self) -> list[Message]:
        return list(self._messages)

    def shape_observation(self, output: str, is_error: bool = False) -> str:
        return shape_observation(output, self._max_output_bytes, is_error=is_error)

    def note_usage(self, usage: dict[str, int] | None) -> None:
        """Record provider-reported prompt tokens from the last response.

        Provider counts include tool schemas and message framing that local
        estimates miss, so they take priority for the condensation trigger.
        """
        if usage and usage.get("prompt_tokens"):
            self._last_prompt_tokens = usage["prompt_tokens"]
            self._pending_tokens = 0

    def fork(self, *, include_history: bool = True) -> "ContextManager":
        forked = ContextManager(
            model=self._model,
            max_output_bytes=self._max_output_bytes,
            proactive_threshold=self._proactive_threshold,
            max_context_tokens=self._max_context_tokens,
            enable_three_step_summary=self._enable_three_step_summary,
            task=self._task,
            keep_recent_turns=self._keep_recent_turns,
            condenser=self._condenser,
            buffer=self._buffer,
        )
        if include_history:
            forked._messages = deepcopy(self._messages)
        return forked

    def _used_tokens(self) -> int:
        if self._last_prompt_tokens is not None:
            return self._last_prompt_tokens + self._pending_tokens
        return self._model.count_tokens(self._messages)

    def usage_fraction(self) -> float:
        if self._max_context_tokens <= 0:
            return 0.0
        return self._used_tokens() / self._max_context_tokens

    async def maybe_summarize(self) -> bool:
        """Ask the condenser whether/how to shrink history; apply if it does."""
        cx = CondenserContext(
            messages=self._messages,
            model=self._model,
            task=self._task,
            used_tokens=self._used_tokens(),
            max_context_tokens=self._max_context_tokens,
            proactive_threshold=self._proactive_threshold,
            keep_recent_turns=self._keep_recent_turns,
            enable_three_step_summary=self._enable_three_step_summary,
            buffer=self._buffer,
        )
        new_messages = await self._condenser.condense(cx)
        if new_messages is None:
            return False
        self._archive_dropped(new_messages)
        self._messages = new_messages
        # The provider count reflected the pre-condensation prompt; invalidate
        # it so the next trigger uses a fresh estimate until the next response.
        self._last_prompt_tokens = None
        self._pending_tokens = 0
        return True

    def _archive_dropped(self, new_messages: list[Message]) -> None:
        """Archive messages the condenser dropped into the session buffer, and leave
        a retrieval pointer in the surviving history.

        Works for any condenser: dropped = in the old list but not the new one
        (in-place prunes keep the same objects, so a prune archives nothing here).
        Best-effort — an archive failure must never break condensation.
        """
        if self._buffer is None:
            return
        kept_ids = {id(m) for m in new_messages}
        dropped = [m for m in self._messages if id(m) not in kept_ids]
        if not dropped:
            return
        try:
            rendered = render_archive_transcript(dropped)
            digest = hashlib.sha1(rendered.encode("utf-8")).hexdigest()[:8]
            self._archive_seq += 1
            buffer_id = f"archive_{self._archive_seq}_{digest}"
            self._buffer.store(buffer_id, rendered, tool_name="context_archive")
        except Exception:
            logger.warning("Failed to archive compacted context", exc_info=True)
            return
        pointer = (
            f"[context-archive] {len(dropped)} earlier messages were compacted out of "
            f"context. Their full transcript is archived in buffer:{buffer_id} — recover "
            f'details with buffer_grep(buffer_id="{buffer_id}", pattern="...") or '
            f'buffer_slice(buffer_id="{buffer_id}", start_line=N, end_line=M).'
        )
        # Attach the pointer to the message the condenser just created (the summary),
        # or, for condensers that drop without summarizing, insert it after the task.
        old_ids = {id(m) for m in self._messages}
        summary = next(
            (m for m in new_messages if id(m) not in old_ids and m.role == Role.USER), None
        )
        if summary is not None:
            summary.content = f"{summary.content}\n\n{pointer}"
            return
        insert_at = 0
        for index, message in enumerate(new_messages):
            if message.role == Role.USER:
                insert_at = index + 1
                break
            if message.role == Role.SYSTEM:
                insert_at = index + 1
        new_messages.insert(insert_at, Message(role=Role.USER, content=pointer))
