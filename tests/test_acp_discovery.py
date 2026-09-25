"""Discovery tests for issue #31 (P1.1).

Executable presence, version parsing (known and unknown), login-state
probes, user disablement, setup guidance, and the guarantee that discovery
never logs in, installs, or reads tokens.
"""

import os
import stat

import pytest

from garuda.acp.catalog import (
    BUILTIN_STUBS,
    discover,
    health_of,
    load_trusted_disabled,
    load_trusted_runtime_settings,
)
from garuda.agents.setup import prepare_runtime_catalog
from garuda.interfaces.main import build_parser, run_task
from garuda.runtime.protocol import AuthStatus
from garuda.runtime.registry import RegistryError, RuntimeRegistry, parse_global_manifests

GUARD_VAR = "GARUDA_DISCOVERY_PARENT_SECRET"


def _manifest(
    runtime_id="fake",
    command=None,
    version="9.9",
    capabilities=None,
    setup="",
    version_args=None,
    version_pattern="",
    auth_probe=None,
):
    item = {
        "runtime_id": runtime_id,
        "kind": "acp",
        "command": command or ["fake-acp"],
        "version": version,
        "capabilities": capabilities or ["prompt"],
        "setup": setup,
    }
    if version_args is not None:
        item["version_args"] = version_args
    if version_pattern:
        item["version_pattern"] = version_pattern
    if auth_probe is not None:
        item["auth_probe"] = auth_probe
    return parse_global_manifests([item])


def _fake_binary(tmp_path, name="fake-acp", version_output="", auth_output="") -> str:
    path = tmp_path / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then printf "%s" {version_output!r}; exit 0; fi\n'
        f'if [ "$1" = "auth" ]; then printf "%s" {auth_output!r}; exit 0; fi\n'
        "exit 0\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def test_missing_executable_explains_setup(tmp_path):
    manifests = _manifest(command=["definitely-not-installed-xyz"], setup="Run the installer.")
    (found,) = [d for d in discover(manifests) if d.runtime_id == "fake"]
    assert found.available is False
    assert found.executable is None
    assert found.auth is AuthStatus.UNKNOWN
    assert any("definitely-not-installed-xyz" in w and "Run the installer." in w for w in found.warnings)


def test_version_probed_and_unknown_shown(tmp_path, monkeypatch):
    binary = _fake_binary(tmp_path, version_output="fake-acp 1.2.3\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""))
    manifests = _manifest(
        command=["fake-acp"],
        version_args=["fake-acp", "--version"],
        version_pattern=r"fake-acp (\d+\.\d+\.\d+)",
    )
    (found,) = [d for d in discover(manifests) if d.runtime_id == "fake"]
    assert found.available is True
    assert found.executable == binary
    assert found.version == "1.2.3"

    _fake_binary(tmp_path, version_output="nonsense output")
    (found,) = [d for d in discover(manifests) if d.runtime_id == "fake"]
    assert found.version == "unknown"


def test_auth_probe_matching_and_unknown(tmp_path, monkeypatch):
    binary = _fake_binary(tmp_path, auth_output="logged in as tester")
    assert binary
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""))
    manifests = _manifest(
        command=["fake-acp"],
        auth_probe={
            "argv": ["fake-acp", "auth"],
            "authenticated_pattern": "logged in",
            "unauthenticated_pattern": "not logged in",
        },
    )
    (found,) = [d for d in discover(manifests) if d.runtime_id == "fake"]
    assert found.auth is AuthStatus.AUTHENTICATED

    _fake_binary(tmp_path, auth_output="something unexpected")
    (found,) = [d for d in discover(manifests) if d.runtime_id == "fake"]
    assert found.auth is AuthStatus.UNKNOWN


def test_disabled_runtimes_skip_probes_entirely():
    calls: list = []
    manifests = _manifest(version_args=["fake-acp", "--version"])
    found = discover(
        manifests, disabled={"fake"}, run_probe=lambda argv, timeout: calls.append(argv)
    )
    (entry,) = [d for d in found if d.runtime_id == "fake"]
    assert entry.available is False
    assert calls == []
    assert any("disabled" in w for w in entry.warnings)


def test_trusted_disablement_flows_from_global_settings(tmp_path, monkeypatch):
    from garuda.runtime.registry import RuntimeRegistry

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("disabled_runtimes:\n  - fake\n", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings_path))
    assert load_trusted_disabled() == frozenset({"fake"})
    assert load_trusted_disabled({"disabled_runtimes": ["a", "b"]}) == frozenset({"a", "b"})
    assert load_trusted_disabled({}) == frozenset()
    with pytest.raises(ValueError, match="disabled_runtimes"):
        load_trusted_disabled({"disabled_runtimes": "fake"})
    with pytest.raises(ValueError, match="disabled_runtimes"):
        load_trusted_disabled({"disabled_runtimes": ["ok", 7]})

    # The product path: trusted set disables discovery AND selection.
    manifests = _manifest()
    (entry,) = [
        d for d in discover(manifests, disabled=load_trusted_disabled())
        if d.runtime_id == "fake"
    ]
    assert entry.available is False
    registry = RuntimeRegistry(manifests, disabled=load_trusted_disabled())
    with pytest.raises(Exception, match="disabled"):
        registry.get("fake")


