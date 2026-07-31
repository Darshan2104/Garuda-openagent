"""Context budget: request preflight, adaptive output shaping, working state, handoffs.

Covers the four things the budget got wrong: it counted messages instead of the
request, spent a flat per-result cap regardless of pressure, kept its structured
state as prose the model re-derived, and offered subagents only "nothing" or
"the whole transcript".
"""

import json

import pytest

from garuda.context.condenser import MicrocompactCondenser, RecentWindowCondenser
from garuda.context.manager import (
    ERROR_OUTPUT_FLOOR_BYTES,
    FORK_BRIEF,
    FORK_FULL,
    FORK_NONE,
    ContextManager,
    local_request_estimate,
    normalize_handoff,
)
from garuda.context.shaper import shape_observation
from garuda.context.state_card import CheckRecord, WorkingState
from garuda.model.protocol import count_request_tokens, estimate_tools_tokens
from garuda.model.script_model import ScriptModel
from garuda.types import Message, Role, ToolCall

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the workspace and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The command"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to read"}},
                "required": ["path"],
            },
        },
    },
]


class CountingModel(ScriptModel):
    """ScriptModel that records how often the token counter is asked to run."""

    def __init__(self):
        super().__init__(responses=[])
        self.count_calls = 0

    def count_tokens(self, messages):
        self.count_calls += 1
        return super().count_tokens(messages)


def _messages(n: int, content: str = "x" * 400) -> list[Message]:
    out = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="task"),
    ]
    for i in range(n):
        out.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})],
            )
        )
        out.append(Message(role=Role.TOOL, content=content, name="bash", tool_call_id=f"c{i}"))
    return out


# --- 1. the counter sees the whole request ----------------------------------


def test_estimate_tools_tokens_scales_with_schema_size():
    assert estimate_tools_tokens(None) == 0
    assert estimate_tools_tokens([]) == 0
    one = estimate_tools_tokens(TOOLS[:1])
    both = estimate_tools_tokens(TOOLS)
    assert 0 < one < both


def test_count_request_tokens_includes_tools_via_fallback():
    """A model that only implements count_tokens still gets a tools-aware figure."""
    model = ScriptModel(responses=[])
    messages = _messages(2)
    without = count_request_tokens(model, messages, None)
    with_tools = count_request_tokens(model, messages, TOOLS)
    assert with_tools > without
    assert with_tools - without == estimate_tools_tokens(TOOLS)


def test_count_request_tokens_prefers_native_counter():
    class Native(ScriptModel):
        def count_request_tokens(self, messages, tools=None):
            return 4242

    assert count_request_tokens(Native(responses=[]), _messages(1), TOOLS) == 4242


def test_estimate_counts_reasoning_and_images():
    """Anything the request path serializes has to be on the gauge."""
    cm = ContextManager(model=ScriptModel(responses=[]), max_context_tokens=100_000)
    cm.seed([Message(role=Role.SYSTEM, content="sys")])
    cm.note_usage({"prompt_tokens": 1000})
    baseline = cm._used_tokens()

    plain = Message(role=Role.ASSISTANT, content="short")
    cm.append(plain)
    after_plain = cm._used_tokens()

    reasoning = Message(role=Role.ASSISTANT, content="short")
    reasoning.metadata["reasoning_content"] = "y" * 4000
    cm.append(reasoning)
    after_reasoning = cm._used_tokens()

    imaged = Message(role=Role.USER, content="look", images=["data:image/png;base64,AAAA"])
    cm.append(imaged)
    after_image = cm._used_tokens()

    assert after_plain > baseline
    # Echoed reasoning is in the prompt; a counter blind to it undercounts badly.
    assert after_reasoning - after_plain > 900
    assert after_image - after_reasoning > 1000


