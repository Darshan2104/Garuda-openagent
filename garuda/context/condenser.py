"""Pluggable context-condensation strategies.

A `Condenser` decides, given the current conversation and a token-budget signal,
whether and how to shrink history. Strategies are swappable so callers can trade
fidelity for cost/latency:

* `MicrocompactCondenser` (default) — cheaper than summarizing: first prunes bulky
  *old* tool outputs in place (no LLM call; message structure and tool_call ids stay
  valid), and only when there is nothing left to prune does it fall back to a full
  LLM summarize. A prune event resets the prompt cache from the first pruned message,
  so prunes are batched (all eligible outputs at once) and triggered infrequently
  (only at high usage) to keep cache-miss events rare. Buffered outputs keep their
  `buffer:<id>` retrieval pointer through pruning.
* `RecentWindowCondenser` — no LLM: keep system + task + last N turns, drop the
  middle. Cheapest, lossy (OpenHands "recent events" style).
* `SummarizingCondenser` — always full 3-step summarize-and-rebuild.

Each strategy's `condense` returns a NEW message list, or None to leave history
unchanged this turn.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from garuda.context.summarizer import summarize_incremental, summarize_three_step
from garuda.model.protocol import Model
from garuda.types import Message, Role

if TYPE_CHECKING:
    from garuda.core.buffer import ToolOutputBuffer

logger = logging.getLogger(__name__)

# Matches the id in a `[buffer:<id> | ... ]` stub produced by the tool-output buffer.
_BUFFER_ID_RE = re.compile(r"\[buffer:([^\s|\]]+)")


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
    # Session tool-output buffer, when available. Condensers use it to demote
    # content to disk instead of destroying it (retrievable via buffer_grep/slice).
    buffer: "ToolOutputBuffer | None" = None
    # The harness's own structured record of the run (see context/state_card.py),
    # already rendered. Passed to the summarizer as established fact so the model
    # is asked for judgement rather than recall, and carried into the rebuilt
    # history so those facts survive verbatim rather than through a paraphrase.
    working_state: str = ""

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

    # Optional. Drop any running state accumulated about a *particular*
    # conversation, keeping tuning. Called on a copy handed to a context that does
    # not share the history that state describes. Strategies holding no such state
    # need not implement it; callers must go through ``reset_condenser`` below.
    def reset(self) -> None: ...


def reset_condenser(condenser: object) -> None:
    """Clear a condenser's conversation-specific state, if it has any."""
    reset = getattr(condenser, "reset", None)
    if callable(reset):
        reset()


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


def _prune_tool_call_arguments(message: Message, prune_min_chars: int) -> bool:
    """Stub oversized string values inside an old assistant turn's tool arguments.

    Only the *values* are replaced, never the argument keys or the call's id/name:
    the shape stays a valid arguments object, so providers that validate it against
    the tool schema still accept the replayed history, and the model can still see
    *which* call it made and with which parameters.
    """
    if message.metadata.get("args_pruned"):
        return False
    changed = False
    for call in message.tool_calls or []:
        arguments = getattr(call, "arguments", None)
        if not isinstance(arguments, dict):
            continue
        for key, value in list(arguments.items()):
            if isinstance(value, str) and len(value) > prune_min_chars:
                arguments[key] = (
                    f"[pruned to save context: {len(value)} chars of "
                    f"{key!r} passed to {call.name}]"
                )
                changed = True
    if changed:
        message.metadata["args_pruned"] = True
    return changed


def microcompact_messages(
    messages: list[Message],
    keep_recent_turns: int,
    prune_min_chars: int = PRUNE_MIN_CHARS,
    buffer: "ToolOutputBuffer | None" = None,
) -> int:
    """Prune bulky tool outputs outside the recent window, in place.

    Preserves message structure (and thus prompt-cache prefixes) — only old
    tool-result *contents* are stubbed. Returns the number pruned.
    """
    boundary = _window_boundary(messages, keep_recent_turns)
    pruned = 0
    for message in messages[:boundary]:
        # An assistant turn's own tool *arguments* can dwarf the result it produced —
        # a whole-file write_file carries the entire file. Pruning only tool results
        # left that text in the window for the rest of the run.
        if message.role == Role.ASSISTANT and message.tool_calls:
            if _prune_tool_call_arguments(message, prune_min_chars):
                pruned += 1
        if (
            message.role == Role.TOOL
            and len(message.content or "") > prune_min_chars
            and not message.metadata.get("pruned")
        ):
            original_len = len(message.content)
            # If this output was captured in the tool-output buffer, keep the
            # retrieval pointer so the agent can still pull it back after pruning
            # (losing it would strand data that IS still available on disk).
            buffer_id = message.metadata.get("buffer_id")
            if not buffer_id:
                match = _BUFFER_ID_RE.search(message.content or "")
                buffer_id = match.group(1) if match else None
            if not buffer_id and buffer is not None:
                # Demote, don't destroy: outputs below the live buffering threshold
                # were never stored, and re-running the tool is not always possible
                # (stateful commands). Same id derivation as the loop's stubs, so
                # ids stay short and can never collide with an already-stored one.
                try:
                    seed = message.tool_call_id or message.content
                    store_id = "buf_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
                    buffer.store(store_id, message.content, tool_name=message.name or "")
                    buffer_id = store_id
                except Exception:
                    logger.warning("Prune-time buffer store failed", exc_info=True)
            if buffer_id:
                message.content = (
                    f"[tool output pruned to save context: was {original_len} chars. "
                    f'Full output retained in buffer:{buffer_id} — retrieve with '
                    f'buffer_grep/buffer_slice(buffer_id="{buffer_id}").]'
                )
            else:
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


