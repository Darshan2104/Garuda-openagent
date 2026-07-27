"""Regression tests for the security & correctness backlog (T1–T8).

One test file per fix would scatter these; they are grouped here because they
share a provenance — the 2026-07-27 re-audit recorded in
``docs/archive/2026-07-28-SECURITY_CORRECTNESS_TODO.md`` — and each names the task it closes.

Live OS-sandbox assertions live in the opt-in ``GARUDA_LIVE_SANDBOX`` job, since
Seatbelt behaviour varies by macOS image.
"""

import json
import logging
import os
import platform
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from garuda.interfaces.tui import ChatRenderer
from garuda.mcp.config import load_mcp_config
from garuda.tools import web
from garuda.tools.background import BWRAP_UNSUPPORTED, BashBackgroundTool
from garuda.workspace.sandbox_policy import SandboxPolicy, build_seatbelt_profile

LIVE_SANDBOX = os.environ.get("GARUDA_LIVE_SANDBOX") == "1"


# --- T1: SSRF ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # AWS/GCP metadata
        "http://127.0.0.1:8765/",  # the local `serve` port
        "http://localhost/",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
    ],
)
def test_ssrf_guard_blocks_non_public_targets(url):
    assert web._ssrf_error(url) is not None


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/"])
def test_ssrf_guard_blocks_non_http_schemes(url):
    assert web._ssrf_error(url) is not None


def test_ssrf_guard_fails_closed_on_unresolvable_host():
    """A host that will not resolve is refused, not handed to urlopen.

    Returning None here (the previous behaviour) makes "could not resolve" and
    "resolved to something we would have rejected" indistinguishable.
    """
    error = web._ssrf_error("http://no-such-host-abc123xyz.invalid/")
    assert error is not None
    assert "could not be resolved" in error