def test_tool_arguments_counted_as_json_not_repr():
    """str(dict) uses single quotes and differs in length from what is sent."""
    cm = ContextManager(model=ScriptModel(responses=[]), max_context_tokens=100_000)
    cm.seed([Message(role=Role.SYSTEM, content="sys")])
    cm.note_usage({"prompt_tokens": 100})
    before = cm._used_tokens()
    args = {"content": "z" * 8000, "path": "a.py"}
    cm.append(
        Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall("c", "write_file", args)])
    )
    grew = cm._used_tokens() - before
    assert grew >= len(json.dumps(args)) // 4


# --- 2. anchored gauge: one full count per compaction ------------------------


def test_provider_usage_avoids_local_counting_entirely():
    model = CountingModel()
    cm = ContextManager(model=model, max_context_tokens=100_000)
    cm.seed(_messages(3))
    cm.note_usage({"prompt_tokens": 5000})
    for _ in range(10):
        cm.usage_fraction()
        cm.output_budget()
    assert model.count_calls == 0


def test_cold_gauge_counts_once_then_reuses_the_anchor():
    model = CountingModel()
    cm = ContextManager(model=model, max_context_tokens=100_000)
    cm.seed(_messages(3))
    for _ in range(10):
        cm.usage_fraction()
    assert model.count_calls == 1


async def test_compaction_reanchors_exactly_once():
    model = CountingModel()
    cm = ContextManager(
        model=model,
        max_context_tokens=2000,
        proactive_threshold=100,
        enable_three_step_summary=False,
        keep_recent_turns=2,
        condenser=RecentWindowCondenser(trigger_fraction=0.0),
    )
    cm.seed(_messages(6))
    cm.usage_fraction()
    assert model.count_calls == 1
    assert await cm.maybe_summarize()
    for _ in range(5):
        cm.usage_fraction()
    assert model.count_calls == 2


# --- 2b. the local count calibrates itself against the provider ---------------


class Undercounter(ScriptModel):
    """A tokenizer that reads low, like litellm's does for a non-OpenAI model.

    Measured on a real run: turn 1 counted 2,730 against a reported 3,575.
    """

    def __init__(self, factor: float = 0.76):
        super().__init__(responses=[])
        self._factor = factor

    def count_tokens(self, messages):
        return int(sum(len(m.content or "") for m in messages) / 4 * self._factor)


def _calibrating_cm(factor: float = 0.76) -> ContextManager:
    cm = ContextManager(model=Undercounter(factor), max_context_tokens=100_000)
    cm.seed(
        [
            Message(role=Role.SYSTEM, content="s" * 4000),
            Message(role=Role.USER, content="u" * 4000),
        ]
    )
    return cm


def test_a_bad_model_tokenizer_cannot_drag_the_gauge_down():
    """litellm has no real tokenizer for many models. On minimax-m2.5 it read a
    3,623-token prompt as 2,530 — 30% low, mostly in the tool schemas — and low is
    the direction that overflows. The gauge takes the higher of the two reads."""
    cm = _calibrating_cm()  # model counts ~24% low
    independent = local_request_estimate(cm.get_messages(), None)
    assert cm._model.count_tokens(cm.get_messages()) < independent
    assert cm._used_tokens() == independent


def test_local_recount_is_corrected_by_the_provider():
    """The re-count after a compaction is where an undercount overflows: the window
    is tightest exactly then. Learned only when *both* local reads came up short."""
    cm = _calibrating_cm()
    estimate = cm._used_tokens()
    cm.note_usage({"prompt_tokens": estimate + 600})
    assert cm._count_overhead == 600
    cm._anchor_tokens = None  # what a compaction does
    assert cm._used_tokens() == estimate + 600


def test_correction_does_not_touch_incremental_estimates():
    """The regression this replaced: a 1.31x factor fitted to the anchor was applied
    to per-message deltas that were already accurate to ~0.5%, and pushed a real
    run's median error from 0.5% to 4.4% with every turn now reading high."""
    cm = _calibrating_cm()
    cm.note_usage({"prompt_tokens": cm._used_tokens() + 600})
    assert cm._count_overhead > 0
    before = cm._used_tokens()
    cm.append(Message(role=Role.USER, content="x" * 4000))
    # ~1000 tokens plus a little framing — not scaled, and not carrying the
    # per-request overhead a second time.
    assert cm._used_tokens() - before == pytest.approx(1000, abs=60)


