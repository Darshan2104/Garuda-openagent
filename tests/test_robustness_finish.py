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
    parse_launch,
    process_pattern,
    wrap_launch,
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
    """A process table the sweep can actually change.

    Returning a fixed pid list to every probe would let a sweep that never kills
    anything pass, which is the bug this fake exists to catch. Here a probe
    reports what is alive *now*: ``visible_after`` models a launch that has not
    reached exec() yet, and ``survives`` a process that outlives its signals.
    """

    workspace_root = "/app"

    def __init__(self, pids="4242", visible_after: int = 0, survives: bool = False):
        self.pids = pids
        self.ran: list[str] = []
        self._probes = 0
        self._visible_after = visible_after
        self._survives = survives
        self._alive = True

    @property
    def probes(self) -> int:
        return self._probes

    async def execute(self, command, timeout=None, cwd=None):
        self.ran.append(command)
        # Kill commands may embed ``pgrep -P`` to reap children; classify kill first.
        if "/bin/kill" in command or "kill -" in command:
            if not self._survives:
                self._alive = False
            return _exec()
        if "pgrep" in command:
            self._probes += 1
            visible = self._alive and self._probes > self._visible_after
            return _exec(stdout=self.pids if visible else "")
        if command.startswith("ss "):
            return _exec(stdout="LISTEN 0 128 0.0.0.0:9999")
        return _exec()

    async def read_file(self, path):
        return ""

    async def write_file(self, path, content):
        return None


def _server_ledger(command="python3 /app/puzzle_server.py &"):
    ledger = SideEffectLedger()
    ledger.observe(
        ToolCall(id="1", name="bash", arguments={"command": command}),
        ToolResult(tool_call_id="1", content="ok"),
    )
    return ledger


async def test_sweep_kills_agent_started_process():
    ledger = _server_ledger()
    assert ledger.has_pending
    env = _SweepEnv()
    await ledger.sweep(env)
    assert ledger.swept and ledger.swept[0]["killed"] is True
    assert ledger.summary()["processes_surviving"] == []
    assert any("kill -s TERM" in c for c in env.ran)
    # The sweep must never be able to take down the container it runs in.
    kill_cmd = next(c for c in env.ran if "kill -s TERM" in c)
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


# --- W3 regression: the sweep has to look twice ----------------------------


async def test_sweep_catches_a_process_that_starts_after_the_first_probe():
    """The launching shell returns before its child reaches exec().

    A single probe sees nothing, concludes the box is clean, and the process
    outlives the run — the exact leak this sweep exists to prevent.
    """
    ledger = _server_ledger()
    env = _SweepEnv(visible_after=1)
    await ledger.sweep(env)
    assert env.probes >= 2, "one probe cannot distinguish 'gone' from 'not started yet'"
    assert ledger.swept[0]["killed"] is True
    assert ledger.swept[0]["survivors"] == []


async def test_sweep_confirms_absence_rather_than_assuming_it():
    """Finding a pid is not evidence the signal landed."""
    ledger = _server_ledger()
    env = _SweepEnv(survives=True)
    await ledger.sweep(env)
    entry = ledger.swept[0]
    assert entry["survivors"] == ["4242"]
    assert ledger.summary()["processes_surviving"] == ["python3 /app/puzzle_server.py"]
    assert "STILL RUNNING" in ledger.render()
    # It escalated instead of giving up after one TERM.
    assert sum(1 for c in env.ran if "kill -s KILL" in c) >= 1


async def test_sweep_reports_a_process_that_was_already_gone():
    ledger = _server_ledger()
    env = _SweepEnv(pids="")
    await ledger.sweep(env)
    assert ledger.swept[0] == {**ledger.processes[0], "killed": False, "survivors": []}
    assert not any("kill -" in c for c in env.ran)


async def test_probe_excludes_the_shell_that_carries_the_pattern_in_its_argv():
    """`pgrep -f` matches the probe's own command line.

    Without the exclusion the sweep finds itself, reports a kill, and calls a
    workspace clean that still has a server on it.
    """
    ledger = _server_ledger()
    env = _SweepEnv()
    await ledger.sweep(env)
    probe = next(c for c in env.ran if "pgrep" in c)
    assert '"$p" = "$$"' in probe
    assert '"$p" = "$PPID"' in probe
    assert '"$p" = "1"' in probe


# --- W3 regression: the process group, not the command text ----------------


def test_the_launch_wrapper_leaves_the_command_and_its_exit_status_alone():
    wrapped = wrap_launch("python3 server.py &", "/tmp/garuda-launch/abc")
    assert wrapped.startswith("python3 server.py &\n")
    assert "__garuda_rc=$?" in wrapped  # captured before the probe can clobber it
    assert wrapped.endswith("exit $__garuda_rc")
    assert "/tmp/garuda-launch/abc" in wrapped


def test_launch_records_are_parsed_or_declined():
    assert parse_launch("4242 4242\n") == {"pid": "4242", "pgid": "4242"}
    for junk in ("", "\n", "not a pid", "4242", "sh: ps: not found"):
        assert parse_launch(junk) is None


def _launched(pid="4242", pgid="4242", command="python3 /app/puzzle_server.py &"):
    """A ledger holding one bash launch that reported the given pid/pgid."""
    ledger = SideEffectLedger()
    ledger.observe(
        ToolCall(id="1", name="bash", arguments={"command": command}),
        ToolResult(
            tool_call_id="1",
            content="ok",
            metadata={"launch": {"pid": pid, "pgid": pgid}},
        ),
    )
    return ledger


