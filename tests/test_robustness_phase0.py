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


# --- W6 regression: a background task writes between tool calls -------------


def _run(memo, call, content="", metadata=None):
    """Put one call through the memo, returning what it served (None = executed)."""
    sig, _ = memo.observe(call)
    cached = memo.lookup(call, sig)
    if cached is None:
        memo.record(call, sig, content, False, metadata)
    return cached


def test_polling_a_background_task_cannot_serve_a_stale_read():
    """launch -> read -> background writer -> poll -> reread.

    Invalidate-on-mutation assumes the tool stream is the only writer. A build
    started with bash_background keeps writing between calls, and `task_output`
    is declared non-mutating, so nothing after the launch marks the file as
    changed. The read has to be taken *after* the launch for the hole to show:
    the launch's own invalidation covers everything before it and nothing after.
    """
    memo = ActionMemo()
    read = _call("read_file", path="/app/out.txt")

    _run(memo, _call("bash_background", command="make build"), "started", {"background_tasks": 1})
    _run(memo, read, "old contents")
    # ... the build writes /app/out.txt here, invisibly to the tool stream ...
    _run(memo, _call("task_output", task_id="a1"), "still running", {"background_tasks": 1})

    assert _run(memo, read, "new contents") is None, "the reread must actually hit disk"


def test_reads_stay_uncached_for_as_long_as_a_task_is_live():
    """Two reads with no call at all between them are just as exposed."""
    memo = ActionMemo()
    read = _call("read_file", path="/app/out.txt")
    _run(memo, _call("bash_background", command="make build"), "started", {"background_tasks": 1})

    assert _run(memo, read, "first") is None
    assert _run(memo, read, "second") is None


def test_caching_resumes_once_the_last_task_has_exited():
    memo = ActionMemo()
    read = _call("read_file", path="/app/out.txt")
    _run(memo, _call("bash_background", command="make build"), "started", {"background_tasks": 1})
    _run(memo, read, "mid-build")
    # The poll that observes the exit drops the count — and with it anything
    # cached during the window in which the build was writing.
    _run(memo, _call("task_output", task_id="a1"), "exited", {"background_tasks": 0})

    assert _run(memo, read, "final") is None  # first post-build read is real
    cached = _run(memo, read)
    assert cached is not None and "final" in cached[0]


def test_buffer_reads_stay_memoized_while_a_task_runs():
    """A live task suspends filesystem caching, not all caching.

    The output buffer holds a stored copy of an earlier tool result in this
    process; a background command has no way to reach it, so re-slicing it is
    the one repeated read that is still safe to answer from memory.
    """
    memo = ActionMemo()
    read = _call("read_file", path="/app/out.txt")
    slice_call = _call("buffer_slice", buffer_id="buf_1", start=0)
    _run(memo, _call("bash_background", command="make build"), "started", {"background_tasks": 1})

    assert _run(memo, slice_call, "lines 0-40") is None  # first one executes
    cached = _run(memo, slice_call)
    assert cached is not None and "lines 0-40" in cached[0]

    # Same sequence against the filesystem never gets a second chance.
    assert _run(memo, read, "contents") is None
    assert _run(memo, read, "contents") is None


def test_a_raw_backgrounding_bash_command_suspends_filesystem_caching():
    """`bash("make build &")` is the same hazard with none of the bookkeeping.

    The live-task count only exists for `bash_background`. A shell command that
    backgrounds its own writer invalidates once, on the way through — and then
    every later read is cacheable again while the build is still writing.
    """
    memo = ActionMemo()
    read = _call("read_file", path="/app/out.txt")

    _run(memo, _call("bash", command="make build > build.log 2>&1 &"), "ok")
    assert memo.untracked_writer is True

    assert _run(memo, read, "mid-build") is None
    assert _run(memo, read, "mid-build") is None, "a build is still writing to it"


@pytest.mark.parametrize(
    "command",
    ["python3 server.py &", "nohup ./run.sh &", "setsid ./daemon --port 80", "nginx"],
)
def test_every_form_of_backgrounding_counts(command):
    memo = ActionMemo()
    _run(memo, _call("bash", command=command), "ok")
    assert memo.filesystem_is_volatile is True


def test_an_ordinary_command_leaves_caching_alone():
    """The suspension is sticky, so it must not fire on `a && b` or a plain run."""
    memo = ActionMemo()
    read = _call("read_file", path="/app/out.txt")
    for command in ("make build", "a && b", "grep -r x . | head", "ls -la"):
        _run(memo, _call("bash", command=command), "ok")
    assert memo.untracked_writer is False

    assert _run(memo, read, "contents") is None
    cached = _run(memo, read)
    assert cached is not None and "contents" in cached[0]


def test_bash_background_does_not_trip_the_sticky_flag():
    """It reports its own exits, so caching may resume when the task ends."""
    memo = ActionMemo()
    read = _call("read_file", path="/app/out.txt")
    _run(memo, _call("bash_background", command="make build"), "started", {"background_tasks": 1})
    assert memo.untracked_writer is False

    _run(memo, _call("task_output", task_id="a1"), "exited", {"background_tasks": 0})
    assert _run(memo, read, "final") is None
    assert _run(memo, read) is not None  # caching resumed


def test_killing_a_task_invalidates_what_it_may_have_written():
    memo = ActionMemo()
    read = _call("read_file", path="/app/out.txt")
    _run(memo, read, "before")
    _run(memo, _call("bash_background", command="make build"), "started", {"background_tasks": 1})
    _run(memo, _call("kill_task", task_id="a1"), "killed", {"background_tasks": 0})

    assert _run(memo, read, "after") is None