def _rebuild_with_summary(
    messages: list[Message],
    summary: str,
    keep_recent_turns: int,
) -> list[Message]:
    system = messages[0] if messages and messages[0].role == Role.SYSTEM else None
    task = next((m for m in messages if m.role == Role.USER), None)
    recent = recent_messages(messages, keep_recent_turns, exclude={id(system), id(task)})
    rebuilt: list[Message] = []
    if system:
        rebuilt.append(system)
    if task:
        rebuilt.append(task)
    # The summary only. The working-state card is not embedded here even though the
    # summarizer was given it: whoever owns the card re-pins it after every
    # compaction (see run_state.reinject_pinned_state), at the end of the history
    # where it is most visible. Writing it here too produced two copies whenever a
    # prune-then-summarize sequence left the earlier pin inside the recent window.
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

# Once a summary rebuild happens, don't summarize again until the conversation has
# grown by at least this many messages — unless usage is critically high — so we
# don't pay a 3-call summarize every single turn (the "re-summarize cliff").
_RESUMMARIZE_MIN_GROWTH = 8
_CRITICAL_FRACTION = 0.92


class MicrocompactCondenser:
    """Prune old tool outputs first (cache-friendly); summarize as last resort."""

    def __init__(self, microcompact_fraction: float = 0.75, prune_min_chars: int = PRUNE_MIN_CHARS):
        self.microcompact_fraction = microcompact_fraction
        self.prune_min_chars = prune_min_chars
        self._last_summary_len = 0
        # Running structured state, maintained incrementally across compactions so
        # summary quality doesn't drift over long horizons (vs re-deriving prose).
        self._state = ""

    def reset(self) -> None:
        """Forget this conversation, keep the tuning.

        Both fields below describe one specific history. Carried into a context
        that does not have that history — a subagent handed a state card rather
        than a transcript — ``_state`` becomes notes about work the child never
        did, which its own summarize would then fold its transcript into; and a
        high ``_last_summary_len`` suppresses the child's first summarize until its
        message count passes a number it had no part in reaching.
        """
        self._state = ""
        self._last_summary_len = 0

    async def condense(self, cx: CondenserContext) -> list[Message] | None:
        if cx.usage_fraction < self.microcompact_fraction:
            return None
        if microcompact_messages(cx.messages, cx.keep_recent_turns, self.prune_min_chars, cx.buffer) > 0:
            return list(cx.messages)
        if cx.free_tokens >= cx.proactive_threshold:
            return None
        # Guard against re-summarizing every turn after a rebuild (nothing left to
        # prune, still over threshold): require real growth, unless we're critical.
        if (
            len(cx.messages) <= self._last_summary_len + _RESUMMARIZE_MIN_GROWTH
            and cx.usage_fraction < _CRITICAL_FRACTION
        ):
            return None
        summary = await self._summarize(cx)
        rebuilt = _rebuild_with_summary(cx.messages, summary, cx.keep_recent_turns)
        self._last_summary_len = len(rebuilt)
        return rebuilt

    async def _summarize(self, cx: CondenserContext) -> str:
        """Incrementally fold the history into a persisted structured state (falls
        back to the LLM-free compact summary on error or when LLM summary is off)."""
        if not cx.enable_three_step_summary:
            return compact_summary(cx.messages)
        try:
            self._state = await summarize_incremental(
                cx.model, self._state, cx.messages, cx.task, cx.working_state
            )
            return self._state
        except Exception as exc:
            logger.warning(
                "Incremental summarize failed (%s: %s); using compact summary",
                type(exc).__name__,
                exc,
            )
            return compact_summary(cx.messages)


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
