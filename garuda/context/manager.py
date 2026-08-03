import hashlib
import json
import logging
import re
from copy import deepcopy
from typing import TYPE_CHECKING

from garuda.context.condenser import (
    Condenser,
    CondenserContext,
    MicrocompactCondenser,
    RecentWindowCondenser,
    make_condenser,
    reset_condenser,
)
from garuda.context.shaper import DEFAULT_MAX_OUTPUT_BYTES, shape_observation
from garuda.model.protocol import Model, count_request_tokens, estimate_tools_tokens
from garuda.types import Message, Role

if TYPE_CHECKING:
    from garuda.core.buffer import ToolOutputBuffer

logger = logging.getLogger(__name__)

# A vision model bills an image by its tiled area, which cannot be recovered from a
# data URI without decoding it. A flat, deliberately generous figure: overestimating
# costs an early compaction, underestimating costs a 400 from the provider.
IMAGE_TOKEN_ESTIMATE = 1_600

# Per-message envelope the provider adds around every message (role, delimiters).
MESSAGE_FRAMING_TOKENS = 4

# Adaptive output shaping. One tool result may claim this share of the *remaining*
# window, converted to bytes at the usual ~4 chars/token. A quarter is deliberately
# generous while there is room (the ceiling clamps it anyway) and bites only once
# free space is small enough that one result could meaningfully crowd out the rest.
OUTPUT_SHARE_OF_FREE = 0.25
BYTES_PER_TOKEN = 4

# Floors. Below these a result stops being usable at all, and an unusable result
# costs a turn to re-fetch — which is more expensive than the bytes it saved.
DEFAULT_MIN_OUTPUT_BYTES = 2_048
ERROR_OUTPUT_FLOOR_BYTES = 6_144

# Ceiling on the learned local-count shortfall. A gap beyond this is not request
# scaffolding, it is a bug or a provider counting something we have no model of,
# and carrying it into the budget would compact continuously.
MAX_COUNT_OVERHEAD = 20_000

# Subagent handoff modes. See ContextManager.fork.
FORK_NONE = "none"
FORK_BRIEF = "brief"
FORK_FULL = "full"
FORK_MODES = (FORK_NONE, FORK_BRIEF, FORK_FULL)

# Buffer pointers carried into a brief handoff. Enough to reach the run's real
# artifacts; not so many that the pointer list becomes the payload.
MAX_HANDOFF_BUFFERS = 12

# Matches the id in a `[buffer:<id> | ... ]` stub, same shape condenser.py uses.
_BUFFER_ID_RE = re.compile(r"\[buffer:([^\s|\]]+)")


def order_tool_results(messages: list[Message]) -> list[Message]:
    """Copy ``messages`` with each assistant ``tool_calls`` block kept adjacent to
    the tool messages answering it.

    Providers require the results for a ``tool_calls`` block to follow the assistant
    message *immediately*; anything in between is a hard 400 on OpenAI-family
    providers, which kills the run:

        An assistant message with 'tool_calls' must be followed by tool messages
        responding to each 'tool_call_id'.

    The gate used to append a USER-role note there and did exactly this on the first
    ``task_complete`` of every ``--mode eval`` run (fixed at source — see
    ``CompletionGate._defer``). This is the backstop for that whole class, because
    the cost of getting it wrong is the entire run rather than a degraded turn, and
    the code that appends to a transcript is spread across the loop, the gate, the
    steering queue and the context manager itself.

    Deliberately narrow. A displaced message is *moved to just after* the tool block
    it interrupted, never dropped or merged, and relative order is preserved within
    both groups. Only messages between an assistant ``tool_calls`` message and the
    next assistant/system boundary are considered, and a well-formed transcript —
    the overwhelmingly common case — returns a plain copy without inspection beyond
    one linear scan.
    """
    if not _has_displaced_tool_result(messages):
        return list(messages)
    logger.warning(
        "Transcript had a message between an assistant tool_calls block and its "
        "results; reordering so the request stays valid. This is a bug in whatever "
        "appended it — see ContextManager.order_tool_results."
    )
    ordered: list[Message] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        ordered.append(message)
        index += 1
        if message.role != Role.ASSISTANT or not message.tool_calls:
            continue
        expected = {call.id for call in message.tool_calls}
        results: list[Message] = []
        displaced: list[Message] = []
        while index < total and messages[index].role not in (Role.ASSISTANT, Role.SYSTEM):
            candidate = messages[index]
            if candidate.role == Role.TOOL and candidate.tool_call_id in expected:
                results.append(candidate)
            else:
                displaced.append(candidate)
            index += 1
        ordered.extend(results)
        ordered.extend(displaced)
    return ordered


