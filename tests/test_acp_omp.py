"""oh-my-pi (`omp`) as a distinct ACP runtime (#380).

The shipped manifest rests on a no-prompt conformance spike against omp
18.16.0 (docs/development/omp-acp-conformance.md). CI never needs omp: a shim
named `omp` answers the declared version probe and, for `omp acp`, replays the
recorded handshake, so the production discover-then-launch path runs against
omp's exact wire shapes. A live handshake stays opt-in through
``GARUDA_LIVE_HARNESS=omp`` (tests/test_acp_vendors.py).
"""

import json
import sys
from pathlib import Path

import pytest

from garuda.acp.catalog import adapter_for_discovered, builtin_manifest_dicts, discover
from garuda.runtime import LifecycleState
from garuda.runtime.registry import parse_global_manifests

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "omp"
CAPTURE = FIXTURES / "omp-18.16.0-no-prompt.json"


def _by_id():
    return {m.runtime_id: m for m in parse_global_manifests(builtin_manifest_dicts())}


def _omp_shim(tmp_path, monkeypatch) -> Path:
    """An executable `omp` on PATH: `--version` prints omp's banner, `acp` replays."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "methods.log"
    shim = bin_dir / "omp"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "omp/18.16.0"; exit 0; fi\n'
        'if [ "$1" = "acp" ]; then\n'
        f'  exec "{sys.executable}" -I "{FIXTURES / "replay_agent.py"}" "{CAPTURE}" "{log}"\n'
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return log


def test_omp_is_its_own_acp_runtime_and_pi_is_unchanged():
    by_id = _by_id()
    omp, pi = by_id["omp"], by_id["pi"]
    assert omp.kind.value == "acp"
    assert omp.command == ("omp", "acp")
    assert omp.version_args == ("omp", "--version")
    assert omp.login.flow == "user-cli"
    assert omp.auth_probe is None  # no status command was proved token-free
    # Pi still launches its own registry adapter; omp is not an alias of it.
    assert pi.command == ("pi-acp",)
    assert pi.version_args == ("pi-acp", "--version")
    # The unexercised subset is stated, not implied.
    warning = " ".join(omp.warnings)
    for untested in ("prompted turns", "permission requests", "session/load", "usage"):
        assert untested in warning
    assert "18.16.0" in omp.setup


def test_omp_is_never_an_inference_model_or_a_proven_option_target(tmp_path, monkeypatch):
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.runtime.registry import RegistryError
    from garuda.runtime.resume import PROVEN_LOAD
    from garuda.runtime.roles import MODE_AUTHORITY, PROVEN_OPTIONS

    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "settings.yaml"))
    catalog = prepare_runtime_catalog(tmp_path)
    assert catalog.select("omp").command == ("omp", "acp")
    with pytest.raises(RegistryError, match="not launchable"):
        catalog.select_for_native_facade("omp")
    # Mode/option selection and cross-process resume were not exercised.
    assert not any(runtime == "omp" for runtime, _ in PROVEN_OPTIONS)
    assert "omp" not in MODE_AUTHORITY
    assert "omp" not in PROVEN_LOAD


def test_discovery_reads_the_omp_version_banner(tmp_path, monkeypatch):
    _omp_shim(tmp_path, monkeypatch)
    (record,) = [d for d in discover([_by_id()["omp"]]) if d.runtime_id == "omp"]
    assert record.available is True
    assert record.executable == str(tmp_path / "bin" / "omp")
    assert record.version == "18.16.0"
    assert not any("generic adapter" in w for w in record.warnings)


async def test_recorded_omp_handshake_runs_the_production_no_prompt_lifecycle(
    tmp_path, monkeypatch
):
    """Discover, launch the discovered path, start, cancel while idle, close.

    The replay answers with omp's captured results; the method log proves the
    lifecycle sent no prompt, no authenticate and no option change.
    """
    log = _omp_shim(tmp_path, monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manifest = _by_id()["omp"]
    (record,) = [d for d in discover([manifest]) if d.runtime_id == "omp"]
    adapter = adapter_for_discovered(manifest, record, cwd=str(workspace))
    try:
        await adapter.start(task="no-prompt handshake", session_id="omp-replay")
        capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
        assert adapter.state is LifecycleState.IDLE
        assert adapter.native_session_id == capture["session_new_result"]["sessionId"]
        # A standard v1 agent declares no Garuda mediation fields, so the
        # negotiated map is intent only (see garuda/acp/authority.py).
        assert adapter.authority is not None
        assert adapter.authority.agent_sandbox is False
        await adapter.cancel(reason="idle cancel")
        assert adapter.state is LifecycleState.CLOSED
    finally:
        await adapter.close()
    assert log.read_text(encoding="utf-8").split() == [
        "initialize", "session/new", "session/cancel",
    ]


def test_capture_records_what_the_docs_claim():
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    text = CAPTURE.read_text(encoding="utf-8")
    assert capture["prompt_sent"] is False
    assert "/Users/" not in text and "/home/" not in text
    init = capture["initialize_result"]
    assert init["protocolVersion"] == 1
    assert init["agentInfo"] == {"name": "omp", "title": "omp", "version": "18.16.0"}
    assert [m["id"] for m in init["authMethods"]] == ["agent"]
    caps = init["agentCapabilities"]
    assert caps["loadSession"] is True
    assert set(caps["sessionCapabilities"]) == {"list", "fork", "resume", "close"}
    session = capture["session_new_result"]
    assert [o["id"] for o in session["configOptions"]] == ["mode", "thinking"]
    assert session["modes"]["currentModeId"] == "default"
    assert [m["id"] for m in session["modes"]["availableModes"]] == ["default", "plan"]
    for case in ("stdin_close_after_initialize", "stdin_close_after_session",
                 "sigterm_group_after_session"):
        assert capture["cleanup"][case]["group_after"] == []
    assert capture["cleanup"]["descendants_while_idle"] == []
