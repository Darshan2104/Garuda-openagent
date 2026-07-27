"""Phase 2 (W3 side effects, W4 budget) and Phase 3 (W2 acceptance criteria)."""

import time

import pytest

from garuda.core.contract import (
    ASSUMED,
    UNVERIFIABLE,
    VERIFIED,
    AcceptanceContract,
    Criterion,
    _coerce,
)
from garuda.core.loop import _budget_fraction
from garuda.core.side_effects import (
    SideEffectLedger,
    is_backgrounding,
    process_pattern,
)
from garuda.tools.bash import (
    MIN_COMMAND_TIMEOUT_SEC,
    resolve_timeout,
)
from garuda.types import ExecResult, ToolCall, ToolResult


def _exec(stdout="", code=0):
    return ExecResult(stdout=stdout, stderr="", exit_code=code, duration_ms=1)


# --- W3: recognising what was left running ---------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 /app/puzzle_server.py &",
        "nohup python3 server.py > log 2>&1 &",
        "setsid ./daemon --port 9999",
        "cd /app && python3 app.py &",
        "nginx",
    ],
)
def test_backgrounding_is_detected(command):
    assert is_backgrounding(command)


@pytest.mark.parametrize(
    "command",
    ["python3 solve.py", "make build && make test", "ls -la /app", "a && b"],
)
def test_foreground_commands_are_not_flagged(command):
    assert not is_backgrounding(command)


def test_pattern_targets_the_specific_launch():
    # The exact failure from solve-escape-room: a server left holding port 9999.
    assert process_pattern("python3 /app/puzzle_server.py &") == "python3 /app/puzzle_server.py"
    assert process_pattern("cd /app && nohup python3 srv.py &") == "python3 srv.py"


def test_pattern_refuses_to_be_vague():
    """A bare interpreter name would sweep unrelated processes, including our own."""
    assert process_pattern("python3 &") is None
    assert process_pattern("bash &") is None


class _SweepEnv:
    workspace_root = "/app"

    def __init__(self, pids="4242"):
        self.pids = pids
        self.ran: list[str] = []

    async def execute(self, command, timeout=None, cwd=None):
        self.ran.append(command)
        if command.startswith("pgrep"):
            return _exec(stdout=self.pids)
        if command.startswith("ss "):
            return _exec(stdout="LISTEN 0 128 0.0.0.0:9999")
        return _exec()

    async def read_file(self, path):
        return ""

    async def write_file(self, path, content):
        return None


async def test_sweep_kills_agent_started_process():
    ledger = SideEffectLedger()
    ledger.observe(
        ToolCall(id="1", name="bash", arguments={"command": "python3 /app/puzzle_server.py &"}),
        ToolResult(tool_call_id="1", content="ok"),
    )
    assert ledger.has_pending
    env = _SweepEnv()
    await ledger.sweep(env)
    assert ledger.swept and ledger.swept[0]["killed"] is True
    assert any("kill -TERM" in c for c in env.ran)
    # The sweep must never be able to take down the container it runs in.
    kill_cmd = next(c for c in env.ran if "kill -TERM" in c)
    assert '"$p" != "1"' in kill_cmd


async def test_sweep_is_a_noop_without_background_processes():
    ledger = SideEffectLedger()
    ledger.observe(
        ToolCall(id="1", name="bash", arguments={"command": "python3 solve.py"}),
        ToolResult(tool_call_id="1", content="ok"),
    )
    env = _SweepEnv()
    await ledger.sweep(env)
    assert ledger.swept == []
    assert not any("kill" in c for c in env.ran)


def test_failed_calls_are_not_recorded():
    ledger = SideEffectLedger()
    ledger.observe(
        ToolCall(id="1", name="write_file", arguments={"path": "/app/x"}),
        ToolResult(tool_call_id="1", content="denied", is_error=True),
    )
    assert ledger.files_written == set()


def test_ledger_records_writes():
    ledger = SideEffectLedger()
    ledger.observe(
        ToolCall(id="1", name="write_file", arguments={"path": "/app/x.py"}),
        ToolResult(tool_call_id="1", content="ok"),
    )
    assert ledger.summary()["files_written"] == ["/app/x.py"]


