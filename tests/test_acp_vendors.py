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
    adapter_for_registry,
    builtin_manifest_dicts,
    discover,
    require_acp_argv,
    shared_registry,
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
    assert {m.runtime_id for m in manifests} == {
        "claude",
        "codex",
        "cursor",
        "opencode",
        "pi",
        "goose",
    }
    for manifest in manifests:
        assert manifest.kind.value == "acp"
        assert manifest.command and all(
            " " not in part and "http" not in part for part in manifest.command
        )
        assert manifest.setup
        assert "never reads" in manifest.setup
    cursor = next(manifest for manifest in manifests if manifest.runtime_id == "cursor")
    assert cursor.command == ("agent", "acp")
    assert cursor.version_args == ("agent", "--version")


def test_no_private_endpoints_or_credential_use():
    for manifest in _manifests():
        blob = " ".join(manifest.command) + manifest.setup + manifest.description
        assert not any(hint in blob for hint in PRIVATE_HINTS)
        lowered = manifest.setup.lower()
        assert "login" in lowered or "log in" in lowered
    for path in CREDENTIAL_PATHS:
        assert path not in " ".join(
            " ".join(m.command) for m in _manifests()
        ), f"credential reference {path} must not be a launch argument"


def test_setup_names_untouchable_credential_paths():
    setups = " ".join(m.setup for m in _manifests())
    assert "~/.claude/.credentials.json" in setups
    assert "~/.codex/auth.json" in setups
    by_id = {m.runtime_id: m for m in _manifests()}
    assert "~/.local/bin/agent" in by_id["cursor"].setup
    assert "~/" not in by_id["opencode"].setup, "OpenCode must not invent credential paths"
    for vendor in ("cursor", "opencode"):
        assert by_id[vendor].warnings, f"{vendor} must warn about undeclared mediation"


async def test_non_acp_fallback_is_never_silent_and_binds_the_checked_binary():
    by_id = {m.runtime_id: m for m in _manifests()}
    with pytest.raises(AcpUnavailableError, match="agent"):
        require_acp_argv(by_id["cursor"], executable=None)
    try:
        require_acp_argv(by_id["cursor"], executable=None)
    except AcpUnavailableError as exc:
        assert "Add a global harness manifest" in exc.setup or "Install" in exc.setup
    assert require_acp_argv(by_id["cursor"], executable=sys.executable) == [
        sys.executable,
        "acp",
    ]


def test_factory_launches_the_exact_discovered_path_after_path_changes(monkeypatch):
    """The executable accepted at construction replaces the bare command, so
    a later PATH substitution cannot change what AcpRuntime starts."""
    import garuda.acp.catalog as catalog

    cursor = next(manifest for manifest in _manifests() if manifest.runtime_id == "cursor")
    monkeypatch.setattr(catalog.shutil, "which", lambda _: sys.executable)
    adapter = adapter_for_manifest(cursor)
    assert adapter._argv == [sys.executable, "acp"]
    monkeypatch.setattr(catalog.shutil, "which", lambda _: "/tmp/attacker-agent")
    assert adapter._argv == [sys.executable, "acp"]


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


async def test_generic_adapter_needs_only_configuration():
    """A standard-capability agent onboards by manifest alone: parse, discover,
    build, and pass the common suite with no new code."""
    manifests = parse_global_manifests(
        [
            {
                "runtime_id": "my-agent",
                "kind": "acp",
                "command": ["my-agent", "--stdio"],
                "version": "1.0",
                "capabilities": ["prompt", "cancel"],
                "setup": "Install my-agent.",
            }
        ]
    )
    (manifest,) = manifests
    (found,) = [d for d in discover(manifests) if d.runtime_id == "my-agent"]
    assert found.available is False
    assert any("generic adapter" in w for w in found.warnings)
    adapter = adapter_for_manifest(
        manifest,
        argv_override=[sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"],
    )
    await run_conformance_suite(lambda adapter=adapter: adapter)


async def test_tested_commands_carry_no_generic_warning():
    found = {d.runtime_id: d for d in discover(_manifests())}
    for vendor in ("claude", "codex", "cursor", "opencode", "pi", "goose"):
        assert not any("generic adapter" in w for w in found[vendor].warnings), vendor


def _generic_dict(runtime_id="my-agent"):
    return {
        "runtime_id": runtime_id,
        "kind": "acp",
        "command": ["my-agent", "--stdio"],
        "version": "1.0",
        "capabilities": ["prompt", "cancel"],
        "setup": "Install my-agent.",
    }


async def test_generic_registration_flows_through_the_shared_registry():
    """Production path: builtins + trusted manifests parse once; duplicates,
    disabled ids, and untrusted project entries are refused; resolution
    (the only route to launch) enforces the same boundary."""
    from garuda.runtime import RegistryError

    registry = shared_registry(
        extra_manifests=[_generic_dict()], disabled=frozenset()
    )
    assert registry.get("my-agent").runtime_id == "my-agent"
    assert registry.get("claude").runtime_id == "claude"
    with pytest.raises(RegistryError, match="duplicate"):
        shared_registry(
            extra_manifests=[_generic_dict(runtime_id="claude")],
            disabled=frozenset(),
        )
    disabled_registry = shared_registry(
        extra_manifests=[_generic_dict()], disabled=frozenset({"my-agent"})
    )
    with pytest.raises(RegistryError, match="disabled"):
        disabled_registry.get("my-agent")
    with pytest.raises(RegistryError, match="disabled"):
        adapter_for_registry(disabled_registry, "my-agent")
    with pytest.raises(RegistryError, match="unknown runtime"):
        shared_registry(
            extra_manifests=[_generic_dict()],
            project_refs=[{"alias": "ghost", "runtime_id": "nope"}],
            disabled=frozenset(),
        )
    adapter = adapter_for_registry(
        registry,
        "my-agent",
        argv_override=[sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"],
    )
    await run_conformance_suite(lambda adapter=adapter: adapter)


def test_registry_adapter_uses_the_discovered_executable(monkeypatch):
    """The registry route keeps the checked path; it cannot re-resolve PATH."""
    import garuda.acp.catalog as catalog

    registry = shared_registry(extra_manifests=[_generic_dict()], disabled=frozenset())
    monkeypatch.setattr(catalog.shutil, "which", lambda _: sys.executable)
    adapter = adapter_for_registry(registry, "my-agent")
    assert adapter._argv == [sys.executable, "--stdio"]
    monkeypatch.setattr(catalog.shutil, "which", lambda _: "/tmp/replaced-agent")
    assert adapter._argv == [sys.executable, "--stdio"]
