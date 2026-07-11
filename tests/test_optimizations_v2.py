"""Turn-reducing optimizations: environment bootstrap snapshot, wider parallel
read batching, and broadened post-edit diagnostics.

These are cheap, benchmark-agnostic changes whose shared payoff is fewer model
round-trips (lower cost + latency) and fewer dead-end turns.
"""

import shutil
from pathlib import Path

import pytest

from garuda.core.bootstrap import environment_snapshot
from garuda.core.loop import PARALLEL_SAFE_TOOLS, DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.tools.diagnostics import check_syntax
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


# --- environment bootstrap snapshot -----------------------------------------


async def test_snapshot_reports_os_cwd_and_runtimes(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    env = LocalEnvironment(workspace_root=tmp_path)
    snap = await environment_snapshot(env)
    assert "## OS" in snap
    assert "## CWD" in snap
    assert "## Runtimes" in snap
    # The harness runs on Python, so python3 must show up in the probe.
    assert "python" in snap.lower()
    # The project marker we dropped is detected.
    assert "pyproject.toml" in snap


async def test_snapshot_cached_after_first_probe(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    calls = {"n": 0}
    original = env.execute

    async def counting(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    env.execute = counting  # shadow the bound method for this instance
    first = await environment_snapshot(env)
    second = await environment_snapshot(env)
    assert first == second
    assert calls["n"] == 1  # second call served from the per-env cache


async def test_snapshot_best_effort_when_probe_fails():
    class BoomEnv:
        workspace_root = "/nowhere"

        async def execute(self, *args, **kwargs):
            raise RuntimeError("probe blew up")

        async def read_file(self, path):  # pragma: no cover - unused
            raise FileNotFoundError

        async def write_file(self, path, content):  # pragma: no cover - unused
            raise OSError

    snap = await environment_snapshot(BoomEnv())
    assert snap == ""  # never raises; just no snapshot


async def test_bootstrap_injects_snapshot_into_first_system_prompt(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(responses=[ModelResponse(content="nothing to do", tool_calls=[])])
    result = await DefaultAgent().run(
        task="say hi",
        model=model,
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=2, enable_verifier=False, bootstrap_environment=True),
    )
    system = next(m for m in result.messages if m.role == Role.SYSTEM)
    assert "Environment snapshot" in system.content
    assert "## OS" in system.content
    event_types = {e["type"] for e in result.metadata["events"]}
    assert "environment_snapshot" in event_types


async def test_bootstrap_off_keeps_system_prompt_clean(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(responses=[ModelResponse(content="nothing to do", tool_calls=[])])
    result = await DefaultAgent().run(
        task="say hi",
        model=model,
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=2, enable_verifier=False, bootstrap_environment=False),
    )
    system = next(m for m in result.messages if m.role == Role.SYSTEM)
    assert "Environment snapshot" not in system.content
    event_types = {e["type"] for e in result.metadata["events"]}
    assert "environment_snapshot" not in event_types


# --- wider parallel read batching -------------------------------------------


def test_read_only_tools_are_parallel_safe():
    for name in (
        "buffer_grep",
        "buffer_slice",
        "buffer_list",
        "buffer_query",
        "image_read",
    ):
        assert name in PARALLEL_SAFE_TOOLS


async def test_batch_of_readonly_buffer_and_read_runs_and_pairs(tmp_path: Path):
    (tmp_path / "a.txt").write_text("AAA", encoding="utf-8")
    env = LocalEnvironment(workspace_root=tmp_path)
    responses = [
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="r1", name="read_file", arguments={"path": "a.txt"}),
                ToolCall(id="b1", name="buffer_list", arguments={}),
            ],
        ),
        ModelResponse(
            content=None,
            tool_calls=[
                ToolCall(id="done", name="task_complete", arguments={"summary": "read and listed buffers"})
            ],
        ),
    ]
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=responses),
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=5, bootstrap_environment=False),
    )
    assert result.success
    tool_msgs = [m for m in result.messages if m.role == Role.TOOL]
    # Both results present, in call order, paired to their ids.
    assert [m.tool_call_id for m in tool_msgs[:2]] == ["r1", "b1"]
    assert "AAA" in tool_msgs[0].content


# --- broadened post-edit diagnostics ----------------------------------------


async def test_check_syntax_flags_bad_shell(tmp_path: Path):
    bad = tmp_path / "bad.sh"
    bad.write_text('if [ -z "$x" ]; then\n  echo hi\n', encoding="utf-8")  # missing `fi`
    env = LocalEnvironment(workspace_root=tmp_path)
    problem = await check_syntax(env, str(bad))
    assert problem is not None


async def test_check_syntax_passes_good_shell(tmp_path: Path):
    good = tmp_path / "good.sh"
    good.write_text('x=1\nif [ -z "$x" ]; then echo hi; fi\n', encoding="utf-8")
    env = LocalEnvironment(workspace_root=tmp_path)
    assert await check_syntax(env, str(good)) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
async def test_check_syntax_flags_bad_js(tmp_path: Path):
    bad = tmp_path / "bad.js"
    bad.write_text("function f( { return 1 }\n", encoding="utf-8")
    env = LocalEnvironment(workspace_root=tmp_path)
    assert await check_syntax(env, str(bad)) is not None

    good = tmp_path / "good.js"
    good.write_text("function f() { return 1; }\n", encoding="utf-8")
    assert await check_syntax(env, str(good)) is None
