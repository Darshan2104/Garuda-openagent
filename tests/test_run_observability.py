"""Run observability: config, turn attribution, and gate records reach the log.

Five things the harness computed and then threw away, so a trajectory could not
answer questions the harness itself knew the answer to:

1. The resolved gate stack was never emitted, which made every gate reading
   ambiguous — no `contract` event could mean the gate was off, or on with
   derivation failing, or on and never reaching a `task_complete`. Those call for
   three different fixes and were indistinguishable from absence.
2. `turn` was missing from `model_response`/`tool_call`/`tool_result`/
   `verification`, so every consumer re-derived turn boundaries positionally.
3. `permission_ask` carried no tool name or id, so a denial was unattributable.
4. `metrics`, `mode` and `acceptance` were dropped at persistence, so a finished
   run read off disk had no latency figures and no record of its posture.
5. A `summarization` event could not distinguish a cheap in-place prune from a
   full summarize-and-rebuild — the two most different things compaction does.

Every addition is additive: a reader falling back on an older log gets the
previous behaviour, never a wrong answer.
"""

from dataclasses import fields
from pathlib import Path

from garuda.context.condenser import (
    CondenseOutcome,
    MicrocompactCondenser,
    RecentWindowCondenser,
    condense_outcome,
)
from garuda.context.manager import ContextManager
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.modes import (
    GATE_FIELDS,
    MODE_PRESETS,
    RUN_CONFIG_FIELDS,
    apply_mode_preset,
    describe_config,
)
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import RigorousAgent
from garuda.core.sessions import SessionStore
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools, tools_for_names
from garuda.types import AgentConfig, AgentResult, Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment

# Event types that must carry turn attribution once emitted from inside a turn.
TURN_SCOPED = ("model_response", "tool_call", "tool_result", "verification")


def _payloads(events: EventStore, event_type: str) -> list[dict]:
    return [e["payload"] for e in events.get_all() if e["type"] == event_type]


def _first(events: EventStore, event_type: str) -> dict:
    found = _payloads(events, event_type)
    assert found, f"no {event_type} event was emitted"
    return found[0]


def _complete(call_id: str = "c1", summary: str = "Implemented the change and verified it.") -> ModelResponse:
    return ModelResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name="task_complete", arguments={"summary": summary})],
    )


async def _run(tmp_path: Path, model: ScriptModel, config: AgentConfig, **kwargs) -> EventStore:
    """Drive one run and hand back its event store."""
    events = kwargs.pop("events", None) or EventStore()
    await DefaultAgent(profile_name=kwargs.pop("profile_name", "build")).run(
        task="do the thing",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=kwargs.pop("tools", None) or default_tools(),
        config=config,
        events=events,
        permissions=PermissionEngine(mode=kwargs.pop("permission_mode", "yolo")),
        **kwargs,
    )
    return events


def _cheap(**overrides) -> AgentConfig:
    """A config that terminates fast and pays for no gate that needs a model call."""
    base = {
        "max_turns": 4,
        "permission_mode": "yolo",
        "enable_llm_verifier": False,
        "enable_acceptance_contract": False,
        "bootstrap_environment": False,
        "sandbox_require": False,
    }
    base.update(overrides)
    return AgentConfig(**base)


# --- 1. the resolved posture -------------------------------------------------


def test_run_config_fields_are_all_real_config_fields():
    """A typo here would silently drop a key from every trajectory ever written.

    `describe_config` reads by name through `getattr(..., None)`, so a misspelled
    field yields None instead of raising — the failure mode this test exists to
    make loud.
    """
    known = {f.name for f in fields(AgentConfig)}
    unknown = [name for name in RUN_CONFIG_FIELDS if name not in known]
    assert not unknown, f"RUN_CONFIG_FIELDS names no such AgentConfig field: {unknown}"


