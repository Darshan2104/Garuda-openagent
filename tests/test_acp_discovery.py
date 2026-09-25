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
    fake = next(entry for entry in catalog.discover() if entry.runtime_id == "fake")
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


def _forbid_startup(monkeypatch):
    """Make any post-selection startup step fail the test loudly."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("startup ran after a refused runtime selection")

    import garuda.interfaces.main as cli_main
    import garuda.sdk.conversation as sdk_conversation
    import garuda.sdk.software_agent as sdk_agent

    monkeypatch.setattr(cli_main, "load_profile", _boom)
    monkeypatch.setattr(cli_main, "build_toolkit", _boom)
    monkeypatch.setattr(cli_main, "LitellmModel", _boom)
    monkeypatch.setattr(sdk_agent, "load_profile", _boom)
    monkeypatch.setattr(sdk_conversation.AgentSession, "create", _boom)
    monkeypatch.setattr(sdk_conversation, "resolve_environment", _boom)


@pytest.mark.asyncio
async def test_enabled_acp_runtime_is_refused_not_silently_native(tmp_path, monkeypatch):
    """A configured, enabled ACP runtime is not launchable by the native facade:
    every launch surface refuses instead of running the native loop."""
    monkeypatch.setenv(
        "GARUDA_GLOBAL_SETTINGS", str(_write_runtime_settings(tmp_path, disabled=False))
    )
    _forbid_startup(monkeypatch)
    for ref in ("fake", "preferred"):
        args = build_parser().parse_args(
            ["run", "--task", "must not run", "--workspace", str(tmp_path), "--runtime", ref]
        )
        with pytest.raises(RegistryError, match="not launchable"):
            await run_task(args)

        from garuda.sdk import SoftwareAgent

        with pytest.raises(RegistryError, match="not launchable"):
            await SoftwareAgent(workspace=tmp_path, runtime=ref).run("must not run")


@pytest.mark.asyncio
async def test_conversation_gate_refuses_before_session_or_environment(tmp_path, monkeypatch):
    from garuda.sdk.conversation import Conversation

    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(_write_runtime_settings(tmp_path)))
    _forbid_startup(monkeypatch)
    with pytest.raises(RegistryError, match="disabled"):
        await Conversation(workspace=tmp_path, runtime="preferred").run("must not run")

    monkeypatch.setenv(
        "GARUDA_GLOBAL_SETTINGS", str(tmp_path / "global-settings.yaml")
    )
    (tmp_path / "global-settings.yaml").write_text(
        "runtimes:\n  - runtime_id: fake\n    kind: acp\n    command: [fake-acp]\n"
        "    version: '1'\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="not launchable"):
        await Conversation(workspace=tmp_path, runtime="fake").run("must not run")


def test_cli_main_refuses_disabled_runtime_with_message_and_no_startup(
    tmp_path, monkeypatch, capsys
):
    """The outermost entry point: `garuda run --runtime <disabled>` exits nonzero
    with an actionable message before any profile, toolkit, or model exists."""
    from garuda.interfaces.main import main

    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(_write_runtime_settings(tmp_path)))
    _forbid_startup(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["garuda", "run", "--task", "x", "--workspace", str(tmp_path), "--runtime", "fake"],
    )
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert "runtime selection refused" in err and "disabled" in err
    assert "Traceback" not in err


def test_launch_path_runs_no_probes_and_probes_get_no_stdin(tmp_path, monkeypatch):
    """Building and selecting through the catalog executes nothing; the probes
    run only when a list/inspect caller asks for discovery."""
    marker = tmp_path / "probed"
    probe = tmp_path / "bin" / "probe-acp"
    probe.parent.mkdir()
    probe.write_text(f"#!/bin/sh\ntouch {marker}\necho 'probe-acp 1.0'\n", encoding="utf-8")
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(probe.parent) + os.pathsep + os.environ.get("PATH", ""))
    settings = tmp_path / "global-settings.yaml"
    settings.write_text(
        "runtimes:\n  - runtime_id: probe\n    kind: acp\n    command: [probe-acp]\n"
        "    version: '1'\n    version_args: [probe-acp, --version]\n"
        "    auth_probe:\n      argv: [probe-acp, auth]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))

    catalog = prepare_runtime_catalog(tmp_path)
    assert catalog.select_for_native_facade("native").runtime_id == "native"
    with pytest.raises(RegistryError, match="not launchable"):
        catalog.select_for_native_facade("probe")
    assert not marker.exists()

    import subprocess

    seen: list = []
    real_run = subprocess.run

    def _recording_run(*args, **kwargs):
        seen.append(kwargs.get("stdin"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _recording_run)
    (entry,) = [d for d in catalog.discover() if d.runtime_id == "probe"]
    assert entry.available is True
    assert marker.exists()
    assert seen and all(value is subprocess.DEVNULL for value in seen)


def test_malformed_project_advice_warns_but_authority_attempts_refuse(tmp_path, monkeypatch):
    settings = tmp_path / "global-settings.yaml"
    settings.write_text(
        "runtimes:\n  - runtime_id: fake\n    kind: acp\n    command: [fake-acp]\n"
        "    version: '1'\n    capabilities: [prompt]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    project = tmp_path / ".agent" / "settings.yaml"
    project.parent.mkdir()

    # Advisory malformations are ignored with a warning: runs still start.
    project.write_text(
        "disabled_runtimes: native\n"
        "runtime_refs:\n"
        "  - alias: ok\n    runtime_id: fake\n"
        "  - alias: ghost\n    runtime_id: nowhere\n"
        "  - alias: typo\n    runtime_id: fake\n    colour: blue\n"
        "  - alias: ok\n    runtime_id: fake\n",
        encoding="utf-8",
    )
    catalog = prepare_runtime_catalog(tmp_path)
    assert catalog.select("native").runtime_id == "native"
    assert catalog.select("ok").runtime_id == "fake"
    joined = " ".join(catalog.warnings)
    assert "disabled_runtimes" in joined
    assert "nowhere" in joined and "colour" in joined and "duplicate" in joined
    with pytest.raises(RegistryError, match="unknown runtime"):
        catalog.select("ghost")

    project.write_text("runtime_refs: not-a-list\n", encoding="utf-8")
    assert "must be a list" in " ".join(prepare_runtime_catalog(tmp_path).warnings)

    # Authority attempts still fail closed.
    project.write_text(
        "runtime_refs:\n  - alias: evil\n    runtime_id: fake\n    command: [sh]\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="cannot authorize an executable"):
        prepare_runtime_catalog(tmp_path)
    project.write_text(
        "runtime_refs:\n  - alias: wide\n    runtime_id: fake\n"
        "    capabilities: [prompt, terminal]\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="widens capabilities"):
        prepare_runtime_catalog(tmp_path)
