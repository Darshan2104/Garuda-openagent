from copy import deepcopy

from garuda.context.shaper import shape_observation
from garuda.model.protocol import Model
from garuda.types import Message, Role


class ContextManager:
    def __init__(
        self,
        model: Model,
        max_output_bytes: int = 30_720,
        proactive_threshold: int = 8000,
        max_context_tokens: int = 128_000,
    ):
        self._model = model
        self._max_output_bytes = max_output_bytes
        self._proactive_threshold = proactive_threshold
        self._max_context_tokens = max_context_tokens
        self._messages: list[Message] = []

    def seed(self, messages: list[Message]) -> None:
        self._messages = list(messages)

    def append(self, message: Message) -> None:
        self._messages.append(message)

    def get_messages(self) -> list[Message]:
        return list(self._messages)

    def shape_observation(self, output: str) -> str:
        return shape_observation(output, self._max_output_bytes)

    def fork(self) -> "ContextManager":
        forked = ContextManager(
            model=self._model,
            max_output_bytes=self._max_output_bytes,
            proactive_threshold=self._proactive_threshold,
            max_context_tokens=self._max_context_tokens,
        )
        forked._messages = deepcopy(self._messages)
        return forked

    async def maybe_summarize(self) -> bool:
        used = self._model.count_tokens(self._messages)
        free = self._max_context_tokens - used
        if free >= self._proactive_threshold:
            return False
        summary = self._compact_summary()
        system = self._messages[0] if self._messages and self._messages[0].role == Role.SYSTEM else None
        task = next((m for m in self._messages if m.role == Role.USER), None)
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
        self._messages = rebuilt
        return True

    def _compact_summary(self) -> str:
        lines: list[str] = []
        for message in self._messages[-40:]:
            prefix = message.role.value
            snippet = message.content[:500]
            lines.append(f"- {prefix}: {snippet}")
        return "\n".join(lines)