def test_correction_never_adjusts_a_provider_anchor():
    cm = _calibrating_cm()
    cm._used_tokens()
    cm.note_usage({"prompt_tokens": 2000})
    cm.note_usage({"prompt_tokens": 5000})
    assert cm._used_tokens() == 5000


def test_correction_is_floored_at_zero():
    """Overcounting compacts early; undercounting overflows. Not symmetric."""
    cm = _calibrating_cm(factor=4.0)  # tokenizer reads high
    cm._used_tokens()
    cm.note_usage({"prompt_tokens": 10})
    assert cm._count_overhead == 0


def test_correction_keeps_the_worst_shortfall_seen():
    """Samples are rare — one per compaction — so there is no averaging to be had,
    and the same asymmetry that floors this at 0 argues for the worst case."""
    cm = _calibrating_cm()
    cm.note_usage({"prompt_tokens": cm._used_tokens() + 900})
    assert cm._count_overhead == 900
    cm._anchor_tokens = None
    raw = cm._used_tokens() - cm._count_overhead
    cm._pending_tokens = 0
    cm._anchor_from_provider = False
    cm._last_raw_anchor = raw
    cm.note_usage({"prompt_tokens": raw + 100})  # a smaller shortfall
    assert cm._count_overhead == 900


def test_correction_is_capped():
    cm = _calibrating_cm(factor=0.01)
    cm._used_tokens()
    cm.note_usage({"prompt_tokens": 10_000_000})
    assert cm._count_overhead <= 20_000


def test_correction_only_learns_from_an_untouched_local_anchor():
    """Once messages have been appended, the provider figure and the raw local count
    describe different prompts, and the difference is no longer the overhead."""
    cm = _calibrating_cm()
    cm._used_tokens()
    cm.append(Message(role=Role.USER, content="z" * 40_000))
    cm.note_usage({"prompt_tokens": 2000})
    assert cm._count_overhead == 0


# --- 3. capacity nets out reserved output ------------------------------------


def test_capacity_reserves_room_for_the_response():
    plain = ContextManager(model=ScriptModel(responses=[]), max_context_tokens=100_000)
    reserved = ContextManager(
        model=ScriptModel(responses=[]),
        max_context_tokens=100_000,
        reserved_output_tokens=16_000,
        safety_margin_tokens=2_000,
    )
    assert plain.capacity() == 100_000
    assert reserved.capacity() == 82_000
    for cm in (plain, reserved):
        cm.seed(_messages(2))
        cm.note_usage({"prompt_tokens": 41_000})
    # Same history, same window — but the reserved run knows it is half full.
    assert plain.usage_fraction() == pytest.approx(0.41, abs=0.01)
    assert reserved.usage_fraction() == pytest.approx(0.50, abs=0.01)


def test_capacity_never_goes_below_one():
    cm = ContextManager(
        model=ScriptModel(responses=[]),
        max_context_tokens=1000,
        reserved_output_tokens=99_000,
        safety_margin_tokens=99_000,
    )
    assert cm.capacity() == 1
    cm.seed([Message(role=Role.SYSTEM, content="sys")])
    cm.usage_fraction()  # must not raise ZeroDivisionError


def test_set_tools_raises_the_measured_request():
    cm = ContextManager(model=ScriptModel(responses=[]), max_context_tokens=100_000)
    cm.seed(_messages(2))
    without = cm._used_tokens()
    cm.set_tools(TOOLS)
    assert cm._used_tokens() > without
    assert cm.tool_schema_tokens() > 0


def test_set_tools_does_not_discard_a_provider_anchor():
    cm = ContextManager(model=ScriptModel(responses=[]), max_context_tokens=100_000)
    cm.seed(_messages(2))
    cm.note_usage({"prompt_tokens": 7777})
    cm.set_tools(TOOLS)
    # The provider count already included the schemas; re-anchoring locally here
    # would replace a true number with an estimate.
    assert cm._used_tokens() == 7777