def test_run_config_fields_cover_every_gate():
    """The gate stack is the whole point; a gate outside the payload is invisible."""
    assert set(GATE_FIELDS) <= set(RUN_CONFIG_FIELDS)


def test_describe_config_reports_the_eval_preset():
    described = describe_config(apply_mode_preset(AgentConfig(), "eval"))
    # Asserted against the preset table rather than literals, so the payload and
    # the posture definition cannot drift apart.
    for field, expected in MODE_PRESETS["eval"].items():
        assert described[field] == expected, field


def test_describe_config_distinguishes_the_postures():
    """Two postures must not describe identically, or the field buys nothing."""
    assert describe_config(apply_mode_preset(AgentConfig(), "eval")) != describe_config(
        apply_mode_preset(AgentConfig(), "interactive")
    )


def test_describe_config_reports_a_declared_override_not_the_mode_implication():
    """It reports what the run did, not what its mode would suggest.

    A profile declaring `enable_acceptance_contract: true` under `interactive`
    keeps it (that is `apply_mode_preset`'s declared-fields rule), and a reader
    inferring the gate from the mode name alone would get this backwards.
    """
    config = apply_mode_preset(
        AgentConfig(enable_acceptance_contract=True),
        "interactive",
        declared_fields={"enable_acceptance_contract"},
    )
    assert config.mode == "interactive"
    assert describe_config(config)["enable_acceptance_contract"] is True
    assert MODE_PRESETS["interactive"]["enable_acceptance_contract"] is False


async def test_session_start_carries_the_resolved_gate_stack(tmp_path: Path):
    config = apply_mode_preset(_cheap(force_final_submission=False, max_turns=1), "eval")
    events = await _run(tmp_path, ScriptModel([ModelResponse(content="thinking", tool_calls=[])]), config)

    start = _first(events, "session_start")
    assert start["task"] == "do the thing"
    assert start["model"] == "script/test"
    assert start["agent"] == "build"
    assert start["mode"] == "eval"
    for field, expected in MODE_PRESETS["eval"].items():
        assert start["config"][field] == expected, field


async def test_session_start_reports_the_engine_permission_mode(tmp_path: Path):
    """The engine's mode, not the config's — the engine is what gated the tools.

    They diverge in practice: the harbor adapter rebuilds a PermissionEngine from
    its own kwarg while the config keeps the profile's value. Reporting only one
    would hide which of the two actually applied.
    """
    config = _cheap(permission_mode="smart", force_final_submission=False, max_turns=1)
    events = await _run(
        tmp_path,
        ScriptModel([ModelResponse(content="thinking", tool_calls=[])]),
        config,
        permission_mode="readonly",
    )
    start = _first(events, "session_start")
    assert start["permission_mode"] == "readonly"
    assert start["config"]["permission_mode"] == "smart"


