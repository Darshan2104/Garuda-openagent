import logging
from copy import deepcopy

from garuda.context.condenser import (
    Condenser,
    CondenserContext,
    MicrocompactCondenser,
    make_condenser,
)
from garuda.context.shaper import shape_observation
from garuda.model.protocol import Model
from garuda.types import Message, Role

logger = logging.getLogger(__name__)


def _estimate_tokens(message: Message) -> int:
    """Cheap token estimate (~4 chars/token) for a single message, used only to
    keep the condensation trigger current between provider counts."""
    n = len(message.content or "") // 4
    for call in message.tool_calls or []:
        n += len(str(call.arguments)) // 4
    return n


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
    ):
        self._model = model
        self._max_output_bytes = max_output_bytes
        self._proactive_threshold = proactive_threshold
        self._max_context_tokens = max_context_tokens
        self._enable_three_step_summary = enable_three_step_summary
        self._task = task
        self._keep_recent_turns = keep_recent_turns
        self._messages: list[Message] = []
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
        )
        new_messages = await self._condenser.condense(cx)
        if new_messages is None:
            return False
        self._messages = new_messages
        # The provider count reflected the pre-condensation prompt; invalidate
        # it so the next trigger uses a fresh estimate until the next response.
        self._last_prompt_tokens = None
        self._pending_tokens = 0
        return True
