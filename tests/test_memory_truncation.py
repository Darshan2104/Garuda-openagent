"""A long AGENTS.md is cut visibly, never silently (#150).

Project memory is capped at ``PROJECT_MEMORY_MAX_CHARS``; the rest used to be
dropped with no log, event or marker, so a long file lost its later rules
without anyone knowing.
"""

import logging
import tracemalloc

import pytest

from garuda.agents.loader import MEMORY_TRUNCATED, PROJECT_MEMORY_MAX_CHARS
from garuda.agents.setup import prepare_agent_run
from garuda.core.events import EventStore, EventType
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.sdk.software_agent import SoftwareAgent
from garuda.types import ToolCall


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "home" / "settings.yaml"))
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))


async def test_a_long_agents_md_is_reported_once_and_marked(tmp_path, caplog):
    (tmp_path / "AGENTS.md").write_text("r" * 9000)

    with caplog.at_level(logging.WARNING, logger="garuda.agents.loader"):
        prepared = await prepare_agent_run("build", workspace=str(tmp_path), model=ScriptModel([]))

    (diagnostic,) = prepared.config.prompt_diagnostics
    assert diagnostic["code"] == MEMORY_TRUNCATED
    assert diagnostic["file"].endswith("AGENTS.md")
    assert diagnostic["cap_chars"] == PROJECT_MEMORY_MAX_CHARS
    assert f"truncated at {PROJECT_MEMORY_MAX_CHARS} characters" in prepared.config.system_prompt
    assert len([r for r in caplog.records if MEMORY_TRUNCATED in r.getMessage()]) == 1


async def test_the_diagnostic_reaches_the_session_start_event(tmp_path):
    (tmp_path / "AGENTS.md").write_text("r" * 9000)
    events = EventStore()
    model = ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="d",
                        name="task_complete",
                        arguments={"summary": "A fully detailed completion summary of the work done."},
                    )
                ],
            )
        ]
    )

    await SoftwareAgent(workspace=str(tmp_path), model=model, agent="explore").run("t", events=events)

    (start,) = [e for e in events.get_all() if e["type"] == EventType.SESSION_START.value]
    assert [d["code"] for d in start["payload"]["diagnostics"]] == [MEMORY_TRUNCATED]


async def test_a_short_agents_md_is_unchanged_and_silent(tmp_path, caplog):
    (tmp_path / "AGENTS.md").write_text("Keep it short.")

    with caplog.at_level(logging.WARNING, logger="garuda.agents.loader"):
        prepared = await prepare_agent_run("build", workspace=str(tmp_path), model=ScriptModel([]))

    assert prepared.config.prompt_diagnostics == []
    assert prepared.config.system_prompt.endswith(
        "\n\n## Project instructions (from AGENTS.md)\nKeep it short."
    )
    assert not [r for r in caplog.records if MEMORY_TRUNCATED in r.getMessage()]


def test_a_huge_file_is_not_read_whole(tmp_path):
    from garuda.agents.loader import _project_memory_block

    (tmp_path / "AGENTS.md").write_text("x" * (50 * 1024 * 1024))
    tracemalloc.start()
    try:
        block = _project_memory_block(tmp_path, [])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(block) < PROJECT_MEMORY_MAX_CHARS + 200
    assert peak < 5 * 1024 * 1024