async def test_nested_runs_emit_no_second_session_start(tmp_path: Path):
    """`emit_session_events=False` is what keeps one log to one session_start."""
    events = EventStore()
    await DefaultAgent().run(
        task="inner",
        model=ScriptModel([_complete()]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_cheap(),
        events=events,
        permissions=PermissionEngine(mode="yolo"),
        emit_session_events=False,
    )
    assert _payloads(events, "session_start") == []


async def test_rigorous_session_start_matches_the_default_shape(tmp_path: Path):
    """One session_start contract, not two — the reader must not special-case mode."""
    events = EventStore()
    await RigorousAgent(profile_name="build").run(
        task="fix it",
        model=ScriptModel(
            [
                ModelResponse(content="1. write the file", tool_calls=[]),
                _complete("a1"),
                ModelResponse(content="APPROVED", tool_calls=[]),
            ]
        ),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools_for_names(["write_file", "task_complete"]),
        config=apply_mode_preset(_cheap(max_turns=6), "rigorous"),
        events=events,
        permissions=PermissionEngine(mode="yolo"),
    )
    start = _first(events, "session_start")
    assert start["mode"] == "rigorous"
    assert start["agent"] == "build"
    assert start["permission_mode"] == "yolo"
    assert set(GATE_FIELDS) <= set(start["config"])


# --- 2. turn attribution -----------------------------------------------------


def _turn_bands(events: EventStore) -> list[tuple[int, list[dict]]]:
    """Group turn-scoped events under the turn their `budget` preflight opened.

    Deliberately built the way a trajectory reader will build it, so this asserts
    the invariant a consumer depends on: the first turn-bearing event of turn N
    precedes turn N's model call.
    """
    bands: list[tuple[int, list[dict]]] = []
    for event in events.get_all():
        payload = event["payload"]
        if event["type"] == "budget" and payload.get("stage") == "context":
            bands.append((payload["turn"], []))
        elif event["type"] in TURN_SCOPED and bands:
            bands[-1][1].append(payload)
    return bands


async def test_every_turn_scoped_event_carries_its_turn(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha")
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"})],
            ),
            _complete(),
        ]
    )
    events = await _run(tmp_path, model, _cheap())

    bands = _turn_bands(events)
    assert [turn for turn, _ in bands] == [1, 2]
    for turn, payloads in bands:
        assert payloads, f"turn {turn} recorded no turn-scoped event"
        for payload in payloads:
            # The truncation marker is a different shape that already carried turn.
            assert payload.get("turn") == turn, payload

    # And each of the four types actually appeared, so the loop above is not vacuous.
    seen = {e["type"] for e in events.get_all()}
    assert set(TURN_SCOPED) <= seen


async def test_parallel_read_batch_carries_turn_on_both_sides(tmp_path: Path):
    """The concurrent path emits its own TOOL_CALL block and must not be forgotten."""
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="a", name="read_file", arguments={"path": "a.txt"}),
                    ToolCall(id="b", name="read_file", arguments={"path": "b.txt"}),
                ],
            ),
            _complete(),
        ]
    )
    events = await _run(tmp_path, model, _cheap())

    calls = _payloads(events, "tool_call")
    results = _payloads(events, "tool_result")
    reads = [c for c in calls if c["name"] == "read_file"]
    assert len(reads) == 2, "expected the batch to dispatch both reads"
    assert all(c["turn"] == 1 for c in reads)
    assert all(r["turn"] == 1 for r in results if r["name"] == "read_file")


async def test_completion_verdict_carries_the_turn_it_was_judged_on(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha")
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"})],
            ),
            _complete(),
        ]
    )
    events = await _run(tmp_path, model, _cheap())
    verdict = _first(events, "verification")
    assert verdict["turn"] == 2
    assert verdict["approved"] is True


async def test_the_rigorous_critic_verdict_carries_no_turn(tmp_path: Path):
    """It is emitted between turns, so a turn number there would be a lie.

    This is the boundary of the turn-attribution rule: only an event emitted inside
    the dynamic extent of a turn may claim one. A value here would make a
    trajectory reader open a spurious turn band.
    """
    events = EventStore()
    await RigorousAgent(profile_name="build").run(
        task="fix it",
        model=ScriptModel(
            [
                ModelResponse(content="1. write the file", tool_calls=[]),
                _complete("a1"),
                ModelResponse(content="APPROVED", tool_calls=[]),
            ]
        ),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools_for_names(["write_file", "task_complete"]),
        config=apply_mode_preset(_cheap(max_turns=6), "rigorous"),
        events=events,
        permissions=PermissionEngine(mode="yolo"),
    )
    critics = [p for p in _payloads(events, "verification") if p.get("phase") == "critic"]
    assert critics, "the critic verdict should have been emitted"
    for critic in critics:
        assert "turn" not in critic, critic


# --- 3. attributable permission denials --------------------------------------