def test_budget_snapshot_reports_the_breakdown():
    cm = ContextManager(
        model=ScriptModel(responses=[]),
        max_context_tokens=100_000,
        reserved_output_tokens=16_000,
        tools_schema=TOOLS,
    )
    cm.seed(_messages(2))
    cm.note_usage({"prompt_tokens": 20_000})
    snap = cm.budget_snapshot()
    assert snap["used_tokens"] == 20_000
    assert snap["capacity_tokens"] == 84_000
    assert snap["reserved_output_tokens"] == 16_000
    assert snap["tool_schema_tokens"] > 0
    assert snap["provider_anchored"] is True
    assert 0 < snap["fraction"] < 1


# --- 4. adaptive output shaping ----------------------------------------------


def _budget_at(used: int, is_error: bool = False, **kw) -> int:
    cm = ContextManager(model=ScriptModel(responses=[]), max_context_tokens=100_000, **kw)
    cm.seed([Message(role=Role.SYSTEM, content="sys")])
    cm.note_usage({"prompt_tokens": used})
    return cm.output_budget(is_error)


def test_output_budget_shrinks_as_the_window_fills():
    roomy = _budget_at(10_000)
    tight = _budget_at(90_000)
    tighter = _budget_at(98_000)
    assert roomy > tight > tighter
    # Plenty of room means no change from the old fixed behaviour.
    assert roomy == 30_720


def test_output_budget_respects_floor_and_ceiling():
    assert _budget_at(99_999) >= 2_048
    assert _budget_at(0) <= 30_720


def test_errors_get_more_room_than_ordinary_output():
    used = 99_000
    assert _budget_at(used, is_error=True) > _budget_at(used, is_error=False)
    assert _budget_at(used, is_error=True) >= ERROR_OUTPUT_FLOOR_BYTES


def test_adaptive_output_can_be_switched_off():
    assert _budget_at(99_000, adaptive_output=False) == 30_720


def test_output_budget_is_the_ceiling_before_the_gauge_is_anchored():
    cm = ContextManager(model=ScriptModel(responses=[]), max_context_tokens=100_000)
    cm.seed(_messages(2))
    assert not cm.has_token_anchor()
    # Must not trigger a full tokenizer pass per tool result just to size one.
    assert cm.output_budget() == 30_720


def test_error_truncation_keeps_the_tail():
    """A traceback says what went wrong at the end, not the beginning."""
    text = "HEAD" * 500 + "TAIL_MARKER"
    shaped = shape_observation(text, max_bytes=400, is_error=True)
    head, tail = shaped.split("...[truncated", 1)
    assert "TAIL_MARKER" in shaped
    assert len(tail) > len(head)


def test_non_error_truncation_stays_balanced():
    text = "a" * 2000
    shaped = shape_observation(text, max_bytes=400)
    head, _, tail = shaped.partition("...[truncated 1600 bytes]...")
    assert len(head.strip()) == pytest.approx(len(tail.strip()), abs=2)


# --- 5. working state card ---------------------------------------------------


def _card() -> WorkingState:
    state = WorkingState(task="Build the thing")
    state.goal = "Make tests pass"
    state.todos = [{"content": "write parser", "status": "completed"}]
    state.files_modified = ["src/a.py", "src/b.py"]
    state.acceptance = "  ☐ [c1] writes out.txt"
    state.note_check("pytest -q", 1, turn=3)
    state.note_failure("edit", "old_string not found")
    state.narrative = "Parser needs a lookahead."
    return state


def test_card_renders_every_source():
    rendered = _card().render()
    for fragment in (
        "Build the thing",
        "Make tests pass",
        "write parser",
        "src/a.py",
        "pytest -q",
        "exit 1",
        "old_string not found",
        "writes out.txt",
        "lookahead",
    ):
        assert fragment in rendered


