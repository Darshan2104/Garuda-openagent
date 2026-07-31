"""End-to-end: the hardened gate as the loop actually applies it.

Each test reproduces a failure mode observed on terminal-bench-pro and asserts
the agent can no longer finish that way.
"""

import os
from pathlib import Path

from garuda.core.events import EventType
from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.local import LocalEnvironment

SUMMARY = "Implemented the solution and verified it end to end."


def _complete(commands=None, summary=SUMMARY, call_id="tc"):
    args = {"summary": summary}
    if commands is not None:
        args["verification_commands"] = commands
    return ModelResponse(
        content=None, tool_calls=[ToolCall(id=call_id, name="task_complete", arguments=args)]
    )


def _bash(command, call_id="b"):
    return ModelResponse(
        content=None, tool_calls=[ToolCall(id=call_id, name="bash", arguments={"command": command})]
    )


def _config(**kw):
    # These tests exercise the strict gates, so they name them explicitly rather
    # than inheriting defaults: the default posture is `interactive`, which has
    # them off. The two that stay off here are the ones that would cost a model
    # call or a second command run and are not what these cases are about.
    base = dict(
        max_turns=12,
        enable_llm_verifier=False,
        enable_acceptance_contract=False,
        require_stable_verification=False,
        require_discriminating_evidence=True,
        enable_side_effect_sweep=True,
    )
    base.update(kw)
    return AgentConfig(**base)


def _feedback(result):
    return "\n".join(
        m.content or "" for m in result.messages if m.role == Role.TOOL and m.name == "task_complete"
    )


# --- the tensor-parallel failure: py_compile as "verification" -------------


async def test_syntax_check_alone_cannot_finish_the_run(tmp_path: Path):
    (tmp_path / "solution.py").write_text("def f():\n    return 1\n")
    model = ScriptModel(
        responses=[
            _complete([f"python3 -m py_compile {tmp_path}/solution.py"]),
            _complete([f"python3 {tmp_path}/solution.py"], call_id="tc2"),
        ]
    )
    result = await DefaultAgent().run(
        task="Implement solution.py",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(),
    )
    assert result.success is True  # the second, real check was accepted
    assert "none of your verification commands can fail" in _feedback(result)


async def test_run_fails_when_the_agent_never_supplies_real_evidence(tmp_path: Path):
    """Repeatedly resubmitting weak evidence must not eventually be accepted."""
    weak = [_complete([f"cat {tmp_path}/out.txt"], call_id=f"tc{i}") for i in range(6)]
    (tmp_path / "out.txt").write_text("42")
    result = await DefaultAgent().run(
        task="Produce out.txt",
        model=ScriptModel(responses=weak),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(max_turns=6),
    )
    assert result.success is False


# --- the reject-then-redeclare bypass --------------------------------------


async def test_dropping_the_failing_check_does_not_get_past_the_gate(tmp_path: Path):
    (tmp_path / "out.txt").write_text("wrong")
    model = ScriptModel(
        responses=[
            # First attempt: a real check that fails.
            _complete(["false"], call_id="tc1"),
            # Second: silently drop it — the exact bypass seen in the traces.
            _complete([], call_id="tc2"),
            # Third: weaker still.
            _complete([f"cat {tmp_path}/out.txt"], call_id="tc3"),
        ]
    )
    result = await DefaultAgent().run(
        task="Produce out.txt",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(max_turns=5),
    )
    assert result.success is False
    feedback = _feedback(result)
    assert "no new evidence" in feedback


# --- the escape-room failure: a server left holding a port -----------------


async def test_background_process_is_swept_before_verification(tmp_path: Path):
    script = tmp_path / "server.py"
    # Writes its own pid, so the assertion below can be about the process itself
    # rather than about what the cleanup metadata claims. A sweep that reports a
    # kill it did not perform is the failure mode this test exists to catch, and
    # `processes_killed` alone cannot tell the two apart.
    pidfile = tmp_path / "server.pid"
    script.write_text(
        f"import os, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        f"time.sleep(120)\n"
    )
    marker = tmp_path / "answer.txt"
    marker.write_text("ANSWER")
    # Redirecting output is what a well-behaved launch looks like; the tool
    # returns promptly and the process survives, exactly as in the escape-room
    # trace that cost a point.
    model = ScriptModel(
        responses=[
            _bash(f"python3 {script} > /dev/null 2>&1 &"),
            _complete([f"grep -q ANSWER {marker}"]),
        ]
    )
    result = await DefaultAgent().run(
        task="Solve the puzzle and write answer.txt",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(),
    )
    assert result.success is True
    effects = result.metadata["side_effects"]
    assert any("server.py" in p for p in effects["processes_started"])
    assert effects["processes_killed"], "the process the agent started must not outlive the run"
    assert not effects["processes_surviving"]
    swept = [e for e in result.metadata["events"] if e["type"] == EventType.SIDE_EFFECTS.value]
    assert swept

    # The claim, checked against the kernel rather than against the report.
    assert pidfile.exists(), "the launch never ran, so the sweep proved nothing"
    pid = int(pidfile.read_text().strip())
    assert not _alive(pid), f"pid {pid} outlived the run despite a reported sweep"