async def test_permission_denial_names_the_call_it_blocked(tmp_path: Path):
    """Without name/id a denial could not be tied to the call it stopped.

    `readonly` denies write_file, so the run's own transcript records the refusal;
    the event has to carry enough to place it against a tool step.
    """
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="w9", name="write_file", arguments={"path": "x.txt", "content": "no"})
                ],
            ),
            _complete(),
        ]
    )
    events = await _run(tmp_path, model, _cheap(permission_mode="readonly"), permission_mode="readonly")

    denial = _first(events, "permission_ask")
    assert denial["approved"] is False
    assert denial["name"] == "write_file"
    assert denial["id"] == "w9"
    assert denial["turn"] == 1
    assert denial["reason"]
    # A denied call is screened before dispatch, so it never becomes a tool_call.
    assert not [c for c in _payloads(events, "tool_call") if c["name"] == "write_file"]


# --- 4. what survives to disk ------------------------------------------------


async def test_finished_session_meta_carries_metrics_and_mode(tmp_path: Path):
    store = SessionStore(root=tmp_path / "sessions")
    store.begin(
        session_id="s1", task="t", model="script/test", agent="build", workspace=str(tmp_path)
    )
    events = await _run(tmp_path, ScriptModel([_complete()]), _cheap())
    result = AgentResult(
        success=True, final_message="done", messages=[], turns=1, metadata={"usage": {}, "mode": "eval",
        "metrics": {"turns": 1, "model_ms_total": 12.5}}
    )
    store.finish("s1", result)

    meta = store.load_meta("s1")
    assert meta["status"] == "success"
    assert meta["mode"] == "eval"
    assert meta["metrics"]["turns"] == 1
    assert meta["metrics"]["model_ms_total"] == 12.5
    assert _payloads(events, "session_end")


def test_absent_metadata_keys_are_not_written_as_null(tmp_path: Path):
    """`bare_result` carries no acceptance; absent beats a null a reader must special-case."""
    store = SessionStore(root=tmp_path / "sessions")
    store.begin(session_id="s2", task="t", model="m", agent="build", workspace=str(tmp_path))
    store.finish(
        "s2",
        AgentResult(success=False, final_message="died", messages=[], turns=0, metadata={"usage": {}}),
    )
    meta = store.load_meta("s2")
    assert "acceptance" not in meta
    assert "metrics" not in meta
    assert "mode" not in meta
    assert meta["status"] == "failed"


def test_acceptance_is_persisted_when_a_contract_ran(tmp_path: Path):
    store = SessionStore(root=tmp_path / "sessions")
    store.begin(session_id="s3", task="t", model="m", agent="build", workspace=str(tmp_path))
    acceptance = {"total": 2, "verified": 1, "criteria": [{"id": "c1", "status": "verified"}]}
    store.finish(
        "s3",
        AgentResult(
            success=True, final_message="ok", messages=[], turns=3,
            metadata={"usage": {}, "acceptance": acceptance},
        ),
    )
    assert store.load_meta("s3")["acceptance"] == acceptance


async def test_run_metadata_reports_the_mode_it_ran_under(tmp_path: Path):
    """`mode` has to come off the result, because `finish` only sees an AgentResult."""
    config = apply_mode_preset(_cheap(force_final_submission=False, max_turns=1), "eval")
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel([ModelResponse(content="thinking", tool_calls=[])]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=config,
        permissions=PermissionEngine(mode="yolo"),
    )
    assert result.metadata["mode"] == "eval"


# --- 5. what compaction actually did ----------------------------------------


def _loaded_context(used_tokens: int = 800, **overrides) -> ContextManager:
    """A context sitting on prunable bulk, over the microcompact threshold.

    ``used_tokens`` is the provider-anchored count against a 1000-token window, so
    the default 800 is 80% — past microcompact's 0.75 gate. `recent_window` triggers
    at 0.85 and needs a higher figure.
    """
    settings = {
        "max_context_tokens": 1000,
        "proactive_threshold": 300,
        "enable_three_step_summary": False,
        "keep_recent_turns": 2,
    }
    settings.update(overrides)
    ctx = ContextManager(model=ScriptModel(responses=[]), **settings)
    ctx.seed([Message(role=Role.SYSTEM, content="sys"), Message(role=Role.USER, content="task")])
    for i in range(6):
        ctx.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="bash", arguments={"command": "ls"})],
            )
        )
        ctx.append(Message(role=Role.TOOL, content="x" * 2000, name="bash", tool_call_id=f"c{i}"))
    ctx.note_usage({"prompt_tokens": used_tokens})
    return ctx