class _RedirectServer(BaseHTTPRequestHandler):
    target = "http://169.254.169.254/latest/meta-data/"

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.target)
            self.end_headers()
            return
        body = b"<html><body>plain</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def redirect_server():
    server = HTTPServer(("127.0.0.1", 0), _RedirectServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def _allow_loopback(monkeypatch):
    """Let the test reach its own loopback server, and nothing else.

    Patches ``_resolve_and_vet`` — the single chokepoint both the pre-flight check
    and the pinned connect go through — rather than ``_ssrf_error`` alone. Patching
    only the pre-flight would leave the connect-time guard refusing loopback, which
    is exactly the property that closes DNS rebinding: there is deliberately no way
    to satisfy the pre-flight and then connect somewhere it did not vet.
    """
    real = web._resolve_and_vet

    def vet(host, port=None):
        if host == "127.0.0.1":
            return (None, "127.0.0.1")
        return real(host, port)

    monkeypatch.setattr(web, "_resolve_and_vet", vet)


def test_redirect_to_metadata_is_refused(redirect_server, monkeypatch):
    """The guard must run on every hop, not just the URL the caller passed.

    Without this, an attacker-controlled page need only answer
    ``302 Location: http://169.254.169.254/…`` to have the host-side tool read
    cloud metadata back into the model's context.
    """
    port = redirect_server
    _allow_loopback(monkeypatch)
    error, text = web._blocking_fetch(f"http://127.0.0.1:{port}/redirect", 10_000)
    assert error is not None
    assert "blocked redirect" in error
    assert "169.254.169.254" in error
    assert not text


def test_non_redirect_fetch_still_works(redirect_server, monkeypatch):
    port = redirect_server
    _allow_loopback(monkeypatch)
    error, text = web._blocking_fetch(f"http://127.0.0.1:{port}/plain", 10_000)
    assert error is None
    assert "plain" in text


def test_connect_is_pinned_to_the_vetted_address(redirect_server, monkeypatch):
    """Closing DNS rebinding: satisfying the pre-flight must not be enough.

    The old guard resolved the host and then handed the *hostname* to urlopen,
    which resolved it again — a short-TTL record could answer publicly for the
    check and 169.254.169.254 for the connect. Here the pre-flight is stubbed to
    approve everything; the fetch must still be refused, because the connect does
    its own vetting against the address it actually dials.
    """
    port = redirect_server
    monkeypatch.setattr(web, "_ssrf_error", lambda url: None)
    error, text = web._blocking_fetch(f"http://127.0.0.1:{port}/plain", 10_000)
    assert error is not None, "a rubber-stamped pre-flight must not grant a connection"
    assert "blocked connection" in error
    assert not text


def test_opener_installs_the_validating_redirect_handler():
    handlers = web._build_opener().handlers
    assert any(isinstance(h, web._ValidatingRedirectHandler) for h in handlers)


def test_redirect_hop_cap_is_bounded():
    """A redirect loop must not spin the turn away even when every hop is public."""
    handler = web._ValidatingRedirectHandler()
    assert handler.max_repeats == web.MAX_REDIRECTS
    assert handler.max_redirections == web.MAX_REDIRECTS


# --- T3: Seatbelt signal ----------------------------------------------------


def test_seatbelt_profile_allows_process_group_signals():
    """`(target self)` alone is a no-op for killing children — `pgrp` is required.

    Verified empirically on macOS 25.5: with only `self`, `sleep 30 & kill $!`
    still returns "Operation not permitted".
    """
    profile = build_seatbelt_profile("/tmp/ws", SandboxPolicy())
    assert "(allow signal (target self) (target pgrp))" in profile
    # `others` would let a sandboxed command signal the agent itself.
    assert "(target others)" not in profile


def test_seatbelt_profile_allows_terminal_ioctl():
    assert "(allow file-ioctl)" in build_seatbelt_profile("/tmp/ws", SandboxPolicy())


@pytest.mark.skipif(
    not LIVE_SANDBOX or platform.system() != "Darwin",
    reason="live Seatbelt test; set GARUDA_LIVE_SANDBOX=1 on macOS",
)
def test_live_seatbelt_can_kill_child_but_not_host():
    import subprocess

    workdir = os.path.realpath(tempfile.mkdtemp())
    profile = build_seatbelt_profile(
        workdir, SandboxPolicy(writable_paths=["/private/tmp", "/dev"])
    )

    def run(command):
        proc = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "/bin/bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.stdout + proc.stderr

    assert "KILL_OK" in run("sleep 30 & kill $! && echo KILL_OK")
    # PID 1 is outside the sandbox's process group and must stay untouchable.
    assert "CANNOT" in run("kill -TERM 1 2>&1 || echo CANNOT_SIGNAL_HOST")


# --- T4: MCP config fault isolation ----------------------------------------


def _write_config(tmp_path: Path, text: str) -> str:
    path = tmp_path / "mcp.yaml"
    path.write_text(text)
    return str(path)


def test_one_malformed_entry_does_not_drop_the_others(tmp_path):
    path = _write_config(
        tmp_path,
        """servers:
  - name: ok1
    command: echo
  - name: bad
    command: echo
    env: not-a-mapping
  - name: ok2
    command: echo
""",
    )
    names = [c.name for c in load_mcp_config(path)]
    assert names == ["ok1", "ok2"]


def test_args_given_as_a_string_is_not_split_into_characters(tmp_path, caplog):
    """`args: hello` used to become ['h','e','l','l','o'].

    The server then launched with per-character arguments and failed somewhere
    far from the cause.
    """
    path = _write_config(
        tmp_path, "servers:\n  - name: a\n    command: echo\n    args: hello\n"
    )
    with caplog.at_level(logging.WARNING):
        configs = load_mcp_config(path)
    assert [c.args for c in configs] == [["hello"]]
    assert "should be a list" in caplog.text


def test_args_of_a_nonsensical_type_skips_only_that_entry(tmp_path):
    path = _write_config(
        tmp_path,
        """servers:
  - name: numeric_args
    command: echo
    args: 42
  - name: fine
    command: echo
""",
    )
    assert [c.name for c in load_mcp_config(path)] == ["fine"]


@pytest.mark.parametrize(
    "text", ["", "- just\n- a\n- list\n", "servers:\n  - name: a\n   bad: [\n"]
)
def test_unusable_config_yields_no_servers_without_raising(tmp_path, text):
    assert load_mcp_config(_write_config(tmp_path, text)) == []


# --- T5: bash_background under bwrap ---------------------------------------


class _FakeEnv:
    """Minimal Environment stand-in that reports a sandbox backend."""

    workspace_root = "/app"

    def __init__(self, backend=None):
        self.backend = backend
        self.executed: list[str] = []

    async def execute(self, command, timeout=None, cwd=None):  # pragma: no cover
        self.executed.append(command)
        raise AssertionError("bwrap backend must be refused before executing")


class _Ctx:
    session_id = "s1"


async def test_background_refuses_under_bwrap():
    """bwrap would accept the launch, return a namespace-local pid, and reap the
    process — leaving the agent polling a task that never existed."""
    env = _FakeEnv(backend="bwrap")
    result = await BashBackgroundTool().execute({"command": "sleep 5"}, env, _Ctx())
    assert result.is_error
    assert result.content == BWRAP_UNSUPPORTED
    assert env.executed == [], "must refuse before spawning anything"


async def test_background_allowed_on_other_backends(tmp_path):
    """Seatbelt/local/docker keep working — the refusal is bwrap-specific."""
    from garuda.workspace.local import LocalEnvironment

    for backend in (None, "seatbelt"):
        env = LocalEnvironment(workspace_root=tmp_path)
        env.backend = backend
        result = await BashBackgroundTool().execute({"command": "sleep 0.1"}, env, _Ctx())
        assert not result.is_error, result.content
        assert "Started background task" in result.content


# --- T6: chat --json stdout purity -----------------------------------------


def test_renderer_writes_to_the_given_stream():
    import io

    stream = io.StringIO()
    renderer = ChatRenderer(use_rich=False, stream=stream)
    renderer.header(model="m", agent="a", workspace="local", session_id="s")
    renderer.on_tool_call("bash", {"command": "ls"})
    renderer.on_tool_result("bash", "out")
    renderer.on_todo([{"content": "x", "status": "pending"}])
    with renderer.thinking():
        pass
    renderer.on_done("final")
    written = stream.getvalue()
    assert "Garuda chat" in written
    assert "[tool] bash" in written
    assert "final" in written


def test_renderer_defaults_to_stdout(capsys):
    renderer = ChatRenderer(use_rich=False)
    renderer.on_done("done here")
    captured = capsys.readouterr()
    assert "done here" in captured.out
    assert captured.err == ""


def test_approval_prompt_can_be_routed_off_stdout(capsys, monkeypatch):
    import asyncio

    from garuda.interfaces.cli import stdin_approval

    monkeypatch.setattr("builtins.input", lambda: "y")
    assert asyncio.run(stdin_approval("run rm", stream=sys.stderr)) is True
    captured = capsys.readouterr()
    assert "Approve" in captured.err
    assert captured.out == ""


# --- T8: deadline propagation ----------------------------------------------


def _adapter(**kwargs):
    pytest.importorskip("harbor")
    from garuda.eval.harbor_adapter import GarudaHarborAgent

    return GarudaHarborAgent(
        logs_dir=Path(tempfile.mkdtemp()), model_name="openrouter/x/y", **kwargs
    )


def test_deadline_holds_back_a_margin_for_wind_down():
    """The agent's budget must expire before the harness kills it, not after."""
    assert _adapter(agent_timeout_sec=900)._resolved_deadline_sec() == pytest.approx(810.0)


def test_deadline_margin_is_configurable():
    resolved = _adapter(agent_timeout_sec=900, deadline_margin=0.2)._resolved_deadline_sec()
    assert resolved == pytest.approx(720.0)


def test_no_timeout_leaves_the_run_turn_bounded():
    assert _adapter()._resolved_deadline_sec() is None


@pytest.mark.parametrize("bad", ["abc", -5, 0, None])
def test_unusable_timeout_is_ignored_rather_than_crashing(bad):
    assert _adapter(agent_timeout_sec=bad)._resolved_deadline_sec() is None


def test_out_of_range_margin_falls_back_to_the_default():
    from garuda.eval.harbor_adapter import DEFAULT_DEADLINE_MARGIN

    resolved = _adapter(agent_timeout_sec=100, deadline_margin=5)._resolved_deadline_sec()
    assert resolved == pytest.approx(100 * (1 - DEFAULT_DEADLINE_MARGIN))


# --- benchmark-configurable system prompt ---------------------------------


def _harbor_profile():
    from garuda.agents.loader import load_profile

    return load_profile("harbor")


def test_no_prompt_override_leaves_the_profile_prompt_alone():
    """None means "don't touch it" — the caller keeps what prepare_agent_run resolved."""
    assert _adapter()._base_system_prompt(_harbor_profile()) is None


def test_inline_prompt_replaces_the_profile_base():
    resolved = _adapter(system_prompt="BENCH PROMPT")._base_system_prompt(_harbor_profile())
    assert resolved == "BENCH PROMPT"


def test_prompt_can_come_from_a_file(tmp_path):
    path = tmp_path / "prompt.txt"
    path.write_text("PROMPT FROM FILE\n")
    resolved = _adapter(system_prompt_path=str(path))._base_system_prompt(_harbor_profile())
    assert resolved == "PROMPT FROM FILE\n"


def test_append_keeps_the_profile_base():
    profile = _harbor_profile()
    resolved = _adapter(append_system_prompt="EXTRA RULE")._base_system_prompt(profile)
    assert resolved.startswith(profile.system_prompt.rstrip()[:40])
    assert resolved.endswith("EXTRA RULE")


def test_append_composes_onto_an_inline_replacement():
    resolved = _adapter(
        system_prompt="BASE", append_system_prompt="EXTRA"
    )._base_system_prompt(_harbor_profile())
    assert resolved == "BASE\n\nEXTRA"


def test_two_prompt_sources_are_refused():
    """One would silently shadow the other, and the run would be scored anyway."""
    with pytest.raises(ValueError, match="not both"):
        _adapter(system_prompt="a", system_prompt_path="b")


def test_unreadable_prompt_file_refuses_rather_than_falling_back(tmp_path):
    """Falling back to the profile prompt would score a run that did not use the
    prompt under test — the one thing a prompt experiment must not do."""
    agent = _adapter(system_prompt_path=str(tmp_path / "missing.txt"))
    with pytest.raises(ValueError, match="Cannot read system_prompt_path"):
        agent._base_system_prompt(_harbor_profile())


def test_prompt_override_preserves_skills_and_project_memory(tmp_path):
    """The override replaces the *base*, so `resolve_system_prompt` still appends
    the skills block and AGENTS.md memory. Overwriting the resolved prompt instead
    would drop both silently."""
    from garuda.agents.loader import resolve_system_prompt

    skill_dir = tmp_path / ".agent" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: a demo skill\n---\n\nBody.\n"
    )
    (tmp_path / "AGENTS.md").write_text("Project rule: always use tabs.\n")

    profile = _harbor_profile()
    profile.system_prompt = _adapter(system_prompt="CUSTOM")._base_system_prompt(profile)
    resolved = resolve_system_prompt(profile, tmp_path)

    assert "CUSTOM" in resolved
    assert "demo" in resolved, "skills block must survive the override"
    assert "always use tabs" in resolved, "project memory must survive the override"


