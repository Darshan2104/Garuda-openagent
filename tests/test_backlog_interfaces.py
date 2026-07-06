"""Review-backlog fixes: profile mode honoring (C3), web SSRF/caps (B), image_read
via env (B), condenser re-summarize guard (#13), buffer path collision (#15)."""

from pathlib import Path

import pytest

from garuda.agents.setup import prepare_agent_run


# --- C3: profile mode is honored unless explicitly overridden ---------------

def _write_profile(tmp_path: Path, name: str, mode: str) -> Path:
    (tmp_path / f"{name}.yaml").write_text(
        f"name: {name}\nmode: {mode}\ntools:\n  - bash\n  - task_complete\n", encoding="utf-8"
    )
    return tmp_path


async def test_profile_mode_honored_when_no_override(tmp_path: Path):
    _write_profile(tmp_path, "myrig", "rigorous")
    _, config, _, _, _, mgr = await prepare_agent_run(
        "myrig", workspace=str(tmp_path), agents_dir=tmp_path, mode=None
    )
    assert config.mode == "rigorous"
    if mgr:
        await mgr.close()


async def test_explicit_mode_overrides_profile(tmp_path: Path):
    _write_profile(tmp_path, "myrig", "rigorous")
    _, config, _, _, _, mgr = await prepare_agent_run(
        "myrig", workspace=str(tmp_path), agents_dir=tmp_path, mode="standard"
    )
    assert config.mode == "standard"
    if mgr:
        await mgr.close()


# --- B: web_fetch SSRF guard + byte cap -------------------------------------

def test_ssrf_blocks_private_and_loopback():
    from garuda.tools.web import _ssrf_error

    assert _ssrf_error("http://127.0.0.1/") is not None
    assert _ssrf_error("http://169.254.169.254/latest/meta-data/") is not None  # cloud metadata
    assert _ssrf_error("http://10.0.0.5/") is not None
    assert _ssrf_error("http://[::1]/") is not None


def test_ssrf_allows_public_ip():
    from garuda.tools.web import _ssrf_error

    assert _ssrf_error("http://8.8.8.8/") is None  # public


async def test_web_fetch_rejects_loopback(tmp_path):
    from garuda.tools.protocol import ToolContext
    from garuda.tools.web import WebFetchTool
    from garuda.workspace.local import LocalEnvironment

    res = await WebFetchTool().execute(
        {"url": "http://127.0.0.1:6379/"}, LocalEnvironment(workspace_root=tmp_path), ToolContext(session_id="s")
    )
    assert res.is_error
    assert "non-public" in res.content


# --- B: image_read reads through the environment ----------------------------

async def test_image_read_uses_env(tmp_path: Path):
    from garuda.tools.image_read import ImageReadTool
    from garuda.tools.protocol import ToolContext
    from garuda.workspace.local import LocalEnvironment

    # A .png file whose bytes are read via `base64` in the env (model=None path).
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"fakepixels" * 10)
    res = await ImageReadTool().execute(
        {"path": "pic.png"}, LocalEnvironment(workspace_root=tmp_path), ToolContext(session_id="s", model=None)
    )
    assert not res.is_error
    assert "Loaded image" in res.content  # base64 read succeeded
    assert res.images and res.images[0].startswith("data:image/png;base64,")  # attached for the model


async def test_image_read_missing_file(tmp_path: Path):
    from garuda.tools.image_read import ImageReadTool
    from garuda.tools.protocol import ToolContext
    from garuda.workspace.local import LocalEnvironment

    res = await ImageReadTool().execute(
        {"path": "nope.png"}, LocalEnvironment(workspace_root=tmp_path), ToolContext(session_id="s", model=None)
    )
    assert res.is_error


# --- #13: condenser doesn't re-summarize without growth ---------------------

async def test_condenser_avoids_resummarize_cliff():
    from garuda.context.condenser import CondenserContext, MicrocompactCondenser
    from garuda.model.script_model import ScriptModel
    from garuda.types import Message, Role

    cond = MicrocompactCondenser()
    # All recent (nothing prunable), high usage, and we "just summarized" at this size.
    msgs = [Message(role=Role.SYSTEM, content="s"), Message(role=Role.USER, content="t")]
    cond._last_summary_len = len(msgs)
    cx = CondenserContext(
        messages=msgs, model=ScriptModel(responses=[]), task="t",
        used_tokens=950, max_context_tokens=1000, proactive_threshold=100, keep_recent_turns=2,
        enable_three_step_summary=False,
    )
    # usage 0.95 is >= critical (0.92) -> allowed; drop below critical to see the guard.
    cx.used_tokens = 800  # 0.80 usage, over microcompact_fraction but below critical
    assert await cond.condense(cx) is None  # guard prevents an immediate re-summarize


# --- #15: distinct buffer ids never collide onto one file -------------------

def test_buffer_path_no_collision(tmp_path: Path):
    from garuda.core.buffer import ToolOutputBuffer

    buf = ToolOutputBuffer(session_id="s", root=tmp_path)
    assert buf._path("a/b") != buf._path("a_b")  # would previously both -> a_b.txt
    assert buf._path("x") == buf._path("x")  # stable for a safe id