def test_a_launch_that_led_its_group_is_recorded_by_group():
    assert _launched().processes[0]["pgid"] == "4242"


def test_a_launch_that_did_not_lead_its_group_declines_it():
    """A persistent shell serves every command in the run; its group is not ours."""
    ledger = _launched(pid="4242", pgid="900")
    assert ledger.processes[0]["pgid"] is None


@pytest.mark.parametrize("pgid", ["1", "0", "", "nonsense", None])
def test_unusable_group_ids_are_declined(pgid):
    ledger = _launched(pid=pgid, pgid=pgid)
    assert ledger.processes[0]["pgid"] is None


def test_a_group_is_swept_even_when_the_command_is_too_vague_to_pattern_match():
    """`process_pattern` gives up on a bare interpreter; the group still holds."""
    ledger = _launched(command="python3 &")
    entry = ledger.processes[0]
    assert entry["pattern"] is None and entry["pgid"] == "4242"


async def test_the_sweep_finds_a_process_whose_argv_no_longer_matches():
    """The macOS case: `python3 x.py` re-execs as `.../MacOS/Python x.py`.

    The pattern derived from the command text matches nothing at all, so a sweep
    with only that source reports a clean workspace over a live server.
    """

    class _RenamedEnv(_SweepEnv):
        async def execute(self, command, timeout=None, cwd=None):
            self.ran.append(command)
            # Kill may embed ``pgrep -P``; classify it before pattern probes.
            if "/bin/kill" in command or "kill -" in command:
                self._alive = False
                return _exec()
            if "pgrep" in command:
                return _exec(stdout="")  # the argv no longer resembles the command
            if "ps -Ao" in command:
                self._probes += 1
                return _exec(stdout=self.pids if self._alive else "")
            return _exec()

    ledger = _launched()
    env = _RenamedEnv()
    await ledger.sweep(env)
    assert ledger.swept[0]["killed"] is True
    assert ledger.swept[0]["survivors"] == []
    assert any("ps -Ao pid=,pgid=" in c for c in env.ran)


async def test_the_group_probe_never_targets_init_or_the_probing_shell():
    ledger = _launched()
    env = _SweepEnv(pids="")
    await ledger.sweep(env)
    probe = next(c for c in env.ran if "ps -Ao" in c)
    assert '"$p" = "$$"' in probe
    assert '"$p" = "$PPID"' in probe
    assert '"$p" = "1"' in probe
    assert '[ "$g" = 4242 ]' in probe


async def test_sweep_survives_an_environment_that_raises():
    class _Broken(_SweepEnv):
        async def execute(self, command, timeout=None, cwd=None):
            raise RuntimeError("workspace gone")

    ledger = _server_ledger()
    await ledger.sweep(_Broken())
    assert ledger.swept[0]["killed"] is False
    assert ledger.swept[0]["error"] is True


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


def test_criteria_can_be_resolved_in_one_batch():
    """Cost fix from the 2026-07-28 benchmark run: 229 contract calls across 17
    tasks, every one carrying a single mark, at one model round-trip each — about
    two thirds of this harness's excess model calls versus a comparable one. A
    task derives ~15 criteria, so batching is the difference between ~15 turns of
    bookkeeping and one."""
    contract = _contract()
    applied, messages = contract.mark_many(
        [
            {"id": "c1", "status": VERIFIED, "note": "ran compress.py, diffed 20 filenames"},
            {"id": "c2", "status": ASSUMED, "note": "task gave no count; chose 10000"},
        ]
    )
    assert applied == 2, messages
    assert contract.outstanding == []


def test_a_bad_entry_does_not_discard_the_good_ones():
    """Partial application on purpose: forcing a full resend after one malformed
    note would hand back the turns batching just saved."""
    contract = _contract()
    applied, messages = contract.mark_many(
        [
            {"id": "c1", "status": VERIFIED, "note": "ran compress.py"},
            {"id": "c9", "status": VERIFIED, "note": "no such criterion"},
            {"id": "c2", "status": VERIFIED},  # verified with no note
        ]
    )
    assert applied == 1
    assert contract.get("c1").status == VERIFIED
    assert contract.get("c2").status not in (VERIFIED,), "note-less verify must not land"
    assert any("c9" in m for m in messages)


async def test_contract_tool_accepts_a_batch_and_still_accepts_one():
    """The tool surface, not just the model: both shapes must work, and the batch
    path must render the contract once rather than once per criterion."""
    from garuda.tools.contract import ContractTool
    from garuda.tools.protocol import ToolContext

    contract = _contract()
    tool = ContractTool()
    tool.bind("s1", contract)
    ctx = ToolContext(session_id="s1")

    batch = await tool.execute(
        {
            "action": "mark",
            "marks": [{"id": "c1", "status": VERIFIED, "note": "ran compress.py"}],
        },
        None,
        ctx,
    )
    assert batch.is_error is False
    assert contract.get("c1").status == VERIFIED

    single = await tool.execute(
        {"action": "mark", "id": "c2", "status": ASSUMED, "note": "chose 10000"},
        None,
        ctx,
    )
    assert single.is_error is False, "the single-mark form must keep working"
    assert contract.outstanding == []

    nothing = await tool.execute({"action": "mark"}, None, ctx)
    assert nothing.is_error is True and "marks" in nothing.content


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