def _alive(pid: int) -> bool:
    """Whether ``pid`` still exists. Signal 0 checks without delivering anything."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


async def test_a_launch_whose_argv_does_not_match_the_command_is_still_swept(tmp_path: Path):
    """The sweep cannot rely on the process looking like the command that made it.

    macOS `/usr/bin/python3` re-execs as `.../Python.app/Contents/MacOS/Python`,
    so a `pgrep -f "python3 server.py"` pattern derived from the command text
    matches nothing and the server is left holding its port while the run reports
    a clean workspace. Here the process renames itself outright, which is the
    same failure with the platform dependency removed.
    """
    script = tmp_path / "server.py"
    pidfile = tmp_path / "server.pid"
    # setproctitle isn't a dependency, so rewrite argv the portable way: re-exec
    # the interpreter under a name that shares nothing with the command. The
    # guard is an env var, not argv[0] — Python rewrites sys.argv[0] to the
    # script path, so re-reading it would loop forever.
    disguise = tmp_path / "totally-unrelated-daemon"
    # The pid is written before the re-exec and survives it unchanged, so the
    # guard below cannot be satisfied by the sweep killing the process during the
    # brief window in which it still looks like the command that launched it.
    script.write_text(
        "import os, sys, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "if not os.environ.get('GARUDA_TEST_DISGUISED'):\n"
        "    os.environ['GARUDA_TEST_DISGUISED'] = '1'\n"
        f"    os.execve(sys.executable, [{str(disguise)!r}, __file__], os.environ)\n"
        "time.sleep(120)\n"
    )
    marker = tmp_path / "answer.txt"
    marker.write_text("ANSWER")
    result = await DefaultAgent().run(
        task="Solve the puzzle and write answer.txt",
        model=ScriptModel(
            responses=[
                _bash(f"python3 {script} > /dev/null 2>&1 &"),
                _complete([f"grep -q ANSWER {marker}"]),
            ]
        ),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(),
    )
    assert result.success is True
    assert pidfile.exists(), "the disguised launch never ran, so the sweep proved nothing"
    pid = int(pidfile.read_text().strip())
    assert not _alive(pid), (
        f"pid {pid} renamed itself and outlived the sweep — the process group, "
        "not the command text, has to be the handle"
    )


# --- acceptance criteria ---------------------------------------------------


_EXTRACTION = ModelResponse(
    content=(
        '{"criteria": [{"text": "Output file must be named out.txt.lz77", '
        '"kind": "path", "specified": true, "check": ""}]}'
    ),
    tool_calls=[],
)


async def test_unresolved_criteria_block_completion(tmp_path: Path):
    """A requirement the agent never addressed must stop the run finishing."""
    (tmp_path / "out.txt").write_text("hi")
    check = f"grep -q hi {tmp_path}/out.txt"
    model = ScriptModel(
        responses=[
            _complete([check], call_id="tc1"),  # triggers lazy extraction, then rejected
            _EXTRACTION,
            _complete([check, "test -d ."], call_id="tc2"),  # still unresolved
        ]
    )
    result = await DefaultAgent().run(
        task="Write out.txt.lz77",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(max_turns=6, enable_acceptance_contract=True),
    )
    assert result.success is False
    feedback = _feedback(result)
    assert "acceptance criterion" in feedback
    assert "out.txt.lz77" in feedback
    assert result.metadata["acceptance"]["total"] == 1


async def test_resolving_criteria_lets_the_run_finish(tmp_path: Path):
    (tmp_path / "out.txt").write_text("hi")
    check = f"grep -q hi {tmp_path}/out.txt"
    model = ScriptModel(
        responses=[
            _complete([check], call_id="tc1"),
            _EXTRACTION,
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="ct",
                        name="contract",
                        arguments={
                            "action": "mark",
                            "id": "c1",
                            "status": "verified",
                            "note": "ls showed out.txt.lz77 present with the right suffix",
                        },
                    )
                ],
            ),
            _complete([check, "test -d ."], call_id="tc2"),
        ]
    )
    result = await DefaultAgent().run(
        task="Write out.txt.lz77",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(max_turns=8, enable_acceptance_contract=True),
    )
    assert result.success is True
    assert result.metadata["acceptance"]["verified"] == 1


async def test_contract_tool_is_available_to_the_agent():
    names = {t.name for t in default_tools()}
    assert "contract" in names


# --- budget ----------------------------------------------------------------


async def test_final_turn_is_reserved_for_committing_an_answer(tmp_path: Path):
    """Hitting the cap must not end the run with nothing, as decode-go-ctf did."""
    responses = [_bash("echo working", call_id=f"b{i}") for i in range(20)]
    result = await DefaultAgent().run(
        task="An unsolvable investigation",
        model=ScriptModel(responses=responses),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(max_turns=4),
    )
    assert result.success is False
    nudges = [
        m.content for m in result.messages if m.role == Role.USER and "FINAL turn" in (m.content or "")
    ]
    assert nudges, "the agent must be told to commit an answer before the run ends"
    budget_events = [e for e in result.metadata["events"] if e["type"] == EventType.BUDGET.value]
    assert any(e["payload"].get("stage") == "final_turn" for e in budget_events)


async def test_budget_review_fires_partway_through(tmp_path: Path):
    responses = [_bash("echo x", call_id=f"b{i}") for i in range(20)]
    result = await DefaultAgent().run(
        task="A long task",
        model=ScriptModel(responses=responses),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(max_turns=10),
    )
    reviews = [
        m.content for m in result.messages if m.role == Role.USER and "What have you established" in (m.content or "")
    ]
    assert len(reviews) >= 2, "a stalled run must be made to re-examine its approach"


# --- diagnostics -----------------------------------------------------------


async def test_result_reports_repetition_and_cleanup(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    read = ModelResponse(
        content=None,
        tool_calls=[ToolCall(id="r", name="read_file", arguments={"path": str(tmp_path / "a.txt")})],
    )
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=[read, read, _complete(["test -d ."])]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=_config(),
    )
    assert result.metadata["action_memo"]["repeat_calls"] >= 1
    assert result.metadata["action_memo"]["cache_hits"] >= 1
    assert "side_effects" in result.metadata
