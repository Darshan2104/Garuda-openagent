"""Auth UX tests for issue #35 (P1.5).

Absent, logged-out, unknown-quota, and delegated-login states: login opens
only the manifest's user-driven flow, quota surfaces only when supplied, and
no code path reads credential stores.
"""

import pathlib
import sys

from garuda.acp.adapter import AcpRuntime
from garuda.acp.catalog import builtin_manifest_dicts, discover, health_of
from garuda.runtime.registry import LoginFlow, parse_global_manifests

FAKE = [sys.executable, "-m", "garuda.acp.fake_agent"]


def test_login_flows_are_user_driven_only():
    for manifest in parse_global_manifests(builtin_manifest_dicts()):
        assert manifest.login.flow in ("user-cli", "api-key")
        assert manifest.login.instructions, manifest.runtime_id
    assert LoginFlow.parse(None) == LoginFlow()
    assert LoginFlow.parse({"flow": "api-key", "instructions": "export it"}).flow == "api-key"
    for bad in (
        {"flow": "browser-sso"},
        {"flow": "user-cli", "instructions": ["x"]},
        {"flow": "user-cli", "bogus": 1},
        ["not-a-mapping"],
    ):
        try:
            LoginFlow.parse(bad, where="t.login")
        except Exception:
            continue
        raise AssertionError(f"login accepted {bad!r}")


def test_absent_and_logged_out_states_guide_without_scanning():
    manifests = parse_global_manifests(builtin_manifest_dicts())
    found = {d.runtime_id: d for d in discover(manifests)}
    for vendor in ("claude", "codex", "cursor", "opencode", "pi", "goose"):
        auth_lines = found[vendor].describe_auth()
        assert any("unknown" in line or "logged out" in line for line in auth_lines)
        assert any("to log in:" in line for line in auth_lines)
        assert any("vendor's policy" in line for line in auth_lines)
        health = health_of(found[vendor])
        assert health["quota"] is None
        assert health["login"]["flow"] == "user-cli"


def test_no_python_reads_credential_stores():
    roots = [pathlib.Path("garuda/acp")]
    hits = []
    for root in roots:
        for path in sorted(root.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for marker in (".credentials.json", "/auth.json", "Keychain"):
                if marker in text:
                    hits.append(f"{path}:{marker}")
    assert hits == [], f"credential-store references in code: {hits}"


async def test_quota_passes_through_only_when_supplied():
    plain = AcpRuntime([*FAKE, "--profile", "success"], runtime_id="q-plain")
    await plain.start(task="t", session_id="q1")
    assert plain.quota is None
    await plain.close()

    quota = '{"remaining": 42, "unit": "credits"}'
    rich = AcpRuntime(
        [*FAKE, "--profile", "success", "--quota-json", quota], runtime_id="q-rich"
    )
    await rich.start(task="t", session_id="q2")
    assert rich.quota == {"remaining": 42, "unit": "credits"}
    await rich.close()
