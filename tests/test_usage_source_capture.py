"""Usage and limit source capture (#155, plan task A.6).

The capture runs only the vendors' documented status interfaces and never sends
a prompt. CI checks the normalization and redaction, that the committed fixture
says what the docs claim, and — against a fake ``codex app-server`` that logs
every method it receives — that the token-returning method is never called.
"""

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "usage" / "usage-sources.json"


@pytest.fixture(scope="module")
def capture():
    spec = importlib.util.spec_from_file_location(
        "capture_usage_sources", ROOT / "scripts" / "capture_usage_sources.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_claude_auth_status_keeps_bindable_fields_and_drops_identity(capture):
    raw = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "email": "someone@example.com",
        "orgId": "org-123",
        "orgName": "Example Org",
        "subscriptionType": "max",
        "configDirectory": "/home/someone/.claude",
    }
    out = capture.normalize_claude_auth(raw)
    text = json.dumps(out)
    assert out["logged_in"] is True and out["subscription_type"] == "max"
    assert out["account_binding"].startswith("sha256:")
    assert "someone" not in text and "Example Org" not in text and "org-123" not in text


def test_codex_rate_limits_become_windows(capture):
    raw = {
        "accountId": "acct-1",
        "ordinaryUsageAllowed": True,
        "rateLimits": {
            "limitId": "codex",
            "planType": "plus",
            "primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 100},
            "secondary": {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": 200},
            "rateLimitReachedType": None,
        },
    }
    out = capture.normalize_codex_rate_limits(raw)
    assert out["windows"] == [
        {"name": "primary", "used_fraction": 0.4, "window_minutes": 300, "resets_at_epoch_s": 100},
        {"name": "secondary", "used_fraction": 0.05, "window_minutes": 10080, "resets_at_epoch_s": 200},
    ]
    assert "acct-1" not in json.dumps(out)


def test_the_app_server_capture_never_asks_for_a_token(capture, tmp_path):
    log = tmp_path / "methods.log"
    fake = tmp_path / "codex"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"log = open({str(log)!r}, 'a')\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    log.write(msg.get('method', '') + '\\n'); log.flush()\n"
        "    if 'id' in msg:\n"
        "        result = {'rateLimits': {'primary': {'usedPercent': 1, 'windowDurationMins': 300, 'resetsAt': 1}}}\n"
        "        print(json.dumps({'id': msg['id'], 'result': result}), flush=True)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    out = capture._codex_app_server(str(fake))

    methods = log.read_text().split()
    assert methods == ["initialize", "initialized", "account/read", "account/rateLimits/read"]
    assert "getAuthStatus" not in methods
    assert out["rate_limits"]["status"] == "supported"


def test_the_committed_fixture_is_redacted_and_promptless():
    text = FIXTURE.read_text()
    data = json.loads(text)
    assert data["prompt_sent"] is False and data["credentials_read"] is False
    assert "@" not in text and "/Users/" not in text and "/home/" not in text


def test_the_fixture_matches_the_documented_sources():
    data = json.loads(FIXTURE.read_text())["harnesses"]
    claude, codex = data["claude"], data["codex"]

    assert claude["auth_status"]["status"] == "supported"
    assert claude["auth_status"]["account_binding"].startswith("sha256:")
    assert claude["usage_windows"]["status"] == "unknown"

    assert codex["login_status"]["logged_in"] is True
    limits = codex["rate_limits"]
    assert limits["status"] == "supported"
    assert [w["window_minutes"] for w in limits["windows"]] == [300, 10080]
    for window in limits["windows"]:
        assert 0 <= window["used_fraction"] <= 1 and window["resets_at_epoch_s"] > 0
    # Both reads bind to the same (salted) account digest.
    assert limits["account_binding"] == codex["account"]["account_binding"]