def test_card_omits_empty_sections():
    rendered = WorkingState(task="only a task").render()
    assert "## Task" in rendered
    assert "## Checks run" not in rendered
    assert "## Files modified" not in rendered


def test_card_is_bounded_under_load():
    state = WorkingState(task="t")
    state.files_modified = [f"src/file_{i}.py" for i in range(200)]
    for i in range(100):
        state.note_check(f"pytest tests/test_{i}.py", 0, turn=i)
    for i in range(50):
        state.note_failure("bash", f"failure number {i} " + "z" * 5000)
    state.narrative = "n" * 50_000
    rendered = state.render()
    assert len(state.checks) == 20
    assert len(state.failures) == 5
    assert len(rendered) < 12_000
    assert "and 160 more" in rendered


def test_note_check_keeps_the_latest_verdict_per_command():
    state = WorkingState()
    state.note_check("pytest -q", 1, turn=2)
    state.note_check("pytest -q", 0, turn=9)
    assert [(c.command, c.exit_code, c.turn) for c in state.checks] == [("pytest -q", 0, 9)]


def test_card_round_trips_through_a_checkpoint():
    original = _card()
    restored = WorkingState.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.render() == original.render()


def test_card_from_partial_document_does_not_raise():
    restored = WorkingState.from_dict({"task": "t", "checks": [{"command": "x"}, "junk"]})
    assert restored.task == "t"
    assert restored.checks == [CheckRecord(command="x", exit_code=0, turn=0)]


def test_is_empty_only_when_nothing_but_the_task():
    assert WorkingState(task="t").is_empty()
    assert not _card().is_empty()


# --- 6. subagent handoffs -----------------------------------------------------


def _parent(with_state: bool = True) -> ContextManager:
    cm = ContextManager(
        model=ScriptModel(responses=[]),
        max_context_tokens=100_000,
        condenser=MicrocompactCondenser(),
    )
    cm.seed(_messages(8))
    cm.append(
        Message(
            role=Role.TOOL,
            content="[buffer:buf_abc123 | 90000 bytes | 12 lines tool=bash]\npreview",
            name="bash",
            tool_call_id="cX",
        )
    )
    if with_state:
        cm.set_state_provider(_card().render)
    return cm


def test_brief_handoff_is_far_smaller_than_full():
    parent = _parent()
    brief = parent.fork(mode=FORK_BRIEF)
    full = parent.fork(mode=FORK_FULL)
    assert len(brief.get_messages()) < len(full.get_messages())
    assert len(full.get_messages()) == len(parent.get_messages())
    assert len(brief.get_messages()) == 2  # system + the handoff card


def test_brief_handoff_carries_the_card_and_buffer_ids():
    handoff = _parent().fork(mode=FORK_BRIEF).get_messages()[-1].content
    assert "Make tests pass" in handoff
    assert "src/a.py" in handoff
    assert "buffer:buf_abc123" in handoff


def test_brief_handoff_without_a_card_still_passes_buffer_pointers():
    """No card is not nothing: the parent's artifacts are still worth pointing at."""
    brief = _parent(with_state=False).fork(mode=FORK_BRIEF)
    messages = brief.get_messages()
    assert [m.role for m in messages] == [Role.SYSTEM, Role.USER]
    assert "buffer:buf_abc123" in messages[1].content
    assert "Make tests pass" not in messages[1].content


def test_brief_handoff_with_nothing_to_say_is_just_the_system_prompt():
    cm = ContextManager(model=ScriptModel(responses=[]), max_context_tokens=100_000)
    cm.seed([Message(role=Role.SYSTEM, content="sys"), Message(role=Role.USER, content="t")])
    assert [m.role for m in cm.fork(mode=FORK_BRIEF).get_messages()] == [Role.SYSTEM]


def test_none_handoff_starts_cold():
    assert _parent().fork(mode=FORK_NONE).get_messages() == []


