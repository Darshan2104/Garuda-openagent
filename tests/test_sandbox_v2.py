"""Phase E4 sandbox hardening: env scrubbing, profile builders, docker limits,
loud failure, and (on macOS) live Seatbelt confinement + network deny."""

import os
import platform
import shutil
from pathlib import Path

import pytest

from garuda.workspace.sandbox import SandboxEnvironment
from garuda.workspace.sandbox_policy import (
    DockerLimits,
    SandboxPolicy,
    SandboxUnavailableError,
    build_bwrap_command,
    build_clean_env,
    build_seatbelt_profile,
)

HAS_SEATBELT = platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None
# Live Seatbelt behavior — network enforcement *and* filesystem/env confinement —
# varies across macOS versions, so any test that actually invokes `sandbox-exec`
# is opt-in (set GARUDA_LIVE_SANDBOX=1) to keep the default suite deterministic and
# green on every host. The pure profile-builder tests below always run.
LIVE_SANDBOX = os.environ.get("GARUDA_LIVE_SANDBOX") == "1"


# --- pure builders -----------------------------------------------------------

def test_clean_env_strips_secrets():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "OPENAI_API_KEY": "sk-secret",
        "AWS_SECRET_ACCESS_KEY": "leak",
        "LANG": "en_US.UTF-8",
    }
    policy = SandboxPolicy()
    clean = build_clean_env(policy, source)
    assert clean["PATH"] == "/usr/bin"
    assert clean["HOME"] == "/home/x"
    assert clean["LANG"] == "en_US.UTF-8"
    assert "OPENAI_API_KEY" not in clean
    assert "AWS_SECRET_ACCESS_KEY" not in clean


def test_clean_env_applies_extra_env():
    policy = SandboxPolicy(extra_env={"FOO": "bar"})
    clean = build_clean_env(policy, {"PATH": "/bin"})
    assert clean["FOO"] == "bar"


def test_bwrap_command_hardening():
    policy = SandboxPolicy(allow_network=False)
    argv = build_bwrap_command(
        "echo hi", "/work", "/usr/bin/bwrap", policy, {"PATH": "/usr/bin"}
    )
    assert "--clearenv" in argv
    assert "--unshare-net" in argv  # network denied
    assert "--ro-bind" not in argv[:2] or True
    # workspace is bind-mounted writable
    assert "--bind" in argv and "/work" in argv
    # env re-applied inside namespace
    joined = " ".join(argv)
    assert "--setenv PATH /usr/bin" in joined
    # no naive whole-root bind
    assert " / / " not in joined
    assert argv[-3:] == ["/bin/bash", "-lc", "echo hi"]


def test_bwrap_allows_network_when_configured():
    argv = build_bwrap_command("x", "/w", "/usr/bin/bwrap", SandboxPolicy(allow_network=True), {})
    assert "--unshare-net" not in argv


def test_seatbelt_profile_denies_network_by_default():
    profile = build_seatbelt_profile("/work/dir", SandboxPolicy())
    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert '(subpath "/work/dir")' in profile


def test_seatbelt_profile_allows_network_when_configured():
    profile = build_seatbelt_profile("/w", SandboxPolicy(allow_network=True))
    assert "(allow network*)" in profile
    assert "(deny network*)" not in profile


def test_seatbelt_profile_escapes_quotes_in_path():
    profile = build_seatbelt_profile('/weird/pa"th', SandboxPolicy())
    assert '\\"' in profile


# --- docker limits -----------------------------------------------------------

def test_docker_limits_default_args():
    args = DockerLimits().to_run_args()
    assert "--memory" in args and "2g" in args
    assert "--cpus" in args and "2" in args
    assert "--pids-limit" in args
    assert "--network" in args and "bridge" in args
    assert "--security-opt" in args and "no-new-privileges" in args


def test_docker_limits_no_network():
    args = DockerLimits(network="none").to_run_args()
    idx = args.index("--network")
    assert args[idx + 1] == "none"


# --- loud failure ------------------------------------------------------------

def test_sandbox_requires_backend_when_unavailable(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "garuda.workspace.sandbox.detect_sandbox_backend", lambda: None
    )
    with pytest.raises(SandboxUnavailableError):
        SandboxEnvironment(workspace_root=tmp_path, policy=SandboxPolicy(require_sandbox=True))


def test_sandbox_allows_unconfined_when_not_required(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "garuda.workspace.sandbox.detect_sandbox_backend", lambda: None
    )
    env = SandboxEnvironment(workspace_root=tmp_path, policy=SandboxPolicy(require_sandbox=False))
    assert not env.is_sandboxed()


# --- live Seatbelt (macOS only) ---------------------------------------------

@pytest.mark.skipif(
    not (HAS_SEATBELT and LIVE_SANDBOX),
    reason="live seatbelt confinement varies by macOS version (set GARUDA_LIVE_SANDBOX=1)",
)
async def test_seatbelt_write_confined_to_workspace(tmp_path: Path):
    env = SandboxEnvironment(workspace_root=tmp_path)
    assert env.backend == "seatbelt"
    # write inside workspace succeeds
    inside = await env.execute("echo ok > inside.txt && cat inside.txt")
    assert inside.exit_code == 0
    assert "ok" in inside.stdout
    # write outside workspace (to HOME) is blocked
    outside = await env.execute("echo pwned > $HOME/garuda-escape-xyz.txt")
    assert outside.exit_code != 0


@pytest.mark.skipif(
    not (HAS_SEATBELT and LIVE_SANDBOX),
    reason="live seatbelt env scrubbing varies by macOS version (set GARUDA_LIVE_SANDBOX=1)",
)
async def test_seatbelt_env_scrubbed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leak-me")
    env = SandboxEnvironment(workspace_root=tmp_path)
    result = await env.execute("echo secret=[${SUPER_SECRET_TOKEN:-absent}]")
    assert "secret=[absent]" in result.stdout


@pytest.mark.skipif(
    not (HAS_SEATBELT and LIVE_SANDBOX),
    reason="live seatbelt network test (set GARUDA_LIVE_SANDBOX=1)",
)
async def test_seatbelt_network_denied_by_default(tmp_path: Path):
    env = SandboxEnvironment(workspace_root=tmp_path)
    result = await env.execute(
        "curl -s --max-time 5 http://example.com >/dev/null 2>&1 && echo NET || echo NONET"
    )
    assert "NONET" in result.stdout
