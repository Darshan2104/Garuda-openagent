from copy import deepcopy

from garuda.context.shaper import shape_observation
from garuda.context.summarizer import summarize_three_step
from garuda.model.protocol import Model
from garuda.types import Message, Role


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

    def seed(self, messages: list[Message]) -> None:
        self._messages = list(messages)
        task_message = next((m for m in messages if m.role == Role.USER), None)
        if task_message:
            self._task = task_message.content

    def append(self, message: Message) -> None:
        self._messages.append(message)

    def get_messages(self) -> list[Message]:
        return list(self._messages)

    def shape_observation(self, output: str) -> str:
        return shape_observation(output, self._max_output_bytes)

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

    def _estimate_tokens(self) -> int:
        return self._model.count_tokens(self._messages)

    async def maybe_summarize(self) -> bool:
        used = self._estimate_tokens()
        free = self._max_context_tokens - used
        turn_pairs = sum(1 for m in self._messages if m.role == Role.ASSISTANT)
        if free >= self._proactive_threshold and turn_pairs < self._keep_recent_turns * 2:
            return False

        if self._enable_three_step_summary:
            summary = await summarize_three_step(self._model, self._messages, self._task)
        else:
            summary = self._compact_summary()

        system = self._messages[0] if self._messages and self._messages[0].role == Role.SYSTEM else None
        task = next((m for m in self._messages if m.role == Role.USER), None)
        recent = self._recent_messages()
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
        return True

    def _recent_messages(self) -> list[Message]:
        if self._keep_recent_turns <= 0:
            return []
        collected: list[Message] = []
        turns = 0
        skipped_seed_user = False
        for message in reversed(self._messages):
            if message.role == Role.SYSTEM:
                continue
            if message.role == Role.USER and not skipped_seed_user:
                skipped_seed_user = True
                continue
            if message.role == Role.ASSISTANT:
                turns += 1
            collected.append(message)
            if turns >= self._keep_recent_turns:
                break
        return list(reversed(collected))

    def _compact_summary(self) -> str:
        lines: list[str] = []
        for message in self._messages[-40:]:
            prefix = message.role.value
            snippet = message.content[:500]
            lines.append(f"- {prefix}: {snippet}")
        return "\n".join(lines)
