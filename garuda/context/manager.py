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
    make_condenser,
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
        return list(self._messages)

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
            self._anchor_tokens = usage["prompt_tokens"]
            self._anchor_from_provider = True
            self._pending_tokens = 0

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
        forked = ContextManager(
            model=self._model,
            max_output_bytes=self._max_output_bytes,
            proactive_threshold=self._proactive_threshold,
            max_context_tokens=self._max_context_tokens,
            enable_three_step_summary=self._enable_three_step_summary,
            task=self._task,
            keep_recent_turns=self._keep_recent_turns,
            # A fresh condenser of the same strategy, never the same instance: a
            # condenser carries running summary state, and sharing it lets a fork's
            # compaction overwrite the state its parent will summarize from next.
            condenser=type(self._condenser)(),
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
            self._anchor_tokens = count_request_tokens(
                self._model, self._messages, self._tools_schema
            )
            self._anchor_from_provider = False
            self._pending_tokens = 0
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
