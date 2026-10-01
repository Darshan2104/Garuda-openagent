"""Consult transport spike (#156, plan task G.1).

The capture is opt-in and ran against real adapters; CI checks that the probe MCP
server behaves, that the capture claims forwarding only on evidence (the fake ACP
agent never starts MCP servers, so it must not be reported as supporting it), and
that the committed captures say what the docs claim.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = sorted((ROOT / "tests" / "fixtures" / "consult").glob("*.json"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_server_answers_and_logs_what_it_was_asked(tmp_path):
    log = tmp_path / "probe.log"
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "consult_probe_mcp_server.py"), str(log)],
        input="".join(json.dumps(r) + "\n" for r in requests),
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    replies = [json.loads(line) for line in out.splitlines()]

    assert replies[0]["result"]["serverInfo"]["name"] == "garuda-consult-probe"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["consult"]
    logged = [json.loads(line)["method"] for line in log.read_text().splitlines()]
    assert logged == ["initialize", "notifications/initialized", "tools/list"]


async def test_forwarding_is_not_claimed_without_evidence():
    capture = _load("capture_consult_transport")
    result = await capture.capture(
        [sys.executable, str(ROOT / "garuda" / "acp" / "fake_agent.py"), "--profile", "success"],
        timeout=20,
        settle=1.0,
    )

    assert result["prompt_sent"] is False
    assert result["gate"]["forwarding"] != "supported"
    assert result["gate"]["permission_provenance"] == "unknown"
    assert result["gate"]["quiescence"] == "unknown"


@pytest.mark.parametrize("path", CAPTURES, ids=lambda p: p.stem)
def test_committed_captures_match_the_documented_gate(path):
    text = path.read_text()
    data = json.loads(text)
    assert data["prompt_sent"] is False
    assert "/Users/" not in text and "/home/" not in text
    assert data["gate"] == {
        "forwarding": "supported",
        "permission_provenance": "unknown",
        "quiescence": "unknown",
    }
    assert {"initialize", "tools/list"} <= set(data["probe_methods"])
    assert "tools/call" not in data["probe_methods"]  # no prompt, so no call
