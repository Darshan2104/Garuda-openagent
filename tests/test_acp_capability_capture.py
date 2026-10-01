"""The ACP exercised-capability capture (#152, plan task A.3).

``scripts/capture_acp_capabilities.py`` is opt-in and runs against real adapters
on a developer's machine. CI checks two things without any vendor CLI: the script
behaves against Garuda's fake ACP agent (and never sends a prompt), and the
committed captures are redacted and say what the capability table claims.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "acp"
STATUSES = {"supported", "declared", "unknown"}


def _script():
    spec = importlib.util.spec_from_file_location(
        "capture_acp_capabilities", ROOT / "scripts" / "capture_acp_capabilities.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_capture_runs_against_the_fake_agent_without_a_prompt():
    capture = _script()
    result = await capture.capture(
        # The file, not `-m`: the agent runs from a scratch cwd with a minimal
        # environment, where `-m` would import whatever garuda is installed.
        [sys.executable, str(ROOT / "garuda" / "acp" / "fake_agent.py"), "--profile", "success"],
        timeout=20,
    )

    assert result["prompt_sent"] is False
    assert set(result["capabilities"].values()) <= STATUSES
    assert result["capabilities"]["mcp_stdio"] == "supported"
    # The fake agent advertises no model/effort options: nothing is invented.
    assert result["capabilities"]["model_selection"] == "unknown"
    assert result["capabilities"]["usage_update"] == "unknown"


def test_redaction_removes_paths_emails_and_tokens():
    capture = _script()
    redacted = capture.redact(
        {"cwd": "/home/alice/work/x", "who": "alice@example.com", "k": "sk-abcdefghijklmnop"},
        {"/home/alice": "~"},
    )
    assert redacted == {"cwd": "~/work/x", "who": "<email>", "k": "<redacted>"}


def _captures():
    return sorted(FIXTURES.glob("*/*.json"))


@pytest.mark.parametrize("path", _captures(), ids=lambda p: p.stem)
def test_committed_captures_are_redacted_and_promptless(path):
    text = path.read_text()
    data = json.loads(text)
    assert data["prompt_sent"] is False
    assert set(data["capabilities"].values()) <= STATUSES
    assert "/Users/" not in text and "/home/" not in text
    assert "garuda-acp-capture-" not in text  # the scratch workspace never leaks


# What docs/guides/external-harnesses.md states for each captured version.
EXPECTED = {
    "claude-0.85.0": {"model": "model", "effort": "effort"},
    "codex-2.1.1": {"model": "model", "effort": "reasoning_effort"},
}


@pytest.mark.parametrize("path", _captures(), ids=lambda p: p.stem)
def test_captures_match_the_documented_capability_table(path):
    data = json.loads(path.read_text())
    expected = EXPECTED[path.stem]
    caps = data["capabilities"]
    options = {o["category"]: o for o in data["session_summary"]["config_options"]}

    assert caps["model_selection"] == "supported"
    assert caps["effort_selection"] == "supported"
    assert caps["close_session"] == "supported"
    assert caps["load_session"] == "declared"
    assert caps["resume_session"] == "declared"
    assert caps["usage_update"] == "unknown"
    assert options["model"]["id"] == expected["model"]
    assert options["thought_level"]["id"] == expected["effort"]
    assert options["model"]["current"] in options["model"]["values"]
