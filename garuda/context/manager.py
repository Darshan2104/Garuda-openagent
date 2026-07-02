import logging
from copy import deepcopy

from garuda.context.shaper import shape_observation
from garuda.context.summarizer import summarize_three_step
from garuda.model.protocol import Model
from garuda.types import Message, Role

logger = logging.getLogger(__name__)


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

    def seed(self, messages: list[Message]) -> None:
        self._messages = list(messages)
        task_message = next((m for m in messages if m.role == Role.USER), None)
        if task_message:
            self._task = task_message.content

    def append(self, message: Message) -> None:
        self._messages.append(message)

    def get_messages(self) -> list[Message]:
        return list(self._messages)

    def shape_observation(self, output: str, is_error: bool = False) -> str:
        return shape_observation(output, self._max_output_bytes, is_error=is_error)

    def note_usage(self, usage: dict[str, int] | None) -> None:
        """Record provider-reported prompt tokens from the last response.

        Provider counts include tool schemas and message framing that local
        estimates miss, so they take priority for the compaction trigger.
        """
        if usage and usage.get("prompt_tokens"):
            self._last_prompt_tokens = usage["prompt_tokens"]

    def fork(self, *, include_history: bool = True) -> "ContextManager":
        forked = ContextManager(
            model=self._model,
            max_output_bytes=self._max_output_bytes,
            proactive_threshold=self._proactive_threshold,
            max_context_tokens=self._max_context_tokens,
            enable_three_step_summary=self._enable_three_step_summary,
            task=self._task,
            keep_recent_turns=self._keep_recent_turns,
        )
        if include_history:
            forked._messages = deepcopy(self._messages)
        return forked

    def _used_tokens(self) -> int:
        if self._last_prompt_tokens is not None:
            return self._last_prompt_tokens
        return self._model.count_tokens(self._messages)

    def usage_fraction(self) -> float:
        if self._max_context_tokens <= 0:
            return 0.0
        return self._used_tokens() / self._max_context_tokens

    MICROCOMPACT_FRACTION = 0.75
    PRUNE_MIN_CHARS = 500

    def _microcompact(self) -> int:
        """Prune bulky tool outputs outside the recent window, in place.

        Keeps the message structure (and thus provider prompt-cache prefixes)
        intact — only old tool-result *contents* are replaced with stubs.
        Returns the number of messages pruned.
        """
        # Find the index where the recent window starts (keep_recent_turns
        # assistant turns from the end); prune only before it.
        turns = 0
        boundary = 0
        for index in range(len(self._messages) - 1, -1, -1):
            if self._messages[index].role == Role.ASSISTANT:
                turns += 1
                if turns >= self._keep_recent_turns:
                    boundary = index
                    break
        pruned = 0
        for message in self._messages[:boundary]:
            if (
                message.role == Role.TOOL
                and len(message.content or "") > self.PRUNE_MIN_CHARS
                and not message.metadata.get("pruned")
            ):
                original_len = len(message.content)
                message.content = (
                    f"[tool output pruned to save context: was {original_len} chars. "
                    "Re-run the tool if this output is needed again.]"
                )
                message.metadata["pruned"] = True
                pruned += 1
        return pruned

    async def maybe_summarize(self) -> bool:
        if self.usage_fraction() < self.MICROCOMPACT_FRACTION:
            return False

        # Stage 1: cache-friendly in-place pruning of old tool outputs.
        if self._microcompact() > 0:
            self._last_prompt_tokens = None
            return True

        # Stage 2: nothing left to prune — full summarize-and-rebuild.
        free = self._max_context_tokens - self._used_tokens()
        if free >= self._proactive_threshold:
            return False

        summary = await self._build_summary()

        system = self._messages[0] if self._messages and self._messages[0].role == Role.SYSTEM else None
        task = next((m for m in self._messages if m.role == Role.USER), None)
        recent = self._recent_messages(exclude={id(system), id(task)})
        rebuilt: list[Message] = []
        if system:
            rebuilt.append(system)
        if task:
            rebuilt.append(task)
        rebuilt.append(
            Message(
                role=Role.USER,
                content=f"Conversation summary (context compacted):\n{summary}",
            )
        )
        rebuilt.extend(recent)
        self._messages = rebuilt
        # Provider count now reflects the pre-compaction prompt; invalidate it
        # so the next trigger uses a fresh estimate until the next response.
        self._last_prompt_tokens = None
        return True

    async def _build_summary(self) -> str:
        if self._enable_three_step_summary:
            try:
                return await summarize_three_step(self._model, self._messages, self._task)
            except Exception as exc:
                logger.warning(
                    "Three-step summarization failed (%s: %s); falling back to compact summary",
                    type(exc).__name__,
                    exc,
                )
        return self._compact_summary()

    def _recent_messages(self, exclude: set[int] | None = None) -> list[Message]:
        """Last N assistant turns, keeping tool-call/result pairing intact.

        Walks back until enough assistant turns are collected, then drops any
        leading TOOL messages whose assistant tool-call turn fell outside the
        window (an orphaned tool result is an invalid sequence for providers).
        """
        if self._keep_recent_turns <= 0:
            return []
        exclude = exclude or set()
        collected: list[Message] = []
        turns = 0
        for message in reversed(self._messages):
            if message.role == Role.SYSTEM or id(message) in exclude:
                continue
            if message.role == Role.ASSISTANT:
                turns += 1
            collected.append(message)
            if turns >= self._keep_recent_turns:
                break
        recent = list(reversed(collected))
        while recent and recent[0].role == Role.TOOL:
            recent.pop(0)
        return recent

    def _compact_summary(self) -> str:
        lines: list[str] = []
        for message in self._messages[-40:]:
            prefix = message.role.value
            snippet = (message.content or "")[:500]
            if message.tool_calls:
                calls = ", ".join(
                    f"{call.name}({str(call.arguments)[:200]})" for call in message.tool_calls
                )
                snippet = f"{snippet} [tool calls: {calls}]".strip()
            lines.append(f"- {prefix}: {snippet}")
        return "\n".join(lines)
