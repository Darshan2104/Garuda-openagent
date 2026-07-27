"""Phase 0: environment liveness (W5) and the session-wide action memo (W6)."""

import pytest

from garuda.core.action_memo import ActionMemo
from garuda.core.events import EventType
from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, ExecResult, ToolCall
from garuda.workspace.health import (
    EnvironmentUnavailableError,
    HealthMonitoredEnvironment,
    classify_exception,
    classify_exec,
    monitored,
)


def _exec(stdout="", stderr="", code=0):
    return ExecResult(stdout=stdout, stderr=stderr, exit_code=code, duration_ms=1)


# --- W5: classification ----------------------------------------------------


def test_dead_container_output_is_fatal():
    assert classify_exec(_exec(stdout='service "main" is not running', code=1)) == "service_not_running"
    assert classify_exec(_exec(stderr="Error: No such container: abc", code=1)) == "no_such_container"
    assert classify_exec(_exec(stderr="Cannot connect to the Docker daemon", code=1)) == "daemon_unreachable"


def test_ordinary_command_failure_is_not_fatal():
    assert classify_exec(_exec(stderr="ModuleNotFoundError: no module named x", code=1)) is None
    assert classify_exec(_exec(stdout="3 failed, 9 passed", code=1)) is None
    assert classify_exec(_exec(stdout="", code=127)) is None


def test_successful_command_never_fatal_even_if_output_mentions_the_phrase():
    # An agent grepping logs must not be able to kill its own run.
    assert classify_exec(_exec(stdout='service "main" is not running', code=0)) is None


def test_exception_classification():
    assert classify_exception(ConnectionResetError("peer")) == "exception:ConnectionResetError"
    assert classify_exception(ValueError("nope")) is None


# --- W5: proxy behaviour ---------------------------------------------------


class _FlakyEnv:
    """Environment that dies after ``alive_for`` successful calls."""

    workspace_root = "/app"

    def __init__(self, alive_for: int):
        self.alive_for = alive_for
        self.calls = 0

    async def execute(self, command, timeout=None, cwd=None):
        self.calls += 1
        if self.calls > self.alive_for:
            return _exec(stdout='service "main" is not running', code=1)
        return _exec(stdout="ok")

    async def read_file(self, path):
        return "x"

    async def write_file(self, path, content):
        return None


async def test_proxy_raises_then_latches():
    env = HealthMonitoredEnvironment(_FlakyEnv(alive_for=1))
    assert (await env.execute("echo hi")).stdout == "ok"
    with pytest.raises(EnvironmentUnavailableError):
        await env.execute("echo hi")
    # Latched: no further transport attempts are made.
    before = env.inner.calls
    with pytest.raises(EnvironmentUnavailableError):
        await env.execute("echo hi")
    assert env.inner.calls == before
    assert env.is_alive is False


def test_monitored_is_idempotent():
    env = monitored(_FlakyEnv(alive_for=1))
    assert monitored(env) is env


class _DeadAfterFirstToolEnv(_FlakyEnv):
    pass


async def test_run_aborts_when_environment_dies():
    """The run must stop, not keep working against a workspace that is gone."""
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="1", name="bash", arguments={"command": "echo one"})],
        ),
        ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="2", name="bash", arguments={"command": "echo two"})],
        ),
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="3",
                    name="task_complete",
                    arguments={"summary": "All done and fully verified, honestly."},
                )
            ],
        ),
    ]
    env = _DeadAfterFirstToolEnv(alive_for=1)
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=responses),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=6, enable_acceptance_contract=False),
    )
    assert result.success is False
    assert "unavailable" in result.final_message
    kinds = [e["type"] for e in result.metadata["events"]]
    assert EventType.ENVIRONMENT_UNAVAILABLE.value in kinds
    end = [e for e in result.metadata["events"] if e["type"] == EventType.SESSION_END.value][-1]
    assert end["payload"]["reason"] == "environment_unavailable"
    # And crucially: it never claimed success against the dead box.
    assert end["payload"]["success"] is False


# --- W6: action memo -------------------------------------------------------


def _call(name, **args):
    return ToolCall(id="x", name=name, arguments=args)


def test_memo_serves_repeated_read_and_invalidates_on_write():
    memo = ActionMemo()
    read = _call("read_file", path="/app/x.py")

    sig, count = memo.observe(read)
    assert count == 1 and memo.lookup(read, sig) is None
    memo.record(read, sig, "contents", False)

    sig2, count2 = memo.observe(read)
    assert count2 == 2
    cached = memo.lookup(read, sig2)
    assert cached is not None and "contents" in cached[0] and "[cached]" in cached[0]

    # A write invalidates: the cached read no longer describes the workspace.
    write = _call("write_file", path="/app/x.py", content="new")
    wsig, _ = memo.observe(write)
    memo.record(write, wsig, "ok", False)

    sig3, _ = memo.observe(read)
    assert memo.lookup(read, sig3) is None


def test_memo_never_caches_bash():
    memo = ActionMemo()
    cmd = _call("bash", command="python3 train.py")
    sig, _ = memo.observe(cmd)
    memo.record(cmd, sig, "output", False)
    sig2, _ = memo.observe(cmd)
    assert memo.lookup(cmd, sig2) is None  # re-running may be legitimate


def test_memo_steers_on_nonconsecutive_repetition():
    """The loop's own detector only sees back-to-back repeats; this catches the rest."""
    memo = ActionMemo()
    a = _call("bash", command="python3 slow_thing.py")
    b = _call("bash", command="ls /app")
    note = None
    for _ in range(3):
        sig, count = memo.observe(a)
        note = memo.steer_note(a, sig, count) or note
        sig_b, count_b = memo.observe(b)  # interleaved: never consecutive
        memo.steer_note(b, sig_b, count_b)
    assert note is not None
    assert "3 times" in note and "slow_thing.py" in note


def test_memo_steers_only_once_per_signature():
    memo = ActionMemo()
    call = _call("bash", command="echo hi")
    notes = []
    for _ in range(6):
        sig, count = memo.observe(call)
        note = memo.steer_note(call, sig, count)
        if note:
            notes.append(note)
    assert len(notes) == 1


def test_memo_stats_report_repeats():
    memo = ActionMemo()
    call = _call("ls", path="/app")
    for _ in range(3):
        sig, _ = memo.observe(call)
        memo.record(call, sig, "listing", False)
    stats = memo.stats()
    assert stats["total_calls"] == 3
    assert stats["distinct_calls"] == 1
    assert stats["repeat_calls"] == 2
