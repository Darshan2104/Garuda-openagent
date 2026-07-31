"""Concurrency in the completion gate must not move the evidence or the verdict.

The win here is narrow by construction (the allowlist excludes every assertion
runner), so what these tests mostly protect is that nothing *changed*: evidence stays
in the agent's command order, a failure still reports the first-in-order command, and
a command that might write still runs alone.
"""

import asyncio

from garuda.core.verifier import CompletionVerifier, gather_git_evidence
from garuda.types import AgentConfig, ExecResult


class RecordingEnv:
    """Records call order and observed concurrency for each command."""

    def __init__(self, exit_codes: dict[str, int] | None = None, delay: float = 0.0):
        self.exit_codes = exit_codes or {}
        self.delay = delay
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def execute(self, command: str, timeout: float | None = None, **kwargs) -> ExecResult:
        self.calls.append(command)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.active -= 1
        code = self.exit_codes.get(command, 0)
        return ExecResult(stdout=f"out:{command}", stderr="", exit_code=code, duration_ms=0)


def _config(**kwargs) -> AgentConfig:
    return AgentConfig(enable_verifier=True, **kwargs)


async def test_readonly_commands_run_concurrently():
    env = RecordingEnv(delay=0.05)
    verifier = CompletionVerifier()
    observed: list[dict] = []
    early = await verifier._run_commands(
        ["cat a.txt", "cat b.txt", "cat c.txt"], env, _config(), None, {}, observed
    )
    assert early is None
    assert env.max_active == 3
    assert [entry["command"] for entry in observed] == ["cat a.txt", "cat b.txt", "cat c.txt"]


async def test_mutating_commands_never_overlap():
    """`pytest` twice concurrently against one tree is exactly what must not happen."""
    env = RecordingEnv(delay=0.05)
    verifier = CompletionVerifier()
    early = await verifier._run_commands(
        ["pytest -q", "make check"], env, _config(), None, {}, []
    )
    assert early is None
    assert env.max_active == 1


async def test_evidence_order_follows_the_agents_command_order():
    """Gathered commands may finish out of order; the evidence must not reflect that."""
    env = RecordingEnv(delay=0.02)
    verifier = CompletionVerifier()
    commands = ["cat a", "cat b", "pytest -q", "cat c", "cat d"]
    observed: list[dict] = []
    checklist: dict = {}
    early = await verifier._run_commands(commands, env, _config(), None, checklist, observed)
    assert early is None
    assert [entry["command"] for entry in observed] == commands
    # checklist keys stay in the agent's original numbering.
    assert [checklist[f"verify_cmd_{i}"] for i in range(5)] == [True] * 5


async def test_failure_inside_a_gathered_segment_reports_the_first_in_order():
    env = RecordingEnv(exit_codes={"cat b": 1, "cat c": 1}, delay=0.01)
    verifier = CompletionVerifier()
    observed: list[dict] = []
    checklist: dict = {}
    early = await verifier._run_commands(
        ["cat a", "cat b", "cat c"], env, _config(), None, checklist, observed
    )
    assert early is not None
    assert not early.approved
    # `cat b` failed first in the agent's order, even though `cat c` also failed.
    assert "cat b" in early.feedback
    assert early.feedback.startswith("Verification command failed (exit 1): cat b")
    assert checklist["verify_cmd_1"] is False
    assert checklist["verify_cmd_2"] is False


async def test_serial_segment_short_circuits_exactly_as_before():
    """A failing command that might write stops the walk: nothing after it runs."""
    env = RecordingEnv(exit_codes={"pytest -q": 1})
    verifier = CompletionVerifier()
    early = await verifier._run_commands(
        ["pytest -q", "make check"], env, _config(), None, {}, []
    )
    assert early is not None and not early.approved
    assert env.calls == ["pytest -q"]


async def test_disabling_the_flag_runs_everything_serially():
    env = RecordingEnv(delay=0.05)
    verifier = CompletionVerifier()
    early = await verifier._run_commands(
        ["cat a", "cat b", "cat c"], env, _config(parallel_verification=False), None, {}, []
    )
    assert early is None
    assert env.max_active == 1


async def test_environment_death_aborts_rather_than_becoming_a_verdict():
    """A run that cannot observe its workspace has no basis for any completion verdict."""
    from garuda.workspace.health import EnvironmentUnavailableError

    class DeadEnv:
        async def execute(self, command, timeout=None, **kwargs):
            raise EnvironmentUnavailableError(reason="container gone", detail="exited")

    verifier = CompletionVerifier()
    for commands in (["cat a", "cat b"], ["pytest -q"]):
        try:
            await verifier._run_commands(commands, DeadEnv(), _config(), None, {}, [])
        except EnvironmentUnavailableError:
            continue
        raise AssertionError(f"expected abort for {commands}")


async def test_permission_denial_reports_the_denied_command():
    class DenyingPermissions:
        async def evaluate_tool_call(self, tool_name, arguments):
            if arguments.get("command") == "cat b":
                return False, "blocked by policy"
            return True, None

    env = RecordingEnv()
    verifier = CompletionVerifier()
    observed: list[dict] = []
    checklist: dict = {}
    early = await verifier._run_commands(
        ["cat a", "cat b", "cat c"], env, _config(), DenyingPermissions(), checklist, observed
    )
    assert early is not None and not early.approved
    assert "denied by permission policy: cat b" in early.feedback
    assert checklist["verify_cmd_1"] is False
    # Nothing past the denial was screened, so `cat c` never ran.
    assert "cat c" not in env.calls


async def test_git_evidence_gathers_both_reads():
    env = RecordingEnv(delay=0.05)
    text = await gather_git_evidence(env)
    assert env.max_active == 2
    # Order is fixed by the command tuple, not by which finished first.
    assert text.index("git status --short") < text.index("git diff HEAD --stat")


async def test_git_evidence_is_empty_outside_a_repo():
    env = RecordingEnv(exit_codes={"git rev-parse --is-inside-work-tree": 1})
    assert await gather_git_evidence(env) == ""
    assert env.calls == ["git rev-parse --is-inside-work-tree"]


async def test_git_evidence_tolerates_one_failing_read():
    env = RecordingEnv(exit_codes={"git diff HEAD --stat": 128})
    text = await gather_git_evidence(env)
    assert "git status --short" in text
    assert "git diff HEAD --stat" not in text