def _has_displaced_tool_result(messages: list[Message]) -> bool:
    """True when some tool result is separated from its call block by another message."""
    for position, message in enumerate(messages):
        if message.role != Role.ASSISTANT or not message.tool_calls:
            continue
        expected = {call.id for call in message.tool_calls}
        interrupted = False
        scan = position + 1
        while scan < len(messages) and messages[scan].role not in (Role.ASSISTANT, Role.SYSTEM):
            candidate = messages[scan]
            if candidate.role == Role.TOOL and candidate.tool_call_id in expected:
                if interrupted:
                    return True
            else:
                interrupted = True
            scan += 1
    return False


def normalize_handoff(value: bool | str | None) -> str:
    """Coerce a handoff selector to a mode name.

    Accepts the older boolean because it is still the tool's parameter and still
    means what it always meant: True is the whole transcript. An unrecognised
    string falls back to ``none`` rather than raising — a model can put anything in
    a tool argument, and a bad value there should cost context, not the run.
    """
    if isinstance(value, bool):
        return FORK_FULL if value else FORK_NONE
    if isinstance(value, str) and value in FORK_MODES:
        return value
    if value:
        logger.warning("Unknown subagent handoff %r; starting the subagent cold", value)
    return FORK_NONE


def _json_chars(value) -> int:
    """Serialized length of a value the way the request path sends it."""
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _estimate_tokens(message: Message) -> int:
    """Cheap token estimate (~4 chars/token) for a single message, used only to
    keep the condensation trigger current between full counts.

    Counts everything the request path actually serializes, not just ``content``:
    tool-call arguments as JSON (the shape ``_serialize_tool_calls`` sends, not
    ``str(dict)``), echoed reasoning, and images. Anything omitted here is a
    systematic undercount that accumulates over a run, and the failure mode of an
    undercount is a context-window overflow rather than a compaction.
    """
    n = MESSAGE_FRAMING_TOKENS + len(message.content or "") // 4
    for call in message.tool_calls or []:
        n += _json_chars(call.arguments) // 4
    # Echoed reasoning is part of the prompt whenever the provider accepts it back
    # (see LitellmModel._serialization_flags), and it is not small.
    reasoning = message.metadata.get("reasoning_content")
    if reasoning:
        n += len(reasoning) // 4
    blocks = message.metadata.get("thinking_blocks")
    if blocks:
        n += _json_chars(blocks) // 4
    if message.images:
        n += IMAGE_TOKEN_ESTIMATE * len(message.images)
    return n


def _history_size(messages: list[Message]) -> int:
    """Total characters a history occupies, contents and tool arguments alike.

    The change detector for compaction. Message count will not do: a prune rewrites
    contents in place and hands back the same list, so counting messages reports
    "nothing happened" after a pass that removed most of the bytes.
    """
    total = 0
    for message in messages:
        total += len(message.content or "")
        for call in message.tool_calls or []:
            total += _json_chars(call.arguments)
    return total


