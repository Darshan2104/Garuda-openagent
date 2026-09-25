"""Vendor adapter tests for issue #32 (P1.2).

Shipped manifests are valid, credential-free, and build adapters that pass
the common suite; setup guidance names the login flow and the paths Garuda
never touches. Real-harness smoke tests are opt-in (GARUDA_LIVE_HARNESS) and
handshake-only, so CI never consumes a subscription.
"""

import os
import sys

import pytest

from garuda.acp.catalog import adapter_for_manifest, builtin_manifest_dicts, discover
from garuda.runtime.protocol import AuthStatus
from garuda.runtime.registry import parse_global_manifests
from tests.test_runtime_conformance import run_conformance_suite

CREDENTIAL_PATHS = (".credentials.json", "auth.json", "Keychain", "API_KEY", "token")
PRIVATE_HINTS = ("http://", "https://", "localhost")


def _manifests():
    return parse_global_manifests(builtin_manifest_dicts())


def test_builtin_manifests_parse_and_cover_both_vendors():
    manifests = _manifests()
    assert {m.runtime_id for m in manifests} == {"claude", "codex"}
    for manifest in manifests:
        assert manifest.kind.value == "acp"
        assert manifest.command and all(
            " " not in part and "http" not in part for part in manifest.command
        )
        assert manifest.setup
        assert "never reads" in manifest.setup


def test_no_private_endpoints_or_credential_use():
    for manifest in _manifests():
        blob = " ".join(manifest.command) + manifest.setup + manifest.description
        assert not any(hint in blob for hint in PRIVATE_HINTS)
        assert "login" in manifest.setup
    for path in CREDENTIAL_PATHS:
        assert path not in " ".join(
            " ".join(m.command) for m in _manifests()
        ), f"credential reference {path} must not be a launch argument"


def test_setup_names_untouchable_credential_paths():
    setups = " ".join(m.setup for m in _manifests())
    assert "~/.claude/.credentials.json" in setups
    assert "~/.codex/auth.json" in setups


async def test_vendor_adapters_pass_the_common_suite_with_strict_v1_fixture():
    """This fixture rejects the former private framing/version/session/prompt
    shapes, so the common suite is protocol-compatibility evidence rather than
    Garuda talking to its old fake dialect."""
    for manifest in _manifests():
        adapter = adapter_for_manifest(
            manifest,
            argv_override=[sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "strict-v1"],
        )
        assert adapter.runtime_id == manifest.runtime_id
        await run_conformance_suite(lambda adapter=adapter: adapter)


async def test_discovery_lists_vendors_without_probes():
    found = {d.runtime_id: d for d in discover(_manifests())}
    for vendor in ("claude", "codex"):
        assert found[vendor].available is (found[vendor].executable is not None)
        assert found[vendor].auth is AuthStatus.UNKNOWN


@pytest.mark.skipif(
    not os.environ.get("GARUDA_LIVE_HARNESS"), reason="opt-in live harness smoke test"
)
async def test_live_harness_handshake_only():
    """Handshake against a real installed harness. No prompt, no spend."""
    wanted = os.environ["GARUDA_LIVE_HARNESS"]
    manifests = {m.runtime_id: m for m in _manifests()}
    assert wanted in manifests, f"unknown harness {wanted}"
    adapter = adapter_for_manifest(manifests[wanted])
    await adapter.start(task="handshake probe", session_id="live-probe")
    try:
        assert adapter.native_session_id
        assert adapter.authority is not None
    finally:
        await adapter.close()
