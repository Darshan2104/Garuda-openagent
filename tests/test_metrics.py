"""Per-turn latency and token metrics.

Every latency claim about this loop used to be computed by hand from outside it
(see docs/BACKLOG.md). These tests pin the numbers the harness now reports itself.
"""

import asyncio
from pathlib import Path

from garuda.core.events import EventType
from garuda.core.loop import DefaultAgent
from garuda.core.metrics import RunMetrics, TurnMetrics, stopwatch
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment

# -- the metrics object in isolation ----------------------------------------------


def test_stopwatch_records_elapsed_even_when_the_block_raises():
    """A model call that fails after retries still spent that wall-clock, and that is
    exactly the case worth measuring."""
    try:
        with stopwatch() as elapsed:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert elapsed[0] >= 0.0


def test_parallel_saved_is_summed_durations_minus_wall_clock():
    record = TurnMetrics(turn=1, tool_ms_total=300.0, tool_wall_ms=110.0)
    assert record.parallel_saved_ms == 190.0


def test_parallel_saved_never_goes_negative():
    """Wall-clock can exceed the summed durations (scheduling overhead on a
    one-call segment); a negative "saving" would be nonsense."""
    record = TurnMetrics(turn=1, tool_ms_total=10.0, tool_wall_ms=12.0)
    assert record.parallel_saved_ms == 0.0


def test_cache_hit_rate_is_read_over_prompt():
    metrics = RunMetrics()
    metrics.open_turn(1)
    metrics.note_usage({"prompt_tokens": 1000, "completion_tokens": 50, "cache_read_tokens": 950})
    metrics.open_turn(2)
    metrics.note_usage({"prompt_tokens": 1000, "completion_tokens": 50, "cache_read_tokens": 850})
    summary = metrics.summary()
    assert summary["prompt_tokens"] == 2000
    assert summary["cache_read_tokens"] == 1800
    assert summary["cache_hit_rate"] == 0.9


def test_cache_hit_rate_is_none_when_no_usage_was_reported():
    """A provider that omits usage is a different situation from a genuine 0% hit
    rate, and reporting 0.0 for it would read as a caching regression."""
    metrics = RunMetrics()
    metrics.open_turn(1)
    assert metrics.summary()["cache_hit_rate"] is None


def test_summary_of_a_run_with_no_turns():
    assert RunMetrics().summary() == {"turns": 0}


def test_note_helpers_are_noops_before_a_turn_is_open():
    """ToolRunner writes through `current`; a directly-constructed runner with no
    turn open must not raise."""
    metrics = RunMetrics()
    metrics.note_tool(5.0, is_error=False)
    metrics.note_tool_wall(5.0)
    metrics.note_parallel_segment(3)
    metrics.note_usage({"prompt_tokens": 10})
    assert metrics.summary() == {"turns": 0}


def test_percentiles_come_from_real_turns():
    metrics = RunMetrics()
    for value in (10.0, 20.0, 30.0, 40.0, 500.0):
        metrics.open_turn(len(metrics.turns) + 1).model_ms = value
    summary = metrics.summary()
    assert summary["turns"] == 5
    assert summary["model_ms_total"] == 600.0
    assert summary["model_ms_p50"] == 30.0
    # Nearest-rank, so p95 is a duration some turn actually took.
    assert summary["model_ms_p95"] == 500.0


# -- end to end through the loop ---------------------------------------------------


async def test_run_reports_per_turn_metrics(tmp_path: Path):
    (tmp_path / "a.txt").write_text("AAA", encoding="utf-8")
    env = LocalEnvironment(workspace_root=tmp_path)
    usage = {"prompt_tokens": 1000, "completion_tokens": 20, "cache_read_tokens": 900}
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"})],
            usage=dict(usage),
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "read the file"})],
            usage=dict(usage),
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=5, enable_verifier=False),
    )
    assert result.success

    metrics = result.metadata["metrics"]
    assert metrics["turns"] == 2
    assert metrics["model_ms_total"] > 0.0
    assert metrics["tool_calls"] == 1  # task_complete is the gate, not a tool call
    assert metrics["tool_ms_total"] > 0.0
    assert metrics["prompt_tokens"] == 2000
    assert metrics["cache_hit_rate"] == 0.9

    # One turn_metrics event per turn, and model latency reaches the event log.
    turn_events = [e for e in result.metadata["events"] if e["type"] == EventType.TURN_METRICS.value]
    assert len(turn_events) == 2
    assert all(e["payload"]["model_ms"] > 0.0 for e in turn_events)

    responses_logged = [
        e for e in result.metadata["events"] if e["type"] == EventType.MODEL_RESPONSE.value
    ]
    # Gives observability/tracing.py the span extent its llm spans lacked.
    assert all("duration_ms" in e["payload"] for e in responses_logged)

    tool_results = [e for e in result.metadata["events"] if e["type"] == EventType.TOOL_RESULT.value]
    assert tool_results and all("duration_ms" in e["payload"] for e in tool_results)


async def test_concurrent_segment_reports_time_saved(tmp_path: Path):
    """Summed per-call durations exceed the segment's wall-clock when reads overlap."""
    env = LocalEnvironment(workspace_root=tmp_path)

    class SlowReadTool:
        name = "read_file"
        description = "slow read"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        async def execute(self, arguments, env, ctx):
            from garuda.types import ToolResult

            await asyncio.sleep(0.05)
            return ToolResult(tool_call_id="", content="read")

    tools = [SlowReadTool()] + [t for t in default_tools() if t.name != "read_file"]
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id=f"r{i}", name="read_file", arguments={"path": f"f{i}"}) for i in range(4)
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="d", name="task_complete", arguments={"summary": "read four files"})],
        ),
    ]
    result = await DefaultAgent().run(
        task="t", model=ScriptModel(responses=responses), env=env, tools=tools,
        config=AgentConfig(max_turns=5, enable_verifier=False),
    )
    assert result.success

    metrics = result.metadata["metrics"]
    assert metrics["parallel_segments"] == 1
    # Four 50ms reads overlapped: ~200ms of tool time in ~50ms of wall-clock. Asserted
    # as an inequality, not a duration, so a slow machine cannot make this flaky.
    assert metrics["tool_ms_total"] > metrics["tool_wall_ms_total"]
    assert metrics["parallel_saved_ms"] > 0.0


async def test_metrics_survive_a_model_error(tmp_path: Path):
    """How long a run spent before dying is exactly what you want to see."""
    env = LocalEnvironment(workspace_root=tmp_path)

    class ExplodingModel:
        model_name = "test/exploding"

        async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
            raise RuntimeError("provider is down")

        def count_tokens(self, messages):
            return 0

    result = await DefaultAgent().run(
        task="t", model=ExplodingModel(), env=env, tools=default_tools(),
        config=AgentConfig(max_turns=3, enable_verifier=False),
    )
    assert not result.success
    metrics = result.metadata["metrics"]
    assert metrics["turns"] == 1
    assert metrics["model_ms_total"] >= 0.0