def local_request_estimate(messages: list[Message], tools: list[dict] | None) -> int:
    """Our own estimate of a whole request, independent of the model's tokenizer.

    Exists to be a second opinion. ``litellm.token_counter`` has no tokenizer for
    many models and falls back to one that can be badly wrong: on
    minimax-m2.5 it read a real 3,623-token prompt as 2,530, a 30% undercount,
    most of it in the tool schemas. Being wrong low is the direction that overflows
    the window, so the gauge takes whichever of the two reads higher.
    """
    return sum(_estimate_tokens(m) for m in messages) + estimate_tools_tokens(tools)


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
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        proactive_threshold: int = 8000,
        max_context_tokens: int = 128_000,
        enable_three_step_summary: bool = True,
        task: str = "",
        keep_recent_turns: int = 12,
        condenser: Condenser | str | None = None,
        buffer: "ToolOutputBuffer | None" = None,
        reserved_output_tokens: int = 0,
        safety_margin_tokens: int = 0,
        tools_schema: list[dict] | None = None,
        adaptive_output: bool = True,
        min_output_bytes: int = DEFAULT_MIN_OUTPUT_BYTES,
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
        # Room held back for the model's own response. A context window is shared
        # between prompt and completion; budgeting the prompt against the whole
        # window puts the run at "90% full" exactly when a long answer overflows it.
        # Both default to 0 so a bare ContextManager behaves as it always has —
        # callers that know the run's shape (prepare_run) pass real figures.
        self._reserved_output_tokens = max(0, reserved_output_tokens)
        self._safety_margin_tokens = max(0, safety_margin_tokens)
        self._tools_schema = tools_schema
        self._tool_tokens: int | None = None
        self._adaptive_output = adaptive_output
        self._min_output_bytes = max(1, min(min_output_bytes, max_output_bytes))
        self._state_provider = None
        # Token accounting is anchor + delta, never a full recount per query.
        # ``_anchor_tokens`` covers everything in ``_messages`` at the moment it was
        # taken (from the provider when available, else one local full count);
        # ``_pending_tokens`` estimates what has been appended since. usage_fraction()
        # is called several times a turn, and re-tokenizing the whole history on each
        # call was the most expensive thing in the compaction path.
        self._anchor_tokens: int | None = None
        self._anchor_from_provider = False
        self._pending_tokens = 0
        # Fixed overhead a local count misses, learned from the provider. litellm's
        # tokenizer is not the provider's and neither sees the per-request scaffolding
        # the provider bills for. Measured on a real run: a full local count of 2,730
        # against a reported 3,575.
        #
        # Additive, not a ratio, because the gap is overhead and not a scaling error.
        # A 1.31x factor fitted to that same turn was tried and made things worse:
        # applied to the *incremental* per-message estimates — which were already
        # accurate to ~0.5% — it pushed every later turn to +1..16% instead. So this
        # is applied only when the whole history is re-counted, never to a delta.
        #
        # It cannot fix the very first turn of a run: there is no provider figure to
        # learn from yet. What it fixes is the re-count after each compaction, which
        # is where the risk actually is — the window is tightest exactly then, and an
        # undercount overflows instead of compacting.
        self._count_overhead = 0
        self._last_raw_anchor = 0
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

    def set_task(self, task: str) -> None:
        """Point this context at a different task.

        A forked subagent inherits its parent's history but has its own assignment,
        and the task is what summaries anchor to — leaving the parent's in place is
        how a subagent's compaction ends up summarizing toward the wrong goal.
        """
        if task:
            self._task = task

    def replace_system_message(self, system_prompt: str) -> None:
        """Swap the leading system message, inserting one if there is none.

        A subagent runs under its own persona; inheriting the parent's system prompt
        along with its history would make the profile choice meaningless.
        """
        message = Message(role=Role.SYSTEM, content=system_prompt)
        if self._messages and self._messages[0].role == Role.SYSTEM:
            self._messages[0] = message
        else:
            self._messages.insert(0, message)
        self._anchor_tokens = None
        self._pending_tokens = 0

    def append(self, message: Message) -> None:
        self._messages.append(message)
        # Only meaningful once an anchor exists — before that, the next _used_tokens()
        # counts the whole list anyway and would double-count this.
        if self._anchor_tokens is not None:
            self._pending_tokens += _estimate_tokens(message)

    def set_state_provider(self, provider) -> None:
        """Bind a callable returning the run's rendered working state.

        A callable rather than the state object: the card is assembled from the
        contract, the tool map and the ledger, none of which this package should
        have to know about. The condenser gets the result as given facts, so the
        model's summary can spend its call on findings instead of re-deriving a
        file list the harness already has exactly.
        """
        self._state_provider = provider

    def working_state(self) -> str:
        """The current working state, or "" when none is bound. Never raises —
        a failure here must degrade the summary, not end the run."""
        if self._state_provider is None:
            return ""
        try:
            return self._state_provider() or ""
        except Exception:
            logger.warning("Working-state provider failed", exc_info=True)
            return ""

    def attach_buffer(self, buffer: "ToolOutputBuffer | None") -> None:
        """Late-bind the session buffer (callers that reuse a context across runs
        create the buffer after the context exists). Never clobbers an existing one."""
        if buffer is not None and self._buffer is None:
            self._buffer = buffer

    def get_messages(self) -> list[Message]:
        return order_tool_results(self._messages)

    def output_budget(self, is_error: bool = False) -> int:
        """Bytes one tool result may occupy, given how full the window already is.

        A fixed cap spends the same on a 29 KB grep at 10% usage as at 95%, which is
        where a run burns its last turns re-reading things it cannot afford. The
        budget is a share of what is actually free, clamped between a floor that
        keeps a result useful and the configured ceiling — so at low usage nothing
        changes, and only under pressure does output start shrinking.

        Errors get the higher floor: an error is the message most likely to change
        the next action, and truncating it to a stub costs a turn of re-running.

        Never triggers a full token count — this is called once per tool result. If
        the gauge is not anchored yet, the run has barely started and the ceiling is
        the right answer anyway.
        """
        if not self._adaptive_output or not self.has_token_anchor():
            return self._max_output_bytes
        free = max(0, self.capacity() - self._used_tokens())
        allowance = int(free * BYTES_PER_TOKEN * OUTPUT_SHARE_OF_FREE)
        floor = max(self._min_output_bytes, ERROR_OUTPUT_FLOOR_BYTES) if is_error \
            else self._min_output_bytes
        return max(min(floor, self._max_output_bytes), min(self._max_output_bytes, allowance))

    def shape_observation(self, output: str, is_error: bool = False) -> str:
        return shape_observation(output, self.output_budget(is_error), is_error=is_error)

    def set_tools(self, tools_schema: list[dict] | None) -> None:
        """Record the tool schemas that ride on every request.

        They are the largest fixed cost in an agentic prompt — a full toolkit is
        routinely several thousand tokens — and a message-only count cannot see
        them. Identity-compared and cached because the schema list is built once
        per run and re-serializing it every turn would cost more than it measures.
        """
        if tools_schema is self._tools_schema:
            return
        self._tools_schema = tools_schema
        self._tool_tokens = None
        # A different toolkit means a different request size; re-anchor unless the
        # provider has already told us the truth for the current shape.
        if not self._anchor_from_provider:
            self._anchor_tokens = None
            self._pending_tokens = 0

    def tool_schema_tokens(self) -> int:
        """Estimated cost of the tool schemas, for reporting.

        Not added to ``_used_tokens`` — both anchor paths (provider count and full
        local count) already include the schemas. Reported separately so a
        trajectory shows how much of the window the toolkit is consuming.
        """
        if not self._tools_schema:
            return 0
        if self._tool_tokens is None:
            self._tool_tokens = estimate_tools_tokens(self._tools_schema)
        return self._tool_tokens

    def note_usage(self, usage: dict[str, int] | None) -> None:
        """Record provider-reported prompt tokens from the last response.

        Provider counts include tool schemas and message framing that local
        estimates miss, so they take priority for the condensation trigger.
        """
        if usage and usage.get("prompt_tokens"):
            self._learn_overhead(usage["prompt_tokens"])
            self._anchor_tokens = usage["prompt_tokens"]
            self._anchor_from_provider = True
            self._pending_tokens = 0

    def _learn_overhead(self, actual: int) -> None:
        """Update the fixed local-count shortfall from a provider figure.

        Learns only from a turn that was still on its local anchor with nothing
        appended since — that is the only moment the provider's number and the raw
        local count describe the same prompt. Once messages have been appended, the
        difference mixes in per-message estimate error and teaches the wrong thing.

        Floored at 0 deliberately, even though the tokenizer sometimes reads high.
        The two errors are not symmetric: overcounting compacts a little early,
        undercounting overflows the window and costs the turn.
        """
        if self._anchor_from_provider or self._pending_tokens or self._last_raw_anchor <= 0:
            return
        shortfall = actual - self._last_raw_anchor
        # The largest shortfall seen, not the latest. Samples are rare — one per
        # compaction — so there is no averaging to be had, and the same asymmetry
        # that floors this at 0 argues for keeping the worst case: overcounting
        # compacts a little early, undercounting overflows.
        self._count_overhead = max(
            self._count_overhead, min(shortfall, MAX_COUNT_OVERHEAD)
        )

    def fork(
        self, *, include_history: bool = True, mode: str | None = None
    ) -> "ContextManager":
        """A child context for a subagent.

        Three handoffs, because "nothing" and "everything" are both usually wrong:

        * ``none`` — a fresh context. The subagent starts blind and rediscovers
          whatever it needs.
        * ``brief`` — the working-state card plus the retrievable buffer ids. A
          couple of KB that carries the goal, the files touched, the checks run and
          the criteria, which is what a delegated subtask actually needs.
        * ``full`` — a deep copy of the transcript. Correct when the subagent has to
          reason about *how* the conversation got here, and expensive every other
          time: the parent's history is frequently the largest object in the run.

        ``include_history`` is the older boolean and still works: True is ``full``.
        """
        if mode is None:
            mode = FORK_FULL if include_history else FORK_NONE
        if mode not in (FORK_NONE, FORK_BRIEF, FORK_FULL):
            raise ValueError(
                f"Unknown fork mode {mode!r}. Options: {FORK_NONE}, {FORK_BRIEF}, {FORK_FULL}"
            )
        forked_condenser = deepcopy(self._condenser)
        if mode != FORK_FULL:
            # Tuning survives the copy; notes about the parent's conversation do not.
            # Only a full fork actually has the history those notes describe.
            reset_condenser(forked_condenser)
        forked = ContextManager(
            model=self._model,
            max_output_bytes=self._max_output_bytes,
            proactive_threshold=self._proactive_threshold,
            max_context_tokens=self._max_context_tokens,
            enable_three_step_summary=self._enable_three_step_summary,
            task=self._task,
            keep_recent_turns=self._keep_recent_turns,
            # A copy, never the same instance: a condenser carries running summary
            # state, and sharing it lets a fork's compaction overwrite the state its
            # parent will summarize from next. Deep-copied rather than reconstructed
            # — `type(c)()` would silently reset tuning (microcompact_fraction,
            # prune_min_chars, trigger_fraction) to defaults, and the Condenser
            # Protocol promises no no-arg constructor, so any strategy with a
            # required argument would raise here and take every subagent with it.
            # The conversation-specific half of that copy is dropped below.
            condenser=forked_condenser,
            buffer=self._buffer,
            reserved_output_tokens=self._reserved_output_tokens,
            safety_margin_tokens=self._safety_margin_tokens,
            tools_schema=self._tools_schema,
            adaptive_output=self._adaptive_output,
            min_output_bytes=self._min_output_bytes,
        )
        if mode == FORK_FULL:
            forked._messages = deepcopy(self._messages)
        elif mode == FORK_BRIEF:
            forked._messages = self._brief_handoff()
        return forked

    def _brief_handoff(self) -> list[Message]:
        """System prompt, plus what the parent knows, as one message.

        Deliberately not "the last N turns": a tail is an arbitrary window that may
        open mid-investigation and says nothing about the run as a whole, while the
        card is complete by construction and a fixed size. The buffer ids come along
        because they are how the subagent reaches anything the card summarizes.
        """
        handoff: list[Message] = []
        if self._messages and self._messages[0].role == Role.SYSTEM:
            handoff.append(deepcopy(self._messages[0]))
        card = self.working_state()
        buffers = self._buffer_ids()
        if buffers:
            card = f"{card}\n\n## Retrievable buffers\n" + "\n".join(
                f"  - buffer:{bid}" for bid in buffers
            ) + (
                "\nRetrieve with buffer_grep(buffer_id=..., pattern=...) or "
                "buffer_slice(buffer_id=..., start_line=N, end_line=M)."
            )
        if card.strip():
            handoff.append(
                Message(
                    role=Role.USER,
                    content=(
                        "[handoff from the parent agent — its state at the moment it "
                        f"delegated to you]\n{card}"
                    ),
                )
            )
        return handoff

    def _buffer_ids(self) -> list[str]:
        """Buffer ids referenced anywhere in the live history, in first-seen order."""
        seen: list[str] = []
        for message in self._messages:
            for bid in _BUFFER_ID_RE.findall(message.content or ""):
                if bid not in seen:
                    seen.append(bid)
        return seen[:MAX_HANDOFF_BUFFERS]

    def _used_tokens(self) -> int:
        """Tokens the next request will carry, as well as we can know it.

        Anchor + delta. The anchor is the provider's own ``prompt_tokens`` when we
        have one (authoritative — it includes framing we cannot see), else a single
        full count over the complete request shape, tool schemas included. Both are
        taken at most once per compaction; every call after that is O(1).
        """
        if self._anchor_tokens is None:
            # Whichever reads higher. The model's own counter is usually better, but
            # when it has no real tokenizer for the model it can be 30% low, and a
            # low read is the one that overflows rather than compacts.
            raw = max(
                count_request_tokens(self._model, self._messages, self._tools_schema),
                local_request_estimate(self._messages, self._tools_schema),
            )
            # Kept separately from the corrected anchor: the next provider figure is
            # compared against the raw count to learn the overhead, and comparing
            # against an already-corrected number would feed it back into itself.
            self._last_raw_anchor = raw
            self._anchor_tokens = raw + self._count_overhead
            self._anchor_from_provider = False
            self._pending_tokens = 0
        # Deltas are deliberately uncorrected. The overhead is per-request, paid once
        # by the prompt as a whole, and adding it again per message would compound.
        return self._anchor_tokens + self._pending_tokens

    def has_token_anchor(self) -> bool:
        """True when the gauge can be read without a full tokenizer pass."""
        return self._anchor_tokens is not None

    def capacity(self) -> int:
        """Prompt tokens actually available for the next request.

        The window is shared between prompt and completion, so the usable prompt
        budget is the window minus the room the response needs. Never below 1, so
        callers can divide by it.
        """
        return max(
            1,
            self._max_context_tokens
            - self._reserved_output_tokens
            - self._safety_margin_tokens,
        )

    def usage_fraction(self) -> float:
        if self._max_context_tokens <= 0:
            return 0.0
        return self._used_tokens() / self.capacity()

    def budget_snapshot(self) -> dict:
        """The current budget, for the trajectory. Cheap once anchored."""
        used = self._used_tokens()
        capacity = self.capacity()
        return {
            "used_tokens": used,
            "tool_schema_tokens": self.tool_schema_tokens(),
            "reserved_output_tokens": self._reserved_output_tokens,
            "safety_margin_tokens": self._safety_margin_tokens,
            "max_context_tokens": self._max_context_tokens,
            "capacity_tokens": capacity,
            "fraction": round(used / capacity, 4),
            "provider_anchored": self._anchor_from_provider,
            "count_overhead": self._count_overhead,
        }

    async def maybe_summarize(self) -> bool:
        """Ask the condenser whether/how to shrink history; apply if it does."""
        cx = CondenserContext(
            messages=self._messages,
            model=self._model,
            task=self._task,
            used_tokens=self._used_tokens(),
            # Usable capacity, not the raw window: a condenser deciding "how much
            # room is left" must not count room reserved for the response.
            max_context_tokens=self.capacity(),
            proactive_threshold=self._proactive_threshold,
            keep_recent_turns=self._keep_recent_turns,
            enable_three_step_summary=self._enable_three_step_summary,
            buffer=self._buffer,
            working_state=self.working_state(),
        )
        new_messages = await self._condenser.condense(cx)
        if new_messages is None:
            return False
        self._archive_dropped(new_messages)
        self._messages = new_messages
        # The anchor described the pre-condensation prompt; invalidate it so the
        # next read re-counts once against the history that actually remains.
        self._anchor_tokens = None
        self._anchor_from_provider = False
        self._pending_tokens = 0
        return True

    async def force_compact(self) -> bool:
        """Shrink history as hard as this context can, ignoring the gauge.

        For the case the gauge got wrong: the provider rejected the prompt as too
        long, so whatever ``usage_fraction`` believed, the real answer is "over".
        Runs the configured condenser against a deliberately impossible budget so
        every threshold in it trips, and if that still yields nothing — the usual
        reason being that there is no bulky tool output left to prune — falls back
        to dropping the middle of the conversation outright.

        Lossy by design, and only reached when the alternative is losing the run.
        Returns whether anything actually changed.
        """
        cx = CondenserContext(
            messages=self._messages,
            model=self._model,
            task=self._task,
            # Beyond every trigger fraction and below every free-token threshold.
            used_tokens=self.capacity() * 2,
            max_context_tokens=self.capacity(),
            proactive_threshold=self._proactive_threshold,
            keep_recent_turns=self._keep_recent_turns,
            enable_three_step_summary=self._enable_three_step_summary,
            buffer=self._buffer,
            working_state=self.working_state(),
        )
        before = _history_size(self._messages)
        new_messages = await self._condenser.condense(cx)
        if new_messages is not None:
            cx.messages = new_messages
        # Then drop the middle regardless. The configured condenser may only have
        # pruned, which shrinks content but can leave the prompt still too long, and
        # there is exactly one retry to get under the limit. Dropped messages are
        # archived to the buffer by _archive_dropped, so they stay retrievable.
        cx.keep_recent_turns = max(1, self._keep_recent_turns // 2)
        dropped = await RecentWindowCondenser(trigger_fraction=0.0).condense(cx)
        if dropped is not None:
            new_messages = dropped
        if new_messages is None:
            return False
        # Size, not message count: a prune rewrites contents in place and returns
        # the same list, so counting messages reports "nothing happened" after a
        # compaction that removed most of the bytes.
        if _history_size(new_messages) >= before and len(new_messages) >= len(self._messages):
            return False
        self._archive_dropped(new_messages)
        self._messages = new_messages
        self._anchor_tokens = None
        self._anchor_from_provider = False
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
