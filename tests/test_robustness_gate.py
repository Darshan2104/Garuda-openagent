"""Phase 1 (W1): the completion gate must rest on evidence that could have failed."""

import pytest

from garuda.core import evidence
from garuda.core.verifier import CompletionGateState, CompletionVerifier
from garuda.types import AgentConfig, ExecResult


def _exec(stdout="", stderr="", code=0):
    return ExecResult(stdout=stdout, stderr=stderr, exit_code=code, duration_ms=1)


class _Env:
    workspace_root = "/app"

    def __init__(self, results=None, default=0):
        self.results = results or {}
        self.default = default
        self.ran: list[str] = []

    async def execute(self, command, timeout=None, cwd=None):
        self.ran.append(command)
        code = self.results.get(command, self.default)
        if callable(code):
            code = code(self.ran.count(command))
        return _exec(stdout="out", code=code)

    async def read_file(self, path):
        return ""

    async def write_file(self, path, content):
        return None


# --- the classifier --------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat /app/answer.txt",
        "ls -la /app/*.json /app/*.txt",
        "echo 'Expected: ESCAPE_COMPLETE_7f17469e_SUCCESS'",
        "wc -l /app/result.txt",
        "od -c /app/answer.txt",
        "python3 -m py_compile /app/tensor_parallel.py && echo 'Syntax verification: PASSED'",
        'python3 -c "from query import SalesAnalyzer"',
        "stat /app/out.bin",
        "head -20 /app/log.txt",
    ],
)
def test_commands_that_cannot_fail_are_rejected_as_evidence(command):
    assert not evidence.is_discriminating(command), command


@pytest.mark.parametrize(
    "command",
    [
        "python3 /app/compress.py",
        "cd /app && python3 -m pytest tests/ -v",
        "diff -u /app/expected.txt /app/actual.txt",
        "cat /app/answer.txt | grep -q ESCAPE_COMPLETE",
        "./run_pipeline.sh",
        "test -f /app/out.txt && python3 /app/check.py",
        "curl -f http://localhost:8080/",
        "make test",
        "/usr/local/nginx/sbin/nginx -t",
        "python3 verify.py",
    ],
)
def test_commands_that_can_fail_count_as_evidence(command):
    assert evidence.is_discriminating(command), command


def test_pipeline_is_as_strong_as_its_strongest_stage():
    # `cat` alone proves nothing; piping into `grep -q` makes the exit code mean something.
    assert not evidence.is_discriminating("cat out.txt")
    assert evidence.is_discriminating("cat out.txt | grep -q EXPECTED")


def test_prefixes_do_not_hide_the_real_command():
    assert evidence.is_discriminating("cd /app && timeout 30 python3 solve.py")
    assert not evidence.is_discriminating("cd /app && cat solution.txt")


# --- the gate --------------------------------------------------------------


async def _verify(commands, env=None, gate=None, config=None):
    return await CompletionVerifier().verify_with_commands(
        task="Write /app/out.txt containing the answer.",
        summary="I wrote the answer to /app/out.txt and checked it.",
        verification_commands=commands,
        env=env or _Env(),
        # require_discriminating_evidence is named explicitly: it is the gate these
        # cases are about, and the default posture (`interactive`) has it off.
        config=config
        or AgentConfig(
            enable_llm_verifier=False,
            require_stable_verification=False,
            require_discriminating_evidence=True,
        ),
        gate=gate,
    )


async def test_completion_with_only_weak_evidence_is_rejected():
    result = await _verify(["cat /app/out.txt", "ls -la /app"])
    assert result.approved is False
    assert "none of your verification commands can fail" in result.feedback
    # The feedback must name each command and why it is weak, or the model
    # cannot act on it.
    assert "cat /app/out.txt" in result.feedback and "exits 0" in result.feedback


async def test_completion_with_no_evidence_is_rejected():
    result = await _verify([])
    assert result.approved is False
    assert "no verification_commands" in result.feedback


async def test_completion_with_real_evidence_is_approved():
    result = await _verify(["python3 /app/check.py", "cat /app/out.txt"])
    assert result.approved is True
    assert [e["exit_code"] for e in result.evidence] == [0, 0]


async def test_weak_evidence_gate_can_be_disabled():
    config = AgentConfig(
        enable_llm_verifier=False,
        require_discriminating_evidence=False,
        require_stable_verification=False,
    )
    result = await _verify(["cat /app/out.txt"], config=config)
    assert result.approved is True


# --- sticky rejection ------------------------------------------------------


async def test_resubmitting_the_same_commands_is_refused_without_running_them():
    gate = CompletionGateState()
    env = _Env(results={"python3 /app/check.py": 1})
    first = await _verify(["python3 /app/check.py"], env=env, gate=gate)
    assert first.approved is False
    gate.record_rejection(["python3 /app/check.py"], first.feedback)
    ran_before = len(env.ran)

    second = await _verify(["python3 /app/check.py"], env=env, gate=gate)
    assert second.approved is False
    assert "no new evidence" in second.feedback
    assert len(env.ran) == ran_before  # short-circuited, nothing re-executed


async def test_dropping_the_failing_check_is_refused():
    """The cheapest way past a rejection must not be removing the check that failed."""
    gate = CompletionGateState()
    gate.record_rejection(["python3 /app/check.py", "cat /app/out.txt"], "check.py exited 1")
    result = await _verify(["cat /app/out.txt"], gate=gate)
    assert result.approved is False
    assert "no new evidence" in result.feedback


async def test_empty_commands_after_a_rejection_is_refused():
    gate = CompletionGateState()
    gate.record_rejection(["python3 /app/check.py"], "check.py exited 1")
    result = await _verify([], gate=gate)
    assert result.approved is False


async def test_genuinely_new_evidence_is_accepted_after_a_rejection():
    gate = CompletionGateState()
    gate.record_rejection(["python3 /app/check.py"], "check.py exited 1")
    result = await _verify(["python3 /app/check.py", "python3 /app/deeper_check.py"], gate=gate)
    assert result.approved is True


# --- stability -------------------------------------------------------------


async def test_verification_that_does_not_repeat_is_rejected():
    """Work that verifies once and not twice depends on state the first run consumed."""
    calls = {"n": 0}

    class _OnceEnv(_Env):
        async def execute(self, command, timeout=None, cwd=None):
            self.ran.append(command)
            if command == "./run_pipeline.sh":
                calls["n"] += 1
                return _exec(code=0 if calls["n"] == 1 else 1)
            return _exec(code=0)

    config = AgentConfig(enable_llm_verifier=False, require_stable_verification=True)
    result = await _verify(["./run_pipeline.sh"], env=_OnceEnv(), config=config)
    assert result.approved is False
    assert "not repeatable" in result.feedback
    assert "idempotent" in result.feedback


async def test_stable_verification_passes():
    config = AgentConfig(enable_llm_verifier=False, require_stable_verification=True)
    result = await _verify(["./run_pipeline.sh"], config=config)
    assert result.approved is True


async def test_stability_recheck_skips_weak_commands():
    """Re-running `cat` proves nothing and must not cost time."""
    env = _Env()
    config = AgentConfig(enable_llm_verifier=False, require_stable_verification=True)
    await _verify(["python3 /app/check.py", "cat /app/out.txt"], env=env, config=config)
    assert env.ran.count("python3 /app/check.py") == 2
    assert env.ran.count("cat /app/out.txt") == 1