def test_fork_does_not_share_the_condensers_running_state():
    """A shared condenser lets a fork's compaction overwrite the parent's state."""
    parent = _parent()
    forked = parent.fork(mode=FORK_FULL)
    assert forked._condenser is not parent._condenser
    assert type(forked._condenser) is type(parent._condenser)
    forked._condenser._state = "the fork's notes"
    assert parent._condenser._state == ""


def test_fork_preserves_condenser_tuning():
    """Copied, not reconstructed — `type(c)()` would silently reset these."""
    cm = ContextManager(
        model=ScriptModel(responses=[]),
        condenser=MicrocompactCondenser(microcompact_fraction=0.55, prune_min_chars=99),
    )
    forked = cm.fork(mode=FORK_NONE)._condenser
    assert forked.microcompact_fraction == 0.55
    assert forked.prune_min_chars == 99


def test_a_brief_fork_does_not_inherit_notes_about_history_it_lacks():
    """The other half of deep-copying: tuning should survive, the parent's running
    notes should not. A subagent handed a 2 KB card would otherwise start with
    notes on a transcript it never saw, and fold its own work into them — and a
    high _last_summary_len would suppress its first summarize until its message
    count passed a number it had no part in reaching."""
    parent = _parent()
    parent._condenser._state = "## Key findings\nthe parent's notes"
    parent._condenser._last_summary_len = 240

    brief = parent.fork(mode=FORK_BRIEF)._condenser
    assert brief._state == ""
    assert brief._last_summary_len == 0
    assert brief.microcompact_fraction == parent._condenser.microcompact_fraction

    # A full fork *does* have that history, so the notes still describe it.
    full = parent.fork(mode=FORK_FULL)._condenser
    assert full._state == "## Key findings\nthe parent's notes"
    assert full._last_summary_len == 240
    # And it is still a copy — writing to it must not reach the parent.
    full._state = "changed"
    assert parent._condenser._state == "## Key findings\nthe parent's notes"


def test_fork_handles_a_condenser_with_no_reset():
    """reset() is optional on the protocol; a strategy holding no per-conversation
    state need not implement it."""

    class Stateless:
        async def condense(self, cx):
            return None

    cm = ContextManager(model=ScriptModel(responses=[]), condenser=Stateless())
    assert cm.fork(mode=FORK_BRIEF)._condenser is not cm._condenser


def test_fork_survives_a_condenser_with_a_required_argument():
    """The Condenser Protocol promises no no-arg constructor. Reconstructing one
    raised TypeError here, which took every subagent invocation with it."""

    class Tuned:
        def __init__(self, threshold):  # no default, on purpose
            self.threshold = threshold

        async def condense(self, cx):
            return None

    cm = ContextManager(model=ScriptModel(responses=[]), condenser=Tuned(0.9))
    assert cm.fork(mode=FORK_NONE)._condenser.threshold == 0.9


def test_fork_keeps_the_boolean_interface_working():
    parent = _parent()
    assert len(parent.fork(include_history=True).get_messages()) == len(parent.get_messages())
    assert parent.fork(include_history=False).get_messages() == []


def test_fork_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        _parent().fork(mode="everything")


def test_normalize_handoff_maps_the_legacy_boolean():
    assert normalize_handoff(True) == FORK_FULL
    assert normalize_handoff(False) == FORK_NONE
    assert normalize_handoff("brief") == FORK_BRIEF
    assert normalize_handoff(None) == FORK_NONE
    # A model can put anything in a tool argument; that should cost context, not the run.
    assert normalize_handoff("everything") == FORK_NONE


def test_replace_system_message_swaps_the_persona():
    parent = _parent()
    forked = parent.fork(mode=FORK_FULL)
    forked.replace_system_message("You are the explore subagent.")
    messages = forked.get_messages()
    assert messages[0].content == "You are the explore subagent."
    assert len([m for m in messages if m.role == Role.SYSTEM]) == 1
    assert parent.get_messages()[0].content == "sys"


def test_set_task_overrides_an_inherited_task():
    forked = _parent().fork(mode=FORK_BRIEF)
    forked.set_task("the subagent's own assignment")
    assert forked._task == "the subagent's own assignment"