def test_append_does_not_compound_across_trials():
    """One adapter instance serves every trial in a Harbor job.

    `run()` mutates `profile.system_prompt` to apply the override, so if profiles
    were shared or cached the appended text would accumulate — trial 1 with one
    copy, trial 50 with fifty, and no error to show for it. `load_profile` returns
    a fresh profile per call; this pins that, because the failure would be silent.
    """
    from garuda.agents.loader import load_profile

    agent = _adapter(append_system_prompt="EXTRA RULE")
    counts = []
    for _ in range(3):
        profile = load_profile("harbor")  # what prepare_agent_run does per run()
        resolved = agent._base_system_prompt(profile)
        profile.system_prompt = resolved  # what run() then assigns
        counts.append(resolved.count("EXTRA RULE"))
    assert counts == [1, 1, 1], f"append compounded across trials: {counts}"


def test_agents_dir_lets_a_job_name_a_fully_custom_profile(tmp_path):
    assert _adapter()._resolved_agents_dirs() is None
    single = _adapter(agents_dir=str(tmp_path))._resolved_agents_dirs()
    assert single == [tmp_path]
    several = _adapter(agents_dir=[str(tmp_path), str(tmp_path / "b")])._resolved_agents_dirs()
    assert several == [tmp_path, tmp_path / "b"]


