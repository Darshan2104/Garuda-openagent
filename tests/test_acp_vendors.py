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
    DiscoveredRuntime,
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
    cursor = next(manifest for manifest in manifests if manifest.runtime_id == "cursor")
    assert cursor.command == ("agent", "acp")
    assert cursor.version_args == ("agent", "--version")


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
    # The child env is PATH/HOME/LANG only: advising users to export an API
    # key for the adapter would be false guidance.
    assert "set CODEX_API_KEY yourself" not in setups
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


def _shim(directory, name, label, marker):
    """An executable named `name` that records `label` and then speaks ACP."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        f"echo {label} >> {marker}\n"
        f'exec "{sys.executable}" -m garuda.acp.fake_agent --profile success\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


async def test_factory_launches_the_exact_discovered_path_after_path_changes(
    tmp_path, monkeypatch
):
    """Two same-named binaries on different PATHs: discovery under PATH=A, then
    PATH switches to B before launch. The product factory must start A."""
    from garuda.acp.catalog import adapter_for_discovered

    marker = tmp_path / "ran"
    shim_a = _shim(tmp_path / "a", "fake-shim-acp", "A", marker)
    _shim(tmp_path / "b", "fake-shim-acp", "B", marker)
    (manifest,) = parse_global_manifests(
        [{"runtime_id": "shim", "kind": "acp", "command": ["fake-shim-acp"], "version": "1"}]
    )
    base_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(tmp_path / "a") + os.pathsep + base_path)
    (record,) = [d for d in discover([manifest]) if d.runtime_id == "shim"]
    assert record.executable == str(shim_a)

    monkeypatch.setenv("PATH", str(tmp_path / "b") + os.pathsep + base_path)
    adapter = adapter_for_discovered(manifest, record, cwd=str(tmp_path))
    await adapter.start(task="which binary", session_id="path-swap")
    try:
        assert marker.read_text(encoding="utf-8").split() == ["A"]
    finally:
        await adapter.close()


def test_factory_refuses_without_a_discovered_executable(tmp_path, monkeypatch):
    """No silent second PATH lookup: without the discovered path the factory
    refuses, and a directory is never accepted as an executable."""
    from garuda.acp.catalog import adapter_for_discovered

    cursor = next(manifest for manifest in _manifests() if manifest.runtime_id == "cursor")
    with pytest.raises(AcpUnavailableError):
        adapter_for_manifest(cursor)
    with pytest.raises(AcpUnavailableError):
        require_acp_argv(cursor, executable=str(tmp_path))
    (missing,) = parse_global_manifests(
        [{"runtime_id": "gone", "kind": "acp", "command": ["definitely-not-installed-xyz"],
          "version": "1", "setup": "Install gone-acp."}]
    )
    (unavailable,) = [d for d in discover([missing]) if d.runtime_id == "gone"]
    with pytest.raises(AcpUnavailableError, match="Install gone-acp"):
        adapter_for_discovered(missing, unavailable)
    impostor = DiscoveredRuntime(
        runtime_id="gone", kind="acp", available=True, executable=sys.executable
    )
    with pytest.raises(AcpUnavailableError, match="discovery record"):
        adapter_for_discovered(cursor, impostor)


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


def test_discovery_runs_only_declared_version_probes_and_never_guesses_login():
    calls: list[tuple[str, ...]] = []

    def _record(argv, timeout):
        calls.append(tuple(argv))
        return None

    manifests = _manifests()
    found = {d.runtime_id: d for d in discover(manifests, run_probe=_record)}
    declared = {tuple(m.version_args) for m in manifests}
    # Only the manifest's own `--version` argv may run — and only where the
    # executable exists. No login/auth probe is shipped, so none runs.
    assert set(calls) <= declared
    for vendor in ("claude", "codex"):
        entry = found[vendor]
        assert entry.auth is AuthStatus.UNKNOWN
        if entry.executable is None:
            assert entry.available is False
            assert any("npm install -g" in warning for warning in entry.warnings)


def test_shipped_manifests_are_in_the_product_catalog(tmp_path, monkeypatch):
    """`--runtime claude` resolves through the one trusted catalog: it is an ACP
    selection that refuses loudly, not an unknown runtime or a native run."""
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.runtime.registry import RegistryError

    settings = tmp_path / "settings.yaml"
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    catalog = prepare_runtime_catalog(tmp_path)
    for vendor, binary in (("claude", "claude-agent-acp"), ("codex", "codex-acp")):
        selected = catalog.select(vendor)
        assert selected.kind.value == "acp"
        assert selected.command == (binary,)
        with pytest.raises(RegistryError, match="not launchable"):
            catalog.select_for_native_facade(vendor)

    settings.write_text(
        "disabled_runtimes: [claude]\n"
        "runtimes:\n  - runtime_id: codex\n    kind: acp\n"
        "    command: [/opt/custom/codex-acp]\n    version: '2'\n",
        encoding="utf-8",
    )
    catalog = prepare_runtime_catalog(tmp_path)
    with pytest.raises(RegistryError, match="disabled"):
        catalog.select("claude")
    # The user's global entry replaces the shipped manifest of the same id.
    assert catalog.select("codex").command == ("/opt/custom/codex-acp",)


async def test_adapter_sends_the_given_session_root(tmp_path, monkeypatch):
    from garuda.acp.adapter import AcpRuntime
    from garuda.acp.client import AcpProcess
    from garuda.runtime.protocol import RuntimeStartError

    with pytest.raises(RuntimeStartError, match="absolute"):
        AcpRuntime(["x"], cwd="relative/dir")

    seen: list = []
    real_session_new = AcpProcess.session_new

    async def _recording(self, cwd=None, *args, **kwargs):
        seen.append(cwd)
        return await real_session_new(self, cwd, *args, **kwargs)

    monkeypatch.setattr(AcpProcess, "session_new", _recording)
    (manifest,) = [m for m in _manifests() if m.runtime_id == "claude"]
    adapter = adapter_for_manifest(
        manifest,
        argv_override=[sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "strict-v1"],
        cwd=str(tmp_path),
    )
    await adapter.start(task="cwd probe", session_id="cwd-probe")
    try:
        assert seen == [str(tmp_path)]
    finally:
        await adapter.close()


@pytest.mark.skipif(
    not os.environ.get("GARUDA_LIVE_HARNESS"), reason="opt-in live harness smoke test"
)
async def test_live_harness_handshake_only(tmp_path):
    """Handshake against a real installed harness. No prompt, no spend. The
    session root is an empty temp dir, never this repository."""
    wanted = os.environ["GARUDA_LIVE_HARNESS"]
    manifests = {m.runtime_id: m for m in _manifests()}
    assert wanted in manifests, f"unknown harness {wanted}"
    from garuda.acp.catalog import adapter_for_discovered

    (record,) = [d for d in discover([manifests[wanted]]) if d.runtime_id == wanted]
    adapter = adapter_for_discovered(manifests[wanted], record, cwd=str(tmp_path))
    await adapter.start(task="handshake probe", session_id="live-probe")
    try:
        assert adapter.native_session_id
        assert adapter.authority is not None
    finally:
        await adapter.close()