async def test_a_prune_is_recorded_as_a_prune_with_its_count():
    """The count was computed by an `> 0` test and discarded; it is the only
    measure of how much a prune recovered."""
    ctx = _loaded_context()
    assert await ctx.maybe_summarize()

    record = ctx.last_compaction
    assert record["strategy"] == "microcompact"
    assert record["action"] == "prune"
    assert record["pruned"] >= 1
    # A prune rewrites contents in place, so the structure is untouched.
    assert record["messages_before"] == record["messages_after"]
    assert record["tokens_before"] == 800
    assert record["tokens_after"] is not None


async def test_a_summarize_is_recorded_as_a_summarize():
    """The distinction a reader needs: this one costs model calls and drops history."""
    ctx = _loaded_context()
    assert await ctx.maybe_summarize()  # prunes
    ctx.note_usage({"prompt_tokens": 800})  # still over the threshold, nothing left to prune
    assert await ctx.maybe_summarize()

    record = ctx.last_compaction
    assert record["action"] == "summarize"
    assert record["pruned"] == 0
    assert record["messages_after"] < record["messages_before"]


async def test_recent_window_reports_a_window_and_no_prune_count():
    """`pruned` means tool outputs stubbed in place. A window drops messages
    instead, and reporting that as a prune count would conflate two mechanisms."""
    ctx = _loaded_context(used_tokens=900, condenser="recent_window")
    assert await ctx.maybe_summarize()

    record = ctx.last_compaction
    assert record["strategy"] == "recent_window"
    assert record["action"] == "window"
    assert record["pruned"] == 0
    assert record["messages_after"] < record["messages_before"]


async def test_force_compact_records_the_window_fallback():
    """force_compact may run the window pass *after* the condenser, so what shrank
    history is not necessarily what the condenser reported."""
    ctx = _loaded_context()
    assert await ctx.force_compact()
    assert ctx.last_compaction["action"] == "window"


async def test_a_condenser_that_reports_nothing_degrades_to_the_strategy_name():
    """The protocol member is optional, exactly as `reset` is. A third-party or
    test condenser implementing only `condense` must keep working."""

    class BareCondenser:
        async def condense(self, cx):
            return list(cx.messages[:3])

    assert condense_outcome(BareCondenser()) is None
    ctx = _loaded_context(condenser=BareCondenser())
    assert await ctx.maybe_summarize()

    record = ctx.last_compaction
    assert record["strategy"] == "BareCondenser"
    assert record["action"] is None
    assert record["messages_after"] == 3


def test_condense_outcome_rejects_a_non_outcome_attribute():
    """`getattr` would happily return anything named last_outcome."""

    class Liar:
        last_outcome = "prune"

    assert condense_outcome(Liar()) is None
    assert condense_outcome(MicrocompactCondenser()) is None  # nothing run yet
    reporting = RecentWindowCondenser()
    reporting.last_outcome = CondenseOutcome("recent_window", "window")
    assert condense_outcome(reporting).action == "window"


async def test_the_summarization_event_keeps_its_original_keys(tmp_path: Path):
    """Additive: `turn` and `duration_ms` are what older readers key on."""
    ctx = _loaded_context()
    assert await ctx.maybe_summarize()
    record = ctx.last_compaction
    payload = {"turn": 3, "duration_ms": 1.5, **record}
    assert payload["turn"] == 3
    assert payload["duration_ms"] == 1.5
    assert payload["action"] == "prune"
