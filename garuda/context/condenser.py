"""Pluggable context-condensation strategies.

A `Condenser` decides, given the current conversation and a token-budget signal,
whether and how to shrink history. Strategies are swappable so callers can trade
fidelity for cost/latency:

* `MicrocompactCondenser` (default) — cache-friendly: first prunes bulky *old*
  tool outputs in place (stable prefix, good for prompt caching), and only when
  there is nothing left to prune does it fall back to a full LLM summarize.
* `RecentWindowCondenser` — no LLM: keep system + task + last N turns, drop the
  middle. Cheapest, lossy (OpenHands "recent events" style).
* `SummarizingCondenser` — always full 3-step summarize-and-rebuild.

Each strategy's `condense` returns a NEW message list, or None to leave history
unchanged this turn.
"""

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from garuda.context.summarizer import summarize_three_step
from garuda.model.protocol import Model
from garuda.types import Message, Role

logger = logging.getLogger(__name__)


@dataclass
class CondenserContext:
    """Everything a condenser needs to make and apply a decision."""

    messages: list[Message]
    model: Model
    task: str
    used_tokens: int
    max_context_tokens: int
    proactive_threshold: int
    keep_recent_turns: int
    enable_three_step_summary: bool = True

    @property
    def free_tokens(self) -> int:
        return self.max_context_tokens - self.used_tokens

    @property
    def usage_fraction(self) -> float:
        if self.max_context_tokens <= 0:
            return 0.0
        return self.used_tokens / self.max_context_tokens


@runtime_checkable
class Condenser(Protocol):
    async def condense(self, cx: CondenserContext) -> list[Message] | None: ...


# --- shared helpers ----------------------------------------------------------

PRUNE_MIN_CHARS = 500


def _window_boundary(messages: list[Message], keep_recent_turns: int) -> int:
    """Index where the recent window begins (keep_recent_turns assistant turns
    from the end); 0 if the whole history fits in the window."""
    turns = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == Role.ASSISTANT:
            turns += 1
            if turns >= keep_recent_turns:
                return index
    return 0


def microcompact_messages(
    messages: list[Message], keep_recent_turns: int, prune_min_chars: int = PRUNE_MIN_CHARS
) -> int:
    """Prune bulky tool outputs outside the recent window, in place.

    Preserves message structure (and thus prompt-cache prefixes) — only old
    tool-result *contents* are stubbed. Returns the number pruned.
    """
    boundary = _window_boundary(messages, keep_recent_turns)
    pruned = 0
    for message in messages[:boundary]:
        if (
            message.role == Role.TOOL
            and len(message.content or "") > prune_min_chars
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


def recent_messages(
    messages: list[Message], keep_recent_turns: int, exclude: set[int] | None = None
) -> list[Message]:
    """Last N assistant turns, dropping any leading orphaned TOOL messages
    (a tool result with no preceding assistant turn is an invalid sequence)."""
    if keep_recent_turns <= 0:
        return []
    exclude = exclude or set()
    collected: list[Message] = []
    turns = 0
    for message in reversed(messages):
        if message.role == Role.SYSTEM or id(message) in exclude:
            continue
        if message.role == Role.ASSISTANT:
            turns += 1
        collected.append(message)
        if turns >= keep_recent_turns:
            break
    result = list(reversed(collected))
    while result and result[0].role == Role.TOOL:
        result.pop(0)
    return result


def compact_summary(messages: list[Message]) -> str:
    """LLM-free fallback summary: a truncated transcript tail."""
    lines: list[str] = []
    for message in messages[-40:]:
        snippet = (message.content or "")[:500]
        if message.tool_calls:
            calls = ", ".join(
                f"{call.name}({str(call.arguments)[:200]})" for call in message.tool_calls
            )
            snippet = f"{snippet} [tool calls: {calls}]".strip()
        lines.append(f"- {message.role.value}: {snippet}")
    return "\n".join(lines)


def _rebuild_with_summary(messages: list[Message], summary: str, keep_recent_turns: int) -> list[Message]:
    system = messages[0] if messages and messages[0].role == Role.SYSTEM else None
    task = next((m for m in messages if m.role == Role.USER), None)
    recent = recent_messages(messages, keep_recent_turns, exclude={id(system), id(task)})
    rebuilt: list[Message] = []
    if system:
        rebuilt.append(system)
    if task:
        rebuilt.append(task)
    rebuilt.append(
        Message(role=Role.USER, content=f"Conversation summary (context compacted):\n{summary}")
    )
    rebuilt.extend(recent)
    return rebuilt


async def build_summary(cx: CondenserContext) -> str:
    if cx.enable_three_step_summary:
        try:
            return await summarize_three_step(cx.model, cx.messages, cx.task)
        except Exception as exc:
            logger.warning(
                "Three-step summarization failed (%s: %s); falling back to compact summary",
                type(exc).__name__,
                exc,
            )
    return compact_summary(cx.messages)


# --- strategies --------------------------------------------------------------

class MicrocompactCondenser:
    """Prune old tool outputs first (cache-friendly); summarize as last resort."""

    def __init__(self, microcompact_fraction: float = 0.75, prune_min_chars: int = PRUNE_MIN_CHARS):
        self.microcompact_fraction = microcompact_fraction
        self.prune_min_chars = prune_min_chars

    async def condense(self, cx: CondenserContext) -> list[Message] | None:
        if cx.usage_fraction < self.microcompact_fraction:
            return None
        if microcompact_messages(cx.messages, cx.keep_recent_turns, self.prune_min_chars) > 0:
            return list(cx.messages)
        if cx.free_tokens >= cx.proactive_threshold:
            return None
        summary = await build_summary(cx)
        return _rebuild_with_summary(cx.messages, summary, cx.keep_recent_turns)


class RecentWindowCondenser:
    """Keep only system + task + last N turns; no LLM call."""

    def __init__(self, trigger_fraction: float = 0.85):
        self.trigger_fraction = trigger_fraction

    async def condense(self, cx: CondenserContext) -> list[Message] | None:
        if cx.usage_fraction < self.trigger_fraction:
            return None
        system = cx.messages[0] if cx.messages and cx.messages[0].role == Role.SYSTEM else None
        task = next((m for m in cx.messages if m.role == Role.USER), None)
        recent = recent_messages(cx.messages, cx.keep_recent_turns, exclude={id(system), id(task)})
        rebuilt: list[Message] = []
        if system:
            rebuilt.append(system)
        if task:
            rebuilt.append(task)
        rebuilt.extend(recent)
        # Only report a change if we actually dropped messages.
        return rebuilt if len(rebuilt) < len(cx.messages) else None


class SummarizingCondenser:
    """Always full summarize-and-rebuild once over the proactive threshold."""

    async def condense(self, cx: CondenserContext) -> list[Message] | None:
        if cx.free_tokens >= cx.proactive_threshold:
            return None
        summary = await build_summary(cx)
        return _rebuild_with_summary(cx.messages, summary, cx.keep_recent_turns)


_STRATEGIES = {
    "microcompact": MicrocompactCondenser,
    "recent_window": RecentWindowCondenser,
    "summarizing": SummarizingCondenser,
}


def make_condenser(name: str) -> Condenser:
    try:
        return _STRATEGIES[name]()
    except KeyError:
        raise ValueError(
            f"Unknown condenser strategy {name!r}. Options: {sorted(_STRATEGIES)}"
        ) from None