def test_malformed_global_runtime_settings_refuse_discovery_and_selection(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("disabled_runtimes: [fake\n", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings_path))

    with pytest.raises(ValueError, match="trusted runtime settings"):
        load_trusted_runtime_settings()
    with pytest.raises(ValueError, match="trusted runtime settings"):
        load_trusted_disabled()
    with pytest.raises(ValueError, match="trusted runtime settings"):
        prepare_runtime_catalog(tmp_path)


def test_project_suggestions_are_recommendation_only():
    manifests = _manifest(command=["definitely-not-installed-xyz"])
    found = discover(manifests, project_disabled={"fake"})
    (entry,) = [d for d in found if d.runtime_id == "fake"]
    # Still discovered normally — the suggestion is a warning, never a block.
    assert any("ignored" in w and "global settings" in w for w in entry.warnings)
    from garuda.runtime.registry import RuntimeRegistry

    registry = RuntimeRegistry(manifests)
    assert registry.get("fake").runtime_id == "fake"

    (stub,) = [d for d in discover([], project_disabled={"claude"}) if d.runtime_id == "claude"]
    assert any("ignored" in w for w in stub.warnings)


def test_stubs_listed_until_configured():
    found = discover([])
    stub_ids = {d.runtime_id for d in found}
    assert {s["runtime_id"] for s in BUILTIN_STUBS} <= stub_ids
    claude = next(d for d in found if d.runtime_id == "claude")
    assert claude.available is False
    assert any("manifest" in w for w in claude.warnings)

    manifests = _manifest(runtime_id="claude", command=["claude"])
    found = discover(manifests)
    claude = next(d for d in found if d.runtime_id == "claude")
    assert "Add a global harness manifest" not in " ".join(claude.warnings)


def test_disabled_builtin_stub_stays_visible_with_its_policy_annotation():
    claude = next(entry for entry in discover([], disabled={"claude"}) if entry.runtime_id == "claude")
    assert claude.available is False
    assert any("disabled by user configuration" in warning for warning in claude.warnings)


def _write_runtime_settings(workspace, *, disabled=True):
    global_settings = workspace / "global-settings.yaml"
    global_settings.write_text(
        "runtimes:\n"
        "  - runtime_id: fake\n"
        "    kind: acp\n"
        "    command: [fake-acp]\n"
        "    version: '1'\n"
        + ("disabled_runtimes: [fake]\n" if disabled else ""),
        encoding="utf-8",
    )
    agent = workspace / ".agent"
    agent.mkdir()
    (agent / "settings.yaml").write_text(
        "runtime_refs:\n  - alias: preferred\n    runtime_id: fake\n"
        "disabled_runtimes: [native]\n",
        encoding="utf-8",
    )
    return global_settings


def test_shared_catalog_keeps_project_disablement_advisory_and_blocks_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(_write_runtime_settings(tmp_path)))
    catalog = prepare_runtime_catalog(tmp_path)
    fake = next(entry for entry in catalog.discovered if entry.runtime_id == "fake")
    assert fake.available is False
    assert any("disabled by user configuration" in warning for warning in fake.warnings)
    # The project's attempt to disable native remains a suggestion only.
    assert catalog.select("native").runtime_id == "native"
    with pytest.raises(RegistryError, match="disabled"):
        catalog.select("preferred")


@pytest.mark.asyncio
async def test_cli_and_sdk_start_gate_block_globally_disabled_alias(tmp_path, monkeypatch):
    """Both public launch surfaces select through the shared trusted catalog
    before tool construction, model calls, or workspace startup."""
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(_write_runtime_settings(tmp_path)))

    args = build_parser().parse_args(
        ["run", "--task", "must not run", "--workspace", str(tmp_path), "--runtime", "preferred"]
    )
    with pytest.raises(RegistryError, match="disabled"):
        await run_task(args)

    from garuda.sdk import SoftwareAgent

    sdk = SoftwareAgent(workspace=tmp_path, runtime="preferred")
    with pytest.raises(RegistryError, match="disabled"):
        await sdk.run("must not run")


def test_discovery_reads_no_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv(GUARD_VAR, "must-not-cross")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""))
    sniffer = tmp_path / "bin" / "sniff-acp"
    sniffer.parent.mkdir(parents=True, exist_ok=True)
    sniffer.write_text(
        "#!/bin/sh\nenv\n",
    )
    sniffer.chmod(sniffer.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    manifests = _manifest(
        command=["sniff-acp"],
        version_args=["sniff-acp", "--version"],
        version_pattern=r"GARUDA_DISCOVERY_PARENT_SECRET=(\S+)",
        auth_probe={"argv": ["sniff-acp", "auth"]},
    )
    (found,) = [d for d in discover(manifests) if d.runtime_id == "fake"]
    assert found.available is True
    assert "must-not-cross" not in found.version


def test_native_always_available_and_healthy():
    registry = RuntimeRegistry([])
    found = discover(registry.manifests)
    native = next(d for d in found if d.runtime_id == "native")
    assert native.available is True
    assert native.auth is AuthStatus.AUTHENTICATED
    assert health_of(native)["health"] == "ok"


def test_probe_fields_validated():
    with pytest.raises(Exception, match="version_pattern"):
        _manifest(version_pattern="([invalid")
    with pytest.raises(Exception, match="auth_probe"):
        _manifest(auth_probe={"argv": []})
    with pytest.raises(Exception, match="setup"):
        parse_global_manifests(
            [
                {
                    "runtime_id": "x",
                    "kind": "acp",
                    "command": ["x"],
                    "version": "1",
                    "setup": ["not", "a", "string"],
                }
            ]
        )