def test_generic_profiles_keep_their_own_prompts():
    """The prompt knob is benchmark-scoped: it must not have changed the profiles
    a normal `garuda run` uses."""
    from garuda.agents.loader import load_profile
    from garuda.types import DEFAULT_SYSTEM_PROMPT

    build = load_profile("build")
    # `build` is the default for run/chat/serve and keeps its full principle list.
    assert build.system_prompt is not None
    assert all(f"{n}." in build.system_prompt for n in range(1, 9))
    # The library default is untouched too.
    assert "Operating principles" in DEFAULT_SYSTEM_PROMPT


def test_cli_exposes_a_deadline_flag():
    """The budget is generic, not an eval-only concern."""
    from garuda.interfaces.main import build_parser

    args = build_parser().parse_args(["run", "-t", "x", "--deadline-sec", "120"])
    assert args.deadline_sec == pytest.approx(120.0)


# --- genericity guard ------------------------------------------------------


AGENT_PACKAGES = ("core", "tools", "agents", "model", "workspace", "context", "mcp")


def test_agent_code_does_not_import_the_eval_package():
    """Benchmark adapters may depend on the agent; never the reverse.

    A core module importing `garuda.eval` is the first step toward the agent
    behaving differently under a benchmark than in real use.
    """
    root = Path(__file__).resolve().parents[1] / "garuda"
    offenders = []
    for package in AGENT_PACKAGES:
        for path in (root / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "garuda.eval" in text or "from garuda import eval" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"agent code must not depend on eval: {offenders}"


def test_no_benchmark_task_names_in_agent_code():
    """Guards against a hardcoded task/answer shortcut creeping into the agent."""
    root = Path(__file__).resolve().parents[1] / "garuda"
    needles = ("terminal-bench-pro", "swe-bench", "swebench")
    offenders = []
    for package in AGENT_PACKAGES:
        for path in (root / package).rglob("*.py"):
            lowered = path.read_text(encoding="utf-8").lower()
            for needle in needles:
                if needle in lowered:
                    offenders.append(f"{path.relative_to(root)}: {needle}")
    assert offenders == [], f"benchmark-specific references in agent code: {offenders}"


def test_events_json_serialisable_for_jsonl_mode():
    """`chat --json` prints events with json.dumps(default=str); a payload that
    cannot survive that would break the stream mid-run."""
    from garuda.core.events import EventStore, EventType

    store = EventStore()
    store.append(EventType.CONTRACT, {"action": "gate_yield", "outstanding": ["c1"]})
    for event in store.get_all():
        json.loads(json.dumps(event, default=str))
