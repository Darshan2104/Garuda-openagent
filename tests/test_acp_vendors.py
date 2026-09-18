"""Vendor adapter tests for issue #32 (P1.2).

Shipped manifests are valid, credential-free, and build adapters that pass
the common suite; setup guidance names the login flow and the paths Garuda
never touches. Real-harness smoke tests are opt-in (GARUDA_LIVE_HARNESS) and
handshake-only, so CI never consumes a subscription.
"""

import os
import sys

import pytest

from garuda.acp.catalog import (
    AcpUnavailableError,
    adapter_for_manifest,
    builtin_manifest_dicts,
    discover,
    require_acp_argv,
)
from garuda.runtime import HandoffTransaction, LifecycleState
from garuda.runtime.fake import FakeRuntime, FakeScenario
from garuda.runtime.protocol import AuthStatus
from garuda.runtime.registry import parse_global_manifests
from tests.test_runtime_conformance import run_conformance_suite

CREDENTIAL_PATHS = (".credentials.json", "auth.json", "Keychain", "API_KEY", "token")
PRIVATE_HINTS = ("http://", "https://", "localhost")


def _manifests():
    return parse_global_manifests(builtin_manifest_dicts())


def test_builtin_manifests_parse_and_cover_all_vendors():
    manifests = _manifests()
    assert {m.runtime_id for m in manifests} == {"claude", "codex", "cursor", "opencode"}
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
        assert "login" in manifest.setup or "log in" in manifest.setup
    for path in CREDENTIAL_PATHS:
        assert path not in " ".join(
            " ".join(m.command) for m in _manifests()
        ), f"credential reference {path} must not be a launch argument"


def test_setup_names_untouchable_credential_paths():
    setups = " ".join(m.setup for m in _manifests())
    assert "~/.claude/.credentials.json" in setups
    assert "~/.codex/auth.json" in setups
    by_id = {m.runtime_id: m for m in _manifests()}
    for vendor in ("cursor", "opencode"):
        assert "~/" not in by_id[vendor].setup, f"{vendor} must not invent credential paths"
        assert by_id[vendor].warnings, f"{vendor} must warn about undeclared mediation"


async def test_non_acp_fallback_is_never_silent():
    by_id = {m.runtime_id: m for m in _manifests()}
    with pytest.raises(AcpUnavailableError, match="cursor-agent"):
        require_acp_argv(by_id["cursor"], executable=None)
    try:
        require_acp_argv(by_id["cursor"], executable=None)
    except AcpUnavailableError as exc:
        assert "Add a global harness manifest" in exc.setup or "Install" in exc.setup
    assert require_acp_argv(by_id["cursor"], executable="/usr/bin/cursor-agent") == [
        "cursor-agent",
        "acp",
    ]


async def test_version_incompatibility_is_actionable():
    by_id = {m.runtime_id: m for m in _manifests()}
    adapter = adapter_for_manifest(
        by_id["cursor"],
        argv_override=[sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "version-mismatch"],
    )
    with pytest.raises(Exception, match="Upgrade the adapter"):
        await adapter.start(task="t", session_id="v1")
    await adapter.close()


async def test_switch_and_cancel_meet_the_contract():
    by_id = {m.runtime_id: m for m in _manifests()}
    source = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-source")
    await source.start(task="work", session_id="s")
    target = adapter_for_manifest(
        by_id["opencode"],
        argv_override=[sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"],
    )
    tx = HandoffTransaction(session_id="s")
    await tx.begin(source, checkpoint=lambda: None)
    await tx.start_target(source, target)
    await tx.acknowledge(source, target)
    assert source.state is LifecycleState.CLOSED
    assert target.state is LifecycleState.IDLE
    await target.close()

    source = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-source")
    await source.start(task="work", session_id="s2")
    target = adapter_for_manifest(
        by_id["cursor"],
        argv_override=[sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"],
    )
    tx = HandoffTransaction(session_id="s2")
    await tx.begin(source, checkpoint=lambda: None)
    await tx.start_target(source, target)
    await tx.cancel(source, target, reason="user stopped")
    assert source.state is LifecycleState.IDLE
    assert target.state is LifecycleState.CLOSED


async def test_vendor_adapters_pass_the_common_suite_with_fakes():
    for manifest in _manifests():
        adapter = adapter_for_manifest(
            manifest,
            argv_override=[sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"],
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
