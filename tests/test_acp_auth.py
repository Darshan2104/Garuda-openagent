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
    # Anchored to the repo root so the gate does not depend on pytest's CWD.
    # Recursive: a shallow top-level glob would miss nested packages such as
    # garuda/acp/builtin/ or any future garuda/acp/vendors/ subtree and then
    # overclaim ("zero matches in whatever we happened to open").
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    roots = [repo_root / "garuda" / "acp", repo_root / "garuda" / "runtime"]
    scanned: list[pathlib.Path] = []
    for root in roots:
        scanned.extend(sorted(p for p in root.rglob("*.py") if p.is_file()))
    # Set-checked: assert what we scanned, not only what we found. A passing
    # test must prove it looked at the production files, including nested ones.
    scanned_names = {p.name for p in scanned}
    for required in ("adapter.py", "catalog.py", "authority.py", "registry.py"):
        assert required in scanned_names, (
            f"credential scan missed required file {required!r}; "
            f"scanned {len(scanned)} files"
        )
    assert len(scanned) >= 10, (
        f"credential scan covered too few files ({len(scanned)}); "
        "the glob is probably shallow again"
    )
    # Fail-closed on nested subtrees: if a nested package exists, it must have
    # been scanned. Today builtin/ holds only JSON, but a future helper there
    # must not slip past this gate.
    nested = [p for p in scanned if "builtin" in p.parts or "vendors" in p.parts]
    # (No assertion on nested count yet — the gate is that rglob would find
    # them. The required-files + minimum-count checks above catch a revert to
    # a shallow glob.)
    markers = (".credentials.json", "credentials.json", "/auth.json", "Keychain")
    hits = []
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker in text:
                rel = path.relative_to(repo_root)
                hits.append(f"{rel}:{marker}")
    assert hits == [], f"credential-store references in code: {hits}"


def test_no_oauth_token_helpers():
    """Fail-closed: no helper that reads, copies, or proxies OAuth tokens.

    Would break if a token-path helper (token_path, get_token, read_token,
    credential_path, find-generic-password, etc.) is added anywhere under
    garuda/acp/ or garuda/runtime/. Intentional 'never reads tokens'
    docstrings do not match these helper patterns.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    roots = [repo_root / "garuda" / "acp", repo_root / "garuda" / "runtime"]
    helper_markers = (
        "token_path",
        "credential_path",
        "get_token",
        "read_token",
        "load_token",
        "copy_token",
        "proxy_token",
        "find-generic-password",
        "OAuth",
        "oauth",
    )
    hits = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for marker in helper_markers:
                if marker in text:
                    rel = path.relative_to(repo_root)
                    hits.append(f"{rel}:{marker}")
    assert hits == [], f"OAuth/token helpers in code: {hits}"


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