# --- W4: budget ------------------------------------------------------------


def test_budget_fraction_uses_whichever_is_further_along():
    now = time.monotonic()
    # 10% of turns spent, but 90% of the clock.
    fraction = _budget_fraction(turn=1, max_turns=10, started_at=now - 90, deadline_at=now + 10)
    assert fraction == pytest.approx(0.9, abs=0.05)
    # No deadline configured: turns are the only signal.
    assert _budget_fraction(turn=5, max_turns=10, started_at=now, deadline_at=None) == 0.5


def test_command_timeout_is_capped_by_remaining_budget():
    deadline = time.monotonic() + 100
    effective, asked, remaining = resolve_timeout(900, deadline, 0.5)
    assert effective == pytest.approx(50, abs=2)
    assert asked == 900  # reported back so the model learns it was capped


def test_modest_request_within_budget_is_untouched():
    deadline = time.monotonic() + 600
    effective, asked, _ = resolve_timeout(30, deadline, 0.5)
    assert effective == 30 and asked is None


def test_exhausted_budget_still_allows_a_short_command():
    """The agent must retain enough room to write out a final answer."""
    effective, _, _ = resolve_timeout(900, time.monotonic() - 1, 0.5)
    assert effective == MIN_COMMAND_TIMEOUT_SEC


def test_no_deadline_keeps_the_requested_timeout():
    effective, asked, _ = resolve_timeout(900, None, 0.5)
    assert effective == 900 and asked is None


# --- W2: acceptance criteria ----------------------------------------------


def _contract():
    return AcceptanceContract(
        criteria=[
            Criterion(id="c1", text="Write /app/out.txt.lz77 for each input file", kind="path"),
            Criterion(id="c2", text="Use 10000 iterations", kind="numeric", specified=False),
        ]
    )


def test_outstanding_blocks_until_resolved():
    contract = _contract()
    assert len(contract.outstanding) == 2
    contract.mark("c1", VERIFIED, "ran compress.py and diffed all 20 filenames")
    assert len(contract.outstanding) == 1
    contract.mark("c2", ASSUMED, "task gave no count; chose 10000, the conventional minimum")
    assert contract.outstanding == []


def test_marking_verified_requires_saying_how():
    """A status with no evidence behind it is the self-report problem again."""
    contract = _contract()
    ok, message = contract.mark("c1", VERIFIED, "")
    assert ok is False and "requires a note" in message
    assert contract.outstanding  # unchanged


def test_unknown_criterion_is_reported_with_valid_ids():
    ok, message = _contract().mark("c9", VERIFIED, "note")
    assert ok is False and "c1" in message


def test_unverifiable_is_an_explicit_escape_not_a_silent_one():
    contract = _contract()
    contract.mark("c1", UNVERIFIABLE, "no network in this sandbox to fetch the reference file")
    assert contract.get("c1").status == UNVERIFIABLE
    assert contract.get("c1").note


def test_render_marks_open_values_for_the_model():
    rendered = _contract().render(header=True)
    assert "value not given by the task" in rendered
    assert "c1" in rendered and "c2" in rendered


def test_extraction_coercion_is_defensive():
    contract = _coerce(
        {
            "criteria": [
                {"text": "Output must be /app/x.csv", "kind": "path", "specified": True},
                {"text": "", "kind": "path"},  # dropped
                "not a dict",  # dropped
            ]
        }
    )
    assert [c.text for c in contract.criteria] == ["Output must be /app/x.csv"]
    assert contract.criteria[0].id == "c1"


def test_coercion_survives_a_garbage_payload():
    assert _coerce({}).criteria == []
    assert _coerce({"criteria": "nope"}).criteria == []


def test_stats_report_progress():
    contract = _contract()
    contract.mark("c1", VERIFIED, "ran it")
    stats = contract.stats()
    assert stats["total"] == 2 and stats[VERIFIED] == 1
