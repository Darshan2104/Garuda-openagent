"""Turn-structured reading of a Garuda ``events.jsonl`` trajectory.

The event log is flat and append-only. Everything that wants to *explain* a run —
a dashboard, a triage script, a failure taxonomy — first has to rebuild the turn
structure from it, and every consumer that did so grew its own copy of the same
fragile rules. This module is that one copy.

It reads two things with one code path: a local session directory
(``<sessions_root>/<id>/events.jsonl``) and a Harbor trial (``<trial>/agent/events.jsonl``),
because the Harbor adapter writes the same format.

What makes this non-trivial, and why the rules below look the way they do:

* **A turn boundary is not a single event.** ``session_start``/``session_end`` frame
  the run, but nothing frames a turn. Turn-scoped events now carry ``payload["turn"]``
  and that is the primary signal — but logs written before that existed have to keep
  working, so the segmenter also derives boundaries positionally from the
  ``budget``/``turn_metrics`` pair a turn always emits.
* **``session_end`` is usually not the last event.** ``RunState.result()`` flushes the
  final ``turn_metrics`` *after* the loop has already emitted ``session_end``, on five
  of the nine ways a run can end. So ``session_end`` neither closes the open turn nor
  stops the walk, and ``Run.ended_at`` is the last event's timestamp.
* **Three event types are overloaded.** A ``model_response`` may be a truncation
  marker; a ``tool_result`` may be a steering failure-streak marker; a ``verification``
  may be the rigorous critic's phase-level verdict rather than a completion gate's.
  Each is discriminated before any generic handling — a naive type filter sees
  phantom tool results.
* **Nothing is dropped and nothing is invented.** Every event index lands in exactly
  one bucket (``Run.preamble_events``, ``Run.trailing_events``, or some
  ``Turn.events``), which is asserted as a disjoint cover in the tests: that single
  property is what proves the segmenter neither loses nor double-counts an event.
  Where the log cannot answer a question the answer is ``None`` with an
  :class:`Anomaly` explaining why, never a plausible zero.

Cross-references are integer indices, never object references, so
``dataclasses.asdict(run)`` is acyclic, non-duplicating, and directly serializable.
(Properties are not included by ``asdict`` — a caller wanting the computed
aggregates adds them explicitly.)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from garuda.eval.costs import estimate_cost

logger = logging.getLogger(__name__)

__all__ = [
    "read_trajectory",
    "build_run",
    "find_events_file",
    "EventTail",
    "TrajectoryReader",
    "Run",
    "Phase",
    "Turn",
    "ModelCall",
    "ToolStep",
    "GateRecord",
    "Compaction",
    "BudgetSnapshot",
    "Anomaly",
]

# --- tool step outcomes ------------------------------------------------------
# A step's status is the honest answer to "what happened to this call", including
# the cases where the log cannot say.
STATUS_OK = "ok"
STATUS_ERROR = "error"
#: A ``tool_call`` with no result: the run died mid-tool, or is still in flight.
STATUS_PENDING = "pending"
#: Denied by permissions — attributable because ``permission_ask`` carries the id.
STATUS_DENIED = "denied"
#: In the model's tool_calls list but never dispatched, and not a denial either:
#: blocked by a hook, or never reached because the turn ended first (an accepted
#: ``task_complete`` or a dead workspace stops the walk mid-response). Those are
#: genuinely indistinguishable on disk, because neither emits anything.
STATUS_UNEXECUTED = "unexecuted"
#: A tool event with no matching entry in any model response — a log whose
#: ``model_response`` was lost, or one from a build that did not record the list.
STATUS_ORPHAN = "orphan"
STATUS_GATE_APPROVED = "gate_approved"
STATUS_GATE_REJECTED = "gate_rejected"
#: A ``task_complete`` with no verdict event. Normal: the contract gate can reject
#: without emitting a ``verification`` at all.
STATUS_GATE_UNKNOWN = "gate_unknown"

KNOWN_EVENT_TYPES = frozenset(
    {
        "user_message",
        "model_response",
        "tool_call",
        "tool_result",
        "permission_ask",
        "verification",
        "summarization",
        "session_start",
        "session_end",
        "environment_snapshot",
        "environment_unavailable",
        "budget",
        "side_effects",
        "contract",
        "turn_metrics",
    }
)

#: Event types that may open a turn band on their own when no turn-bearing event
#: has been seen yet. Deliberately excludes the run-level types, which are handled
#: before the boundary rules.
_OPENS_A_BAND = frozenset({"model_response", "tool_call", "tool_result", "verification", "budget"})

_RIGOROUS_PLAN_PREFIX = "[rigorous:plan]"


def _records_turn_boundaries(events: list[dict[str, Any]]) -> bool:
    """Whether this log carries the anchors that make per-turn statements possible.

    Three of this module's warnings — no number, no budget snapshot, never finished —
    are claims *about a turn*, and a log can only support them if it marks where
    turns begin or end. Exactly three things do: a ``turn_metrics`` event, a
    ``budget`` preflight (``stage == "context"``), or ``turn`` on a ``model_response``.

    Older logs have none of them and still segment fine (one band per model
    response), so those three warnings would be properties of the *format* rather
    than findings about the run — and reported per turn they badge every turn of
    every archived trial as hung. Two real vintages on disk need this:

    * the 1.1.0 shape, with seven event types and no turn information at all;
    * a middle vintage that has ``budget`` events only for the *steering* stages
      (the floats 0.5/0.8 and ``final_turn``). Those carry a ``turn``, so a test for
      "does any event mention a turn" wrongly reads them as turn structure — three
      events out of several hundred re-enabling the warnings for every band.

    So the gate is boundary anchors specifically, not any mention of a turn.
    """
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        etype = event.get("type")
        if etype == "turn_metrics":
            return True
        if etype == "budget" and payload.get("stage") == "context":
            return True
        if etype == "model_response" and _explicit_turn(payload) is not None:
            return True
    return False


# --- defensive coercion ------------------------------------------------------
# Everything below reads numbers off disk: a log from an older build, a payload a
# provider stringified, a field that arrived as null. `json.dumps(..., default=str)`
# in EventStore.append means non-JSON values reach the file as their `str()`. A
# reporting tool must lose that cell, not the whole trajectory. (`eval/dashboard.py`
# carries the same helper for the same reason, on the same kind of data.)


def _as_int(value: Any, fallback: int | None = 0) -> int | None:
    if isinstance(value, bool) or value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any, fallback: float | None = None) -> float | None:
    if isinstance(value, bool) or value is None:
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


# --- records -----------------------------------------------------------------


@dataclass(frozen=True)
class Anomaly:
    """Something the log could not answer, or answered oddly.

    ``code`` is a stable machine token so callers and tests match on it without
    depending on prose; ``message`` is what a UI renders. Indices point into the
    parent's lists — ``turn_index`` into ``Run.turns`` (not a turn *number*, which
    is neither unique nor always known) and ``event_index`` into ``Run.events``.
    """

    code: str
    message: str
    turn_index: int | None = None
    event_index: int | None = None


@dataclass
class BudgetSnapshot:
    """The harness's own pre-call view of the context budget.

    From ``budget`` with ``stage == "context"``, which a turn emits once, before its
    model call. Emission is best-effort in the harness, so a turn can be missing one
    — rendered as a gap, never interpolated.
    """

    used_tokens: int | None = None
    capacity_tokens: int | None = None
    max_context_tokens: int | None = None
    fraction: float | None = None
    tool_schema_tokens: int | None = None
    reserved_output_tokens: int | None = None
    safety_margin_tokens: int | None = None
    provider_anchored: bool | None = None
    count_overhead: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BudgetSnapshot":
        return cls(
            used_tokens=_as_int(payload.get("used_tokens"), None),
            capacity_tokens=_as_int(payload.get("capacity_tokens"), None),
            max_context_tokens=_as_int(payload.get("max_context_tokens"), None),
            fraction=_as_float(payload.get("fraction")),
            tool_schema_tokens=_as_int(payload.get("tool_schema_tokens"), None),
            reserved_output_tokens=_as_int(payload.get("reserved_output_tokens"), None),
            safety_margin_tokens=_as_int(payload.get("safety_margin_tokens"), None),
            provider_anchored=payload.get("provider_anchored"),
            count_overhead=_as_int(payload.get("count_overhead"), None),
            raw=dict(payload),
        )


@dataclass
class Compaction:
    """One context-condensation pass.

    ``reason`` is None on the proactive path and ``"context_overflow"`` on the
    retry after a provider rejected the prompt. ``action`` distinguishes a cheap
    in-place prune from a summarize-and-rebuild that cost model calls and dropped
    history — the difference that explains a run which forgot something.
    """

    turn_number: int | None = None
    reason: str | None = None
    duration_ms: float | None = None
    strategy: str | None = None
    action: str | None = None
    pruned: int = 0
    messages_before: int | None = None
    messages_after: int | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None
    #: Overflow path only: what the gauge read, and against what capacity.
    recovered: bool | None = None
    gauge_said: int | None = None
    capacity: int | None = None
    timestamp: str | None = None
    event_index: int = -1

    @classmethod
    def from_payload(cls, payload: dict[str, Any], timestamp: str | None, index: int) -> "Compaction":
        return cls(
            turn_number=_as_int(payload.get("turn"), None),
            reason=payload.get("reason"),
            duration_ms=_as_float(payload.get("duration_ms")),
            strategy=payload.get("strategy"),
            action=payload.get("action"),
            pruned=_as_int(payload.get("pruned"), 0) or 0,
            messages_before=_as_int(payload.get("messages_before"), None),
            messages_after=_as_int(payload.get("messages_after"), None),
            tokens_before=_as_int(payload.get("tokens_before"), None),
            tokens_after=_as_int(payload.get("tokens_after"), None),
            recovered=payload.get("recovered"),
            gauge_said=_as_int(payload.get("gauge_said"), None),
            capacity=_as_int(payload.get("capacity"), None),
            timestamp=timestamp,
            event_index=index,
        )


@dataclass
class ToolStep:
    """One entry in a model response's ``tool_calls`` list, and what became of it.

    The model response's list is the spine, not the ``tool_call`` events: it is
    where the ids come from, and it is the only place a call appears when it was
    denied, blocked, or never reached. ``task_complete`` exists here for exactly
    that reason — the loop intercepts it before the runner, so it emits no
    ``tool_call``/``tool_result`` pair at all.
    """

    index: int
    call_id: str | None
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    #: What the model asked for, when a before-hook rewrote the call. Populated only
    #: on divergence, which is itself a signal worth surfacing.
    requested_arguments: dict[str, Any] | None = None
    status: str = STATUS_UNEXECUTED
    content: str | None = None
    is_error: bool = False
    #: From ``payload["duration_ms"]`` only. Never a timestamp difference: a parallel
    #: batch emits every ``tool_call`` at dispatch, so differencing reports nonsense.
    duration_ms: float | None = None
    denial_reason: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    #: Index into the owning ``Turn.gates``, for a ``task_complete`` step.
    gate_index: int | None = None
    call_event_index: int | None = None
    result_event_index: int | None = None


@dataclass
class ModelCall:
    content: str | None = None
    reasoning: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    #: Priced per call, never from summed usage: ``costs.merge_usage`` drops
    #: ``cost_usd``, so merging first would discard every provider-reported invoice
    #: and silently fall back to a table estimate.
    cost_usd: float | None = None
    duration_ms: float | None = None
    #: A truncation marker followed this response: the provider cut it at max_tokens.
    truncated: bool = False
    tool_steps: list[ToolStep] = field(default_factory=list)
    timestamp: str | None = None
    event_index: int = -1


@dataclass
class GateRecord:
    """One completion-gate verdict, or the rigorous critic's.

    The two shapes are incompatible — the critic carries no checklist and no
    evidence — so ``kind`` says which, and the missing fields stay empty rather
    than being faked.

    ``attempt`` is kept verbatim and not normalised, because its three producers
    mean three different things by it (the gate's rejection count, a contract
    yield's streak, a rigorous repair round). Normalising would invent a number.
    """

    kind: str = "completion"
    approved: bool | None = None
    attempt: int | None = None
    summary: str | None = None
    verification_commands: list[str] = field(default_factory=list)
    answer_rationale: str | None = None
    checklist: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    feedback: str | None = None
    contract_action: str | None = None
    outstanding: list[str] = field(default_factory=list)
    turn_number: int | None = None
    timestamp: str | None = None
    event_index: int | None = None


@dataclass
class Turn:
    """One pass through the agent loop.

    ``index`` is the unique key; ``number`` is the harness's turn number, which is
    neither unique (the final-submission band reuses it) nor always recoverable.
    """

    index: int
    number: int | None = None
    #: ``"final_submission"`` for the extra exchange spent when the budget ran out.
    label: str | None = None
    phase_index: int = 0
    model_calls: list[ModelCall] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    budget: BudgetSnapshot | None = None
    #: Non-``context`` budget events verbatim. Separate because ``stage`` is
    #: ``str | float`` — the review nudges use the float thresholds 0.5 and 0.8, so
    #: anything treating ``stage`` as a string crashes or mis-buckets.
    budget_reviews: list[dict[str, Any]] = field(default_factory=list)
    gates: list[GateRecord] = field(default_factory=list)
    #: The ``turn_metrics`` payload, verbatim. Not mirrored into typed fields: a
    #: second copy of ``core/metrics.py``'s 15 fields would be a copy that can be
    #: wrong, and would break on a log from a newer build. The four numbers the
    #: aggregates need are promoted below, coerced.
    metrics: dict[str, Any] | None = None
    model_ms: float = 0.0
    tool_ms_total: float = 0.0
    tool_wall_ms: float = 0.0
    tool_errors: int = 0
    permission_denials: list[dict[str, Any]] = field(default_factory=list)
    #: The steering failure-streak marker's value, when this turn tripped it.
    failure_steer: int | None = None
    environment_unavailable: dict[str, Any] | None = None
    #: The nested runs this turn handed work to, in order. Each is the parent's verbatim
    #: handoff payload — ``{subagent, success, session_id, turns, task, content}`` — not the
    #: subagent's own turns, which live in a separate log because they were recorded on a
    #: separate store. This field is what makes them findable: the id joins the two.
    subagents: list[dict[str, Any]] = field(default_factory=list)
    #: ``user_message`` payloads that landed inside this band — steering, a resumed prompt,
    #: an interjection. Turn 1's input is ``Run.task``; for every later turn the input is the
    #: previous turn's tool results *unless* one of these intervened, so a reader that cannot
    #: see them describes the wrong input.
    user_messages: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None
    ended_at: str | None = None
    #: A ``turn_metrics`` was seen for this band. False means the run was killed
    #: here, or is still in flight.
    finished: bool = False
    events: list[int] = field(default_factory=list)
    warnings: list[Anomaly] = field(default_factory=list)

    @property
    def tool_steps(self) -> list[ToolStep]:
        return [step for call in self.model_calls for step in call.tool_steps]

    @property
    def prompt_tokens(self) -> int:
        return sum(call.prompt_tokens for call in self.model_calls)

    @property
    def completion_tokens(self) -> int:
        return sum(call.completion_tokens for call in self.model_calls)

    @property
    def cache_read_tokens(self) -> int:
        return sum(call.cache_read_tokens for call in self.model_calls)

    @property
    def cost_usd(self) -> float | None:
        costs = [call.cost_usd for call in self.model_calls]
        if not costs or any(cost is None for cost in costs):
            return None
        return round(sum(costs), 8)  # type: ignore[arg-type]

    @property
    def is_extra(self) -> bool:
        """A labelled band — a real model call that is not a working turn."""
        return self.label is not None


@dataclass
class Phase:
    """A stretch of turns under one rigorous sub-agent, as an index range.

    A range over the flat ``Run.turns`` rather than a nested tree, so ``Turn.index``
    stays globally unique (what a table needs for row keys) and the structure
    serializes without duplicating turn objects. A non-rigorous run is one phase.

    The critic verdict lives here rather than on a turn because it is emitted
    *between* turns; attaching it to one would be a lie about when it happened.
    """

    index: int
    kind: str = "main"
    attempt: int | None = None
    turn_start: int = 0
    turn_end: int = 0
    summary: str | None = None
    critic: GateRecord | None = None


@dataclass
class Run:
    session_id: str | None = None
    task: str | None = None
    #: Only ever from ``session_start``. There is no per-call model name anywhere,
    #: so a run that switched models mid-flight is not representable.
    model: str | None = None
    agent: str | None = None
    mode: str | None = None
    #: The resolved posture from ``session_start``. ``None`` for a log written before
    #: it was emitted — which is why gate absence in an old log stays ambiguous.
    config: dict[str, Any] | None = None
    permission_mode: str | None = None
    started_at: str | None = None
    #: The **last event's** timestamp, not ``session_end``'s — see the module docstring.
    ended_at: str | None = None
    #: ``None`` when no ``session_end`` was seen. A killed run must not read as failed.
    success: bool | None = None
    end_reason: str | None = None
    end_via: str | None = None
    #: ``session_end.payload["turns"]``, absent on five of its nine shapes.
    reported_turns: int | None = None
    session_end: dict[str, Any] | None = None
    complete: bool = False
    turns: list[Turn] = field(default_factory=list)
    phases: list[Phase] = field(default_factory=list)
    preamble_events: list[int] = field(default_factory=list)
    trailing_events: list[int] = field(default_factory=list)
    environment_snapshot_chars: int | None = None
    side_effects: dict[str, Any] | None = None
    contract_events: list[dict[str, Any]] = field(default_factory=list)
    permission_denials: list[dict[str, Any]] = field(default_factory=list)
    subagent_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Verbatim, and the source of truth for every index in this graph.
    events: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[Anomaly] = field(default_factory=list)
    source: str | None = None

    # -- projections ----------------------------------------------------------

    @property
    def working_turns(self) -> list[Turn]:
        """Unlabelled turns — the count ``RunMetrics.summary()["turns"]`` reports."""
        return [turn for turn in self.turns if turn.label is None]

    @property
    def turn_count(self) -> int:
        return len(self.working_turns)

    @property
    def tool_steps(self) -> list[ToolStep]:
        return [step for turn in self.turns for step in turn.tool_steps]

    @property
    def gates(self) -> list[GateRecord]:
        return [gate for turn in self.turns for gate in turn.gates]

    @property
    def usage(self) -> dict[str, int]:
        """Token totals across every turn, labelled bands included.

        Matches ``RunMetrics.summary()``'s split, where distributions come from
        working turns but totals sum every record.
        """
        return {
            "prompt_tokens": sum(turn.prompt_tokens for turn in self.turns),
            "completion_tokens": sum(turn.completion_tokens for turn in self.turns),
            "cache_read_tokens": sum(turn.cache_read_tokens for turn in self.turns),
        }

    @property
    def cost_usd(self) -> float | None:
        """Summed per-call cost, or ``None`` if anything priceable could not be priced.

        Understating spend silently is the failure ``eval/costs.py`` exists to
        prevent, so one unpriceable call makes the whole figure unavailable and
        raises ``cost_unavailable`` rather than quietly omitting it.
        """
        calls = [call for turn in self.turns for call in turn.model_calls]
        priceable = [call for call in calls if call.usage]
        if not priceable or any(call.cost_usd is None for call in priceable):
            return None
        return round(sum(call.cost_usd for call in priceable), 8)  # type: ignore[arg-type]

    @property
    def warning_codes(self) -> frozenset[str]:
        return frozenset(anomaly.code for anomaly in self.warnings)

    @property
    def gate_stack(self) -> dict[str, Any] | None:
        """Just the gate switches from ``config``, or None if the log predates it."""
        if not self.config:
            return None
        from garuda.core.modes import GATE_FIELDS

        return {name: self.config.get(name) for name in GATE_FIELDS if name in self.config}

    def to_dict(self, *, include_events: bool = True) -> dict[str, Any]:
        """JSON-ready form, with the computed aggregates ``asdict`` cannot see.

        ``include_events=False`` drops the verbatim log for callers that only want
        the structure. Indices are **not** renumbered when it is dropped — they
        still address ``Run.events`` as loaded, so a client that wants to resolve
        one must fetch the log separately.
        """
        payload = asdict(self)
        if not include_events:
            payload.pop("events", None)
        payload.update(
            {
                "turn_count": self.turn_count,
                "usage": self.usage,
                "cost_usd": self.cost_usd,
                "warning_codes": sorted(self.warning_codes),
                "gate_stack": self.gate_stack,
            }
        )
        for turn, turn_payload in zip(self.turns, payload["turns"], strict=True):
            turn_payload.update(
                {
                    "prompt_tokens": turn.prompt_tokens,
                    "completion_tokens": turn.completion_tokens,
                    "cache_read_tokens": turn.cache_read_tokens,
                    "cost_usd": turn.cost_usd,
                    "is_extra": turn.is_extra,
                }
            )
        return payload


# --- payload discrimination --------------------------------------------------


def _explicit_turn(payload: dict[str, Any]) -> int | None:
    """``payload["turn"]``, coerced.

    Deliberately type-agnostic — it reads the key off *any* event rather than a
    whitelist — so the harness adding ``turn`` to another in-turn event makes it a
    boundary signal with no change here. ``True`` is excluded explicitly because
    ``bool`` is an ``int`` in Python and would otherwise read as turn 1.
    """
    value = payload.get("turn")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_truncation_marker(payload: dict[str, Any]) -> bool:
    """A ``model_response`` that is a max_tokens marker, not a response.

    A real one always carries ``content`` and ``usage`` (both set unconditionally by
    ``_record_response``); the marker carries neither.
    """
    return payload.get("truncated") is True and "content" not in payload and "usage" not in payload


def _is_failure_steer(payload: dict[str, Any]) -> bool:
    """A ``tool_result`` that is really the steering failure-streak marker."""
    return "failure_streak" in payload and not payload.get("tool_call_id")


def _is_critic_verification(payload: dict[str, Any]) -> bool:
    return payload.get("phase") == "critic"


def _call_key(payload: dict[str, Any]) -> Any:
    """``tool_call`` uses ``id``; ``tool_result`` uses ``tool_call_id``.

    Accepted on both sides, the same tolerance ``observability/tracing.py`` applies,
    so neither an old log nor a hand-written one falls through the matcher.
    """
    return payload.get("tool_call_id") or payload.get("id")


# --- build-time band --------------------------------------------------------


class _Band:
    """Mutable turn under construction. Converted to a :class:`Turn` at the end."""

    __slots__ = (
        "number",
        "label",
        "phase_index",
        "model_calls",
        "compactions",
        "budget",
        "budget_reviews",
        "metrics",
        "permission_denials",
        "failure_steer",
        "environment_unavailable",
        "subagents",
        "user_messages",
        "events",
        "started_at",
        "ended_at",
        "finished",
        "pending_calls",
        "pending_results",
        "denials_by_id",
        "verdicts",
        "has_real_response",
        "warnings",
    )

    def __init__(self, number: int | None, label: str | None, phase_index: int):
        self.number = number
        self.label = label
        self.phase_index = phase_index
        self.model_calls: list[ModelCall] = []
        self.compactions: list[Compaction] = []
        self.budget: BudgetSnapshot | None = None
        self.budget_reviews: list[dict[str, Any]] = []
        self.metrics: dict[str, Any] | None = None
        self.permission_denials: list[dict[str, Any]] = []
        self.failure_steer: int | None = None
        self.environment_unavailable: dict[str, Any] | None = None
        self.subagents: list[dict[str, Any]] = []
        self.user_messages: list[dict[str, Any]] = []
        self.events: list[int] = []
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.finished = False
        # (event_index, payload) awaiting a spine entry to claim them.
        self.pending_calls: list[tuple[int, dict]] = []
        self.pending_results: list[tuple[int, dict]] = []
        self.denials_by_id: dict[Any, dict] = {}
        self.verdicts: list[tuple[int, dict]] = []
        self.has_real_response = False
        self.warnings: list[Anomaly] = []


def _pop_by_id(entries: list[tuple[int, dict]], call_id: Any) -> tuple[int, dict] | None:
    if call_id is None:
        return None
    for position, (_index, payload) in enumerate(entries):
        if _call_key(payload) == call_id:
            return entries.pop(position)
    return None


def _pop_unclaimed(entries: list[tuple[int, dict]], name: str) -> tuple[int, dict] | None:
    """Earliest unclaimed entry **of this name**, for a log whose ids do not line up.

    Deliberately does not fall back to "earliest of any name". That wider fallback
    mis-attributes rather than degrading: given a response asking for `write_file`
    (denied, so it emits no event) and `read_file` (which does), the denied entry
    would claim the read's event and both steps would report the wrong outcome —
    the read as unexecuted and the denied write as successful. An entry with no
    same-named event has no event; the leftover becomes a flagged orphan instead.

    Claim-once still matters within a name: without it two same-named calls in one
    response both bind the first result, which is the bug
    ``atif_export._append_tool_result`` documents.
    """
    for position, (_index, payload) in enumerate(entries):
        if payload.get("name") == name:
            return entries.pop(position)
    return None


# --- the walk ----------------------------------------------------------------


def build_run(
    events: list[dict[str, Any]],
    *,
    source: str | None = None,
    extra_warnings: Sequence[Anomaly] = (),
) -> Run:
    """Turn a flat event list into a :class:`Run`.

    Takes a plain ``list[dict]`` — the same shape ``tracing.emit_spans_from_events``
    and ``atif_export.events_to_atif`` take — so an in-memory store
    (``build_run(store.get_all())``), a result's metadata, and a file on disk all
    converge on one function with no adapter.
    """
    run = Run(events=events, source=source, warnings=list(extra_warnings))
    if not events:
        run.warnings.append(Anomaly("empty_log", "The trajectory contains no events."))
        return run
    structured = _records_turn_boundaries(events)

    bands: list[_Band] = []
    band: _Band | None = None
    phases: list[Phase] = [Phase(index=0, kind="main")]
    # Suppresses one `turn_number_regressed` when a phase marker already explained
    # the reset that follows it.
    phase_just_closed = False

    def warn(code: str, message: str, *, event_index: int | None = None) -> None:
        run.warnings.append(
            Anomaly(code, message, turn_index=len(bands) - 1 if bands else None, event_index=event_index)
        )

    def open_band(number: int | None, label: str | None = None) -> _Band:
        nonlocal band
        band = _Band(number=number, label=label, phase_index=len(phases) - 1)
        bands.append(band)
        return band

    def close_phase(*, kind: str, attempt: int | None = None, summary: str | None = None,
                    critic: GateRecord | None = None) -> None:
        current = phases[-1]
        current.kind = kind
        current.turn_end = len(bands)
        if attempt is not None:
            current.attempt = attempt
        if summary is not None:
            current.summary = summary
        if critic is not None:
            current.critic = critic

    def open_phase(kind: str, attempt: int | None = None) -> None:
        phases.append(Phase(index=len(phases), kind=kind, attempt=attempt,
                            turn_start=len(bands), turn_end=len(bands)))

    def bucket(index: int) -> None:
        """An event that belongs to no turn: preamble before turn 1, else trailing."""
        (run.trailing_events if bands else run.preamble_events).append(index)

    def attach_or_bucket(index: int) -> None:
        if band is None:
            bucket(index)
        else:
            band.events.append(index)

    for index, event in enumerate(events):
        etype = event.get("type")
        timestamp = event.get("timestamp")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            warn("payload_not_a_dict", f"Event {index} ({etype}) has a non-dict payload.",
                 event_index=index)
            payload = {}
        if etype not in KNOWN_EVENT_TYPES:
            warn("unknown_event_type", f"Event {index} has unrecognised type {etype!r}.",
                 event_index=index)
        if run.session_id is None:
            run.session_id = event.get("session_id")
        turn_number = _explicit_turn(payload)

        # -- run-level: recorded, never a boundary ---------------------------
        if etype == "session_start":
            run.task = run.task or payload.get("task")
            run.model = run.model or payload.get("model")
            run.agent = run.agent or payload.get("agent")
            run.mode = run.mode or payload.get("mode")
            run.permission_mode = run.permission_mode or payload.get("permission_mode")
            if run.config is None and isinstance(payload.get("config"), dict):
                run.config = payload["config"]
            run.started_at = run.started_at or timestamp
            # Learned from the first event, so the opening phase can be named before
            # any turn is parsed.
            if run.mode == "rigorous" and phases[0].kind == "main" and not bands:
                phases[0].kind = "plan"
            bucket(index)
            continue

        if etype == "session_end":
            # NOT a terminator: `turn_metrics` follows it on five of the nine end
            # paths, because RunState.result() flushes metrics after the loop has
            # already emitted this. The band stays open.
            run.session_end = payload
            run.complete = True
            run.success = payload.get("success")
            run.end_reason = payload.get("reason")
            run.end_via = payload.get("via")
            run.reported_turns = _as_int(payload.get("turns"), None)
            run.trailing_events.append(index)
            continue

        if etype == "environment_snapshot":
            run.environment_snapshot_chars = _as_int(payload.get("chars"), None)
            bucket(index)
            continue

        if etype == "user_message":
            content = payload.get("content") or ""
            if "subagent" in payload:
                # Emitted into the *parent* store from inside invoke_subagent, i.e.
                # between that tool's call and its result. Not a boundary of any kind.
                run.subagent_calls.append(payload)
                if band is not None:
                    # Also on the band, so the turn that delegated shows what it delegated
                    # to. `run.subagent_calls` stays as the flat run-level list; this is the
                    # same payload seen from the turn, not a second record of it.
                    band.subagents.append(payload)
                attach_or_bucket(index)
                continue
            if content.startswith(_RIGOROUS_PLAN_PREFIX):
                # A phase *close* marker: it fires after the plan sub-run returned,
                # so the plan's turns are all behind it.
                close_phase(kind="plan", summary=content[len(_RIGOROUS_PLAN_PREFIX):].strip())
                open_phase("execute", attempt=0)
                phase_just_closed = True
                bucket(index)
                continue
            run.task = run.task or content
            if band is not None:
                # A user message inside an open band is what *opened* the next exchange —
                # steering, a resumed conversation's prompt, or the failure-streak nudge. The
                # UI shows it as the turn's input, and without it the only visible input a
                # turn has is the previous turn's tool results, which is wrong exactly when
                # something intervened.
                band.user_messages.append(payload)
            bucket(index)
            continue

        # -- boundaries, in priority order -----------------------------------
        if etype == "budget" and payload.get("stage") == "final_submission":
            # B1. Highest priority: this band reuses the previous turn's number, so
            # B2 structurally cannot see it. Marker and band are co-present in the
            # harness — the event is emitted immediately before the labelled record
            # is opened.
            open_band(number=turn_number, label="final_submission")
            phase_just_closed = False
        elif turn_number is not None and (
            band is None or (band.number is not None and band.number != turn_number)
        ):
            # B2. An explicit turn number that differs from the open band's.
            if (
                band is not None
                and band.number is not None
                and turn_number < band.number
                and not phase_just_closed
            ):
                warn(
                    "turn_number_regressed",
                    f"Turn numbering went from {band.number} back to {turn_number} with no "
                    "phase marker to explain it.",
                    event_index=index,
                )
                close_phase(kind=phases[-1].kind)
                open_phase("unknown")
            open_band(number=turn_number)
            phase_just_closed = False
        elif (
            etype == "turn_metrics"
            and payload.get("label")
            and band is not None
            and band.label is None
            and band.finished
        ):
            # A labelled record landing on an already-closed unlabelled band means
            # B1's marker was missing (a truncated or older log). Open the band late
            # rather than overwriting the working turn's metrics.
            open_band(number=turn_number, label=payload.get("label"))
        elif (
            etype == "model_response"
            and not _is_truncation_marker(payload)
            and band is not None
            and band.has_real_response
        ):
            # B3. Positional fallback. Exactly one real response per turn, so a
            # second means the next turn started with no turn-bearing event — the
            # case where the best-effort budget snapshot was skipped. Named later
            # by the turn_metrics backfill below.
            open_band(number=None)
        elif band is None and etype in _OPENS_A_BAND:
            open_band(number=None)

        if band is None:
            bucket(index)
            continue

        band.events.append(index)
        if band.started_at is None:
            band.started_at = timestamp
        band.ended_at = timestamp

        # -- fill -------------------------------------------------------------
        if etype == "budget":
            if payload.get("stage") == "context":
                band.budget = BudgetSnapshot.from_payload(payload)
            elif payload.get("stage") != "final_submission":
                # `stage` may be a float here (the 0.5/0.8 review thresholds), so it
                # is kept verbatim rather than bucketed by string.
                band.budget_reviews.append(payload)

        elif etype == "model_response":
            if _is_truncation_marker(payload):
                if band.model_calls:
                    band.model_calls[-1].truncated = True
                else:
                    warn("orphan_truncation_marker",
                         "A truncation marker arrived with no model response to attach it to.",
                         event_index=index)
            else:
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                band.model_calls.append(
                    ModelCall(
                        content=payload.get("content"),
                        reasoning=payload.get("reasoning"),
                        usage=usage or {},
                        prompt_tokens=_as_int(usage.get("prompt_tokens")) or 0,
                        completion_tokens=_as_int(usage.get("completion_tokens")) or 0,
                        cache_read_tokens=_as_int(usage.get("cache_read_tokens")) or 0,
                        duration_ms=_as_float(payload.get("duration_ms")),
                        timestamp=timestamp,
                        event_index=index,
                    )
                )
                band.has_real_response = True

        elif etype == "tool_call":
            band.pending_calls.append((index, payload))

        elif etype == "tool_result":
            if _is_failure_steer(payload):
                band.failure_steer = _as_int(payload.get("failure_streak"), None)
            else:
                band.pending_results.append((index, payload))

        elif etype == "permission_ask":
            run.permission_denials.append(payload)
            band.permission_denials.append(payload)
            key = payload.get("id")
            if key is not None:
                band.denials_by_id[key] = payload

        elif etype == "summarization":
            band.compactions.append(Compaction.from_payload(payload, timestamp, index))

        elif etype == "side_effects":
            run.side_effects = payload

        elif etype == "contract":
            run.contract_events.append(payload)
            if payload.get("action") in ("gate_reject", "gate_yield"):
                band.verdicts.append((index, payload))

        elif etype == "verification":
            if _is_critic_verification(payload):
                # Closes one executor round. Emitted between turns, so it belongs to
                # the phase, not to a turn.
                attempt = _as_int(payload.get("attempt"), None)
                close_phase(
                    kind="execute",
                    attempt=attempt,
                    critic=_gate_from_verification(payload, timestamp, index, kind="critic"),
                )
                open_phase("execute", attempt=None if attempt is None else attempt + 1)
                phase_just_closed = True
            else:
                band.verdicts.append((index, payload))

        elif etype == "environment_unavailable":
            band.environment_unavailable = payload

        elif etype == "turn_metrics":
            # Late naming: this is what makes B3 usable, by supplying the number the
            # positional fallback could not know.
            if band.number is None:
                band.number = turn_number
            if band.label is None and payload.get("label"):
                band.label = payload.get("label")
            band.metrics = payload
            band.finished = True

    run.ended_at = events[-1].get("timestamp")
    turns = [_finalise_band(b, i, run, structured) for i, b in enumerate(bands)]
    run.turns = turns
    close_phase(kind=phases[-1].kind)
    run.phases = _tidy_phases(phases, turns)
    _price_calls(run)
    _run_level_warnings(run, structured)
    return run


def _gate_from_verification(
    payload: dict[str, Any], timestamp: str | None, index: int, *, kind: str
) -> GateRecord:
    return GateRecord(
        kind=kind,
        approved=payload.get("approved"),
        attempt=_as_int(payload.get("attempt"), None),
        checklist=payload.get("checklist") or {},
        evidence=payload.get("evidence") or [],
        feedback=payload.get("feedback"),
        turn_number=_explicit_turn(payload),
        timestamp=timestamp,
        event_index=index,
    )


def _gate_from_contract(payload: dict[str, Any], timestamp: str | None, index: int) -> GateRecord:
    action = payload.get("action")
    return GateRecord(
        kind="completion",
        # A contract gate_reject/gate_yield is a refusal: the criteria are not met.
        approved=False,
        attempt=_as_int(payload.get("attempts"), None),
        contract_action=action,
        outstanding=list(payload.get("outstanding") or []),
        timestamp=timestamp,
        event_index=index,
    )


def _finalise_band(band: _Band, index: int, run: Run, structured: bool = True) -> Turn:
    """Resolve one band's tool steps and gates, then freeze it into a Turn."""
    turn = Turn(
        index=index,
        number=band.number,
        label=band.label,
        phase_index=band.phase_index,
        model_calls=band.model_calls,
        compactions=band.compactions,
        budget=band.budget,
        budget_reviews=band.budget_reviews,
        metrics=band.metrics,
        permission_denials=band.permission_denials,
        failure_steer=band.failure_steer,
        environment_unavailable=band.environment_unavailable,
        subagents=band.subagents,
        user_messages=band.user_messages,
        started_at=band.started_at,
        ended_at=band.ended_at,
        finished=band.finished,
        events=band.events,
        warnings=list(band.warnings),
    )
    if band.metrics:
        turn.model_ms = _as_float(band.metrics.get("model_ms"), 0.0) or 0.0
        turn.tool_ms_total = _as_float(band.metrics.get("tool_ms_total"), 0.0) or 0.0
        turn.tool_wall_ms = _as_float(band.metrics.get("tool_wall_ms"), 0.0) or 0.0
        turn.tool_errors = _as_int(band.metrics.get("tool_errors")) or 0

    for call in band.model_calls:
        spine = run.events[call.event_index].get("payload", {}).get("tool_calls") or []
        for position, requested in enumerate(spine):
            if not isinstance(requested, dict):
                continue
            call.tool_steps.append(_resolve_step(band, position, requested))

    # Anything left over never appeared in a model response's list.
    leftovers = [(i, p, True) for i, p in band.pending_calls]
    leftovers += [(i, p, False) for i, p in band.pending_results]
    if leftovers:
        owner = band.model_calls[-1] if band.model_calls else None
        for event_index, payload, is_call in sorted(leftovers):
            step = ToolStep(
                index=len(owner.tool_steps) if owner else 0,
                call_id=_call_key(payload),
                name=payload.get("name") or "unknown",
                arguments=payload.get("arguments") or {},
                status=STATUS_ORPHAN,
                content=None if is_call else payload.get("content"),
                is_error=bool(payload.get("is_error")),
                duration_ms=_as_float(payload.get("duration_ms")),
                call_event_index=event_index if is_call else None,
                result_event_index=None if is_call else event_index,
            )
            if owner is not None:
                owner.tool_steps.append(step)
            turn.warnings.append(
                Anomaly(
                    "orphan_tool_event",
                    f"A {'tool_call' if is_call else 'tool_result'} for {step.name!r} has no "
                    "entry in any model response's tool_calls list.",
                    turn_index=index,
                    event_index=event_index,
                )
            )

    _resolve_gates(band, turn, index)
    _turn_level_warnings(turn, index, structured)
    run.warnings.extend(turn.warnings)
    return turn


def _resolve_step(band: _Band, position: int, requested: dict[str, Any]) -> ToolStep:
    call_id = requested.get("id")
    name = requested.get("name") or "unknown"
    requested_args = requested.get("arguments") or {}

    call_event = _pop_by_id(band.pending_calls, call_id) or _pop_unclaimed(band.pending_calls, name)
    result_event = _pop_by_id(band.pending_results, call_id) or _pop_unclaimed(
        band.pending_results, name
    )
    denial = band.denials_by_id.pop(call_id, None) if call_id is not None else None

    # The hook may have rewritten the call: run_one emits the event with the
    # rewritten arguments while the model response holds what was asked for.
    effective_args = call_event[1].get("arguments") if call_event else None
    step = ToolStep(
        index=position,
        call_id=call_id,
        name=name,
        arguments=effective_args if effective_args is not None else requested_args,
        requested_arguments=(
            requested_args if effective_args is not None and effective_args != requested_args else None
        ),
        call_event_index=call_event[0] if call_event else None,
        result_event_index=result_event[0] if result_event else None,
        started_at=None,
        ended_at=None,
    )
    if result_event:
        payload = result_event[1]
        step.content = payload.get("content")
        step.is_error = bool(payload.get("is_error"))
        step.duration_ms = _as_float(payload.get("duration_ms"))
        step.status = STATUS_ERROR if step.is_error else STATUS_OK
    elif call_event:
        step.status = STATUS_PENDING
    elif denial is not None:
        step.status = STATUS_DENIED
        step.denial_reason = denial.get("reason")
    else:
        step.status = STATUS_UNEXECUTED

    if name == "task_complete":
        # Never dispatched — the loop intercepts it — so the gate pass decides.
        step.status = STATUS_GATE_UNKNOWN
    return step


def _resolve_gates(band: _Band, turn: Turn, turn_index: int) -> None:
    """Pair ``task_complete`` steps with verdict events, in order."""
    steps = [step for call in band.model_calls for step in call.tool_steps
             if step.name == "task_complete"]
    verdicts = band.verdicts
    for position in range(max(len(steps), len(verdicts))):
        step = steps[position] if position < len(steps) else None
        verdict = verdicts[position] if position < len(verdicts) else None
        if verdict is None:
            # A submission with no verdict: normal, the contract gate can reject
            # while emitting only a `contract` event. Left as gate_unknown.
            continue
        event_index, payload = verdict
        timestamp = None
        if payload.get("action"):
            gate = _gate_from_contract(payload, timestamp, event_index)
        else:
            gate = _gate_from_verification(payload, timestamp, event_index, kind="completion")
        if step is not None:
            gate.summary = step.arguments.get("summary")
            gate.verification_commands = list(step.arguments.get("verification_commands") or [])
            gate.answer_rationale = step.arguments.get("answer_rationale")
        turn.gates.append(gate)
        if step is not None:
            step.gate_index = len(turn.gates) - 1
            step.status = STATUS_GATE_APPROVED if gate.approved else STATUS_GATE_REJECTED
        else:
            turn.warnings.append(
                Anomaly(
                    "gate_verdict_unmatched",
                    "A completion verdict has no task_complete call to attribute it to.",
                    turn_index=turn_index,
                    event_index=event_index,
                )
            )


def _turn_level_warnings(turn: Turn, index: int, structured: bool = True) -> None:
    real_calls = len(turn.model_calls)
    if real_calls == 0:
        turn.warnings.append(Anomaly("no_model_call",
                                     f"Turn {turn.number} recorded no model response.",
                                     turn_index=index))
    elif real_calls > 1:
        turn.warnings.append(Anomaly("multiple_model_calls",
                                     f"Turn {turn.number} recorded {real_calls} model responses.",
                                     turn_index=index))
    if not turn.finished and structured:
        turn.warnings.append(
            Anomaly(
                "turn_never_finished",
                f"TURN NEVER FINISHED: turn {turn.number} has no turn_metrics event; the run was "
                "killed here or is still in flight.",
                turn_index=index,
            )
        )
    if turn.number is None and structured:
        turn.warnings.append(Anomaly("turn_number_unknown",
                                     "This turn's number could not be determined.",
                                     turn_index=index))
    if turn.budget is None and structured:
        turn.warnings.append(
            Anomaly("missing_budget_snapshot",
                    f"Turn {turn.number} has no context budget snapshot; the harness emits it "
                    "best-effort, so the pressure series has a gap here.",
                    turn_index=index)
        )
    for step in turn.tool_steps:
        if step.status == STATUS_PENDING:
            turn.warnings.append(
                Anomaly("tool_call_unanswered",
                        f"{step.name!r} was dispatched and never returned a result.",
                        turn_index=index, event_index=step.call_event_index)
            )
        elif step.status == STATUS_UNEXECUTED:
            turn.warnings.append(
                Anomaly("tool_step_unexecuted",
                        f"{step.name!r} was requested but never dispatched: blocked by a hook, or "
                        "never reached because the turn ended first.",
                        turn_index=index)
            )


def _tidy_phases(phases: list[Phase], turns: list[Turn]) -> list[Phase]:
    """Drop empty trailing phases and renumber, rewriting each turn's back-reference."""
    kept = [
        phase
        for phase in phases
        if phase.turn_end > phase.turn_start or phase.critic is not None or phase.summary
    ]
    if not kept:
        kept = [phases[0]]
    remap = {phase.index: position for position, phase in enumerate(kept)}
    for position, phase in enumerate(kept):
        phase.index = position
    for turn in turns:
        # A turn whose phase was dropped belongs to the nearest surviving one.
        turn.phase_index = remap.get(turn.phase_index, max(0, len(kept) - 1))
    return kept


def _price_calls(run: Run) -> None:
    """Price each model call individually, once the model name is known."""
    for turn in run.turns:
        for call in turn.model_calls:
            if call.usage:
                call.cost_usd = estimate_cost(run.model, call.usage)


def _run_level_warnings(run: Run, structured: bool = True) -> None:
    if not structured:
        run.warnings.append(
            Anomaly(
                "legacy_log_format",
                "This log records no turn structure (no turn_metrics, no budget snapshots, no "
                "turn numbers), so turns were segmented one-per-model-response and per-turn "
                "timing, context pressure and completion state are unavailable for the whole run.",
            )
        )
    if run.model is None:
        run.warnings.append(Anomaly("no_session_start",
                                    "No session_start event: model, agent and posture are unknown."))
    if not run.complete:
        run.warnings.append(
            Anomaly("no_session_end",
                    "No session_end event: the run's outcome is unknown, not failed.")
        )
    unpriced = [
        call
        for turn in run.turns
        for call in turn.model_calls
        if call.usage and call.cost_usd is None
    ]
    if unpriced:
        run.warnings.append(
            Anomaly("cost_unavailable",
                    f"{len(unpriced)} of this run's model calls could not be priced, so the total "
                    "is unavailable rather than understated.")
        )
    numbered = [turn for turn in run.working_turns if turn.number is not None]
    seen: dict[int, int] = {}
    for turn in numbered:
        seen[turn.number] = seen.get(turn.number, 0) + 1  # type: ignore[index]
    for number, count in sorted(seen.items()):
        if count > 1:
            run.warnings.append(
                Anomaly("duplicate_turn_number",
                        f"Turn number {number} appears on {count} unlabelled turns.")
            )
    if numbered:
        expected = set(range(min(t.number for t in numbered), max(t.number for t in numbered) + 1))
        gaps = sorted(expected - {t.number for t in numbered})
        if gaps:
            run.warnings.append(
                Anomaly("turn_number_gap",
                        f"No turn recorded for number(s) {', '.join(str(g) for g in gaps)}.")
            )


# --- reading from disk -------------------------------------------------------


class EventTail:
    """Byte-offset cursor over an append-only ``events.jsonl``.

    Advances only past complete, newline-terminated lines. ``EventStore.append``
    opens, writes and closes per event, so an append is not atomic: a cursor that
    ran past the last newline would land mid-object and the reader would desync
    permanently. Holding the torn tail back costs one poll and fixes that.

    Binary mode is required — a byte offset is only well defined in bytes — and
    cutting on ``b"\\n"`` is safe there because 0x0A cannot appear inside a
    multi-byte UTF-8 sequence.
    """

    def __init__(self, path: str | Path, *, offset: int = 0) -> None:
        self._path = Path(path)
        self._offset = offset
        self._inode: int | None = None
        self._lines = 0
        self._restarts = 0
        self._warnings: list[Anomaly] = []

    @property
    def path(self) -> Path:
        return self._path

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def restarts(self) -> int:
        """How many times the cursor has restarted from 0 after a truncation.

        A counter rather than a flag on ``warnings``, because warnings accumulate for
        the life of the tail: a consumer testing "did the file just get replaced?"
        against the warning list would see every poll after the first as a restart
        and discard its accumulated events forever.
        """
        return self._restarts

    @property
    def warnings(self) -> list[Anomaly]:
        return list(self._warnings)

    def reset(self) -> None:
        self._offset = 0
        self._lines = 0
        self._inode = None
        self._warnings.clear()

    def read_new(self) -> list[dict[str, Any]]:
        """Events appended since ``offset``, advancing it. ``[]`` when unchanged."""
        try:
            stat = self._path.stat()
        except OSError:
            # Not written yet, or gone. Neither is an error for a live tail.
            return []

        if self._inode is not None and (stat.st_size < self._offset or stat.st_ino != self._inode):
            # Shrunk or replaced. `events.save()` overwrites, so a re-run Harbor
            # trial dir does exactly this; reading on would return garbage.
            self._warnings.append(
                Anomaly("file_truncated",
                        f"{self._path} shrank or was replaced; the cursor restarted from 0.")
            )
            self._offset = 0
            self._lines = 0
            self._restarts += 1
        self._inode = stat.st_ino

        if stat.st_size == self._offset:
            return []
        with self._path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()

        cut = chunk.rfind(b"\n")
        if cut < 0:
            if chunk:
                self._warnings.append(
                    Anomaly("torn_tail",
                            f"{self._path} ends mid-line at byte {self._offset}; held back.")
                )
            return []
        complete, remainder = chunk[: cut + 1], chunk[cut + 1:]
        if remainder:
            self._warnings.append(
                Anomaly("torn_tail",
                        f"{self._path} ends mid-line at byte {self._offset + cut + 1}; held back.")
            )
        self._offset += cut + 1

        out: list[dict[str, Any]] = []
        for raw in complete.split(b"\n"):
            if not raw.strip():
                continue  # blank lines are skipped silently, as EventStore.load does
            self._lines += 1
            try:
                out.append(json.loads(raw.decode("utf-8", errors="replace")))
            except json.JSONDecodeError:
                self._warnings.append(
                    Anomaly("malformed_line",
                            f"Line {self._lines} of {self._path} is not valid JSON; skipped.")
                )
        return out


def find_events_file(root: str | Path) -> Path | None:
    """Resolve a file, a session directory, or a Harbor trial directory to its log.

    Checked in order: the path itself if it is a file; ``<root>/events.jsonl``
    (a session); ``<root>/agent/events.jsonl`` (a Harbor trial). Deliberately not a
    recursive glob, so the answer is deterministic and a nested trial cannot be
    picked up in place of the one that was asked for.
    """
    candidate = Path(root)
    if candidate.is_file():
        return candidate
    for relative in ("events.jsonl", Path("agent") / "events.jsonl"):
        found = candidate / relative
        if found.is_file():
            return found
    return None


def read_trajectory(path: str | Path) -> Run:
    """Read one trajectory from a file, session dir, or Harbor trial dir."""
    resolved = find_events_file(path)
    if resolved is None:
        raise FileNotFoundError(f"No events.jsonl found at or under {path}")
    tail = EventTail(resolved)
    events = tail.read_new()
    return build_run(events, source=str(resolved), extra_warnings=tail.warnings)


class TrajectoryReader:
    """A :class:`Run` kept current against a growing ``events.jsonl``.

    The tail is incremental; the derivation is not. ``build_run`` is a few dict
    lookups per event — sub-millisecond for a long run — while re-reading and
    re-parsing the file is the expensive part, and :class:`EventTail` removes that.
    A resumable segmenter would need checkpointed state for the open band, the phase
    stack and the pending-claim sets, i.e. a second code path through exactly the
    logic whose edge cases are the hard part. One tested path is worth more.
    """

    def __init__(self, path: str | Path) -> None:
        resolved = find_events_file(path)
        self._path = Path(resolved) if resolved else Path(path)
        self._tail = EventTail(self._path)
        self._events: list[dict[str, Any]] = []
        self._run: Run | None = None
        self._seen_restarts = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run(self) -> Run | None:
        return self._run

    @property
    def event_count(self) -> int:
        return len(self._events)

    def refresh(self) -> Run:
        """Consume the tail and re-derive.

        Returns the *same* object when nothing was appended, so a caller can use
        ``if run is not previous`` as a free change check instead of diffing.
        """
        new_events = self._tail.read_new()
        if self._tail.restarts != self._seen_restarts:
            # The cursor restarted from zero, so what it just returned is the whole
            # file. Extending would double every event that survived the rewrite.
            self._seen_restarts = self._tail.restarts
            self._events = list(new_events)
        elif new_events:
            self._events.extend(new_events)
        elif self._run is not None:
            return self._run
        self._run = build_run(
            self._events, source=str(self._path), extra_warnings=self._tail.warnings
        )
        return self._run
