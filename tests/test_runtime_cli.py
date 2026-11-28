"""Runtime CLI tests for issue #37 (P1.6).

Listing, inspection, explicit selection, handoff preview/confirm, and
recovery — in text and JSON — with the guarantee that previews mutate
nothing and switches need --confirm.

The execution tests drive the real entry points (`main()` for `garuda run`
and `garuda runtime ...`) against a trusted global settings file on disk and
an installed fake ACP executable: a shell shim on a temp PATH that execs
`python -m garuda.acp.fake_agent`.
"""

import asyncio
import contextlib
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from garuda.acp.catalog import builtin_manifest_dicts
from garuda.core.sessions import SessionStore
from garuda.interfaces.main import build_parser, main
from garuda.interfaces.runtime_cli import (
    cmd_handoff_preview,
    cmd_inspect,
    cmd_list,
    cmd_recover,
    configured_catalog,
)
from garuda.runtime.registry import parse_global_manifests

REPO = Path(__file__).resolve().parents[1]
SHIM = "fake-acp-shim"


def _manifests():
    native = {
        "runtime_id": "native",
        "kind": "native",
        "version": "1.2.0",
        "description": "The in-process Garuda loop.",
    }
    return parse_global_manifests([native, *builtin_manifest_dicts()])


# -- fixtures: an installed fake ACP executable and trusted settings -----------


def _install_shim(
    bin_dir: Path,
    profile: str = "success",
    *,
    marker: Path | None = None,
    report_cwd: bool = False,
    state_file: Path | None = None,
    approval_kind: str | None = None,
    extra: tuple[str, ...] = (),
) -> Path:
    """Install `fake-acp-shim` in `bin_dir`: a real executable on PATH.

    The child gets only PATH/HOME/LANG, so the shim sets PYTHONPATH itself.
    `marker` records which installed copy was actually launched.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / SHIM
    lines = ["#!/bin/sh"]
    if marker is not None:
        lines.append(f"echo {shlex.quote(str(bin_dir))} >> {shlex.quote(str(marker))}")
    lines.append(
        f"PYTHONPATH={shlex.quote(str(REPO))} exec {shlex.quote(sys.executable)} "
        f"-m garuda.acp.fake_agent --profile {profile}"
        + (" --report-cwd" if report_cwd else "")
        + (" --state-file " + shlex.quote(str(state_file)) if state_file else "")
        + (" --approval-kind " + shlex.quote(approval_kind) if approval_kind is not None else "")
        + "".join(" " + shlex.quote(arg) for arg in extra)
        + ' "$@"'
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _trusted_settings(tmp_path: Path, monkeypatch, *, disabled=()) -> Path:
    settings = tmp_path / "trusted-settings.yaml"
    body = (
        "runtimes:\n"
        "  - runtime_id: fakeacp\n"
        "    kind: acp\n"
        f"    command: [{SHIM}]\n"
        "    version: '1'\n"
        "    setup: Install the fake shim.\n"
    )
    if disabled:
        body += "disabled_runtimes: [" + ", ".join(disabled) + "]\n"
    settings.write_text(body, encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    return settings


def _on_path(monkeypatch, *dirs: Path) -> None:
    monkeypatch.setenv(
        "PATH", os.pathsep.join([*(str(d) for d in dirs), os.environ.get("PATH", "")])
    )


def _git_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    (ws / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=ws, check=True, env=env)
    # Auto-maintenance can outlive commit and mutate .git during boundary
    # snapshots; fixture setup must leave no background Git writer.
    subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                    "commit", "-qm", "init"], cwd=ws, check=True, env=env)
    return ws


def _main(monkeypatch, capsys, *argv) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["garuda", *argv])
    with pytest.raises(SystemExit) as exited:
        main()
    captured = capsys.readouterr()
    return exited.value.code, captured.out + captured.err


@contextlib.contextmanager
def _hard_deadline(seconds: int):
    """Fail — rather than hang CI — if a guarded path stops answering (e.g. an
    unanswered ACP permission request or a delivery with no timeout)."""

    def _expired(_signum, _frame):
        raise TimeoutError(f"guarded path still running after {seconds}s")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return out.stdout.strip().startswith("Z") or not out.stdout.strip()


def _native_stack(ws: Path, summary: str = "ok"):
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall

    model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": summary})
                ],
            )
        ]
    )
    return dict(
        model=model,
        agent=DefaultAgent(),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
        permissions=PermissionEngine(mode="yolo"),
        workspace=str(ws),
    )


def _native_session(ws: Path, session_id: str) -> None:
    """A real persisted native session (baseline, checkpoint, trail)."""
    from garuda.core.events import EventStore
    from garuda.interfaces.runner import run_agent_task

    result = asyncio.run(
        run_agent_task(
            task="move it",
            events=EventStore(session_id=session_id),
            store=SessionStore(),
            **_native_stack(ws),
        )
    )
    assert result.success


# -- parser, list, inspect ------------------------------------------------------


def test_parser_has_runtime_group_with_defaults():
    args = build_parser().parse_args(["run", "-t", "x"])
    assert args.runtime is None  # omitted: routing may choose; native is the default
    assert build_parser().parse_args(["run", "-t", "x", "--runtime", "codex"]).runtime == "codex"
    sub = build_parser().parse_args(["runtime", "list"])
    assert sub.runtime_command == "list"
    handoff = build_parser().parse_args(
        ["runtime", "handoff", "--session", "s", "--to", "codex"]
    )
    assert handoff.confirm is False
    resumed = build_parser().parse_args(
        ["runtime", "resume", "--session", "s", "-t", "again"]
    )
    assert resumed.runtime_command == "resume"
    assert resumed.session == "s" and resumed.task == "again"


def test_list_and_inspect_text_and_json(tmp_path, monkeypatch):
    # The vendors are absent here even on a machine where they are installed.
    monkeypatch.setenv("PATH", str(tmp_path))
    text = cmd_list(_manifests())
    assert "native" in text and "claude" in text
    records = json.loads(cmd_list(_manifests(), as_json=True))
    assert {r["runtime_id"] for r in records} >= {"native", "claude", "codex"}
    assert all("available" in r and "auth" in r for r in records)

    inspect_text = cmd_inspect(_manifests(), "claude")
    assert "claude" in inspect_text and "log in" in inspect_text
    record = json.loads(cmd_inspect(_manifests(), "codex", as_json=True))
    assert record["runtime_id"] == "codex"
    assert record["quota"] is None
    with pytest.raises(KeyError):
        cmd_inspect(_manifests(), "nope")


def test_configured_catalog_uses_the_shared_builder_and_validates():
    from garuda.runtime import RegistryError

    catalog = configured_catalog(global_settings={"runtimes": []}, project_settings={})
    ids = {m.runtime_id for m in catalog.registry.manifests}
    assert ids >= {"native", *(d["runtime_id"] for d in builtin_manifest_dicts())}
    with pytest.raises(RegistryError):
        configured_catalog(global_settings={"runtimes": "nope"}, project_settings={})


def test_inspect_and_list_through_main_honor_trusted_disablement(tmp_path, monkeypatch, capsys):
    """An *installed* executable disabled in the trusted settings file reads as
    disabled and unavailable — never as launchable — on every CLI surface."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _on_path(monkeypatch, tmp_path / "bin")
    shim = _install_shim(tmp_path / "bin")

    _trusted_settings(tmp_path, monkeypatch)
    code, out = _main(monkeypatch, capsys, "runtime", "inspect", "fakeacp", "--workspace", str(ws))
    assert code == 0
    assert "fakeacp (acp): available" in out and str(shim) in out

    _trusted_settings(tmp_path, monkeypatch, disabled=("fakeacp",))
    code, out = _main(monkeypatch, capsys, "runtime", "inspect", "fakeacp", "--workspace", str(ws))
    assert code == 0
    assert "fakeacp (acp): unavailable" in out
    assert "disabled by user configuration" in out
    assert str(shim) not in out
    code, out = _main(
        monkeypatch, capsys, "runtime", "list", "--json", "--workspace", str(ws)
    )
    (record,) = [r for r in json.loads(out) if r["runtime_id"] == "fakeacp"]
    assert record["available"] is False and record["executable"] is None

    code, out = _main(
        monkeypatch, capsys, "run", "-t", "must not run", "--workspace", str(ws),
        "--runtime", "fakeacp",
    )
    assert code == 2
    assert "disabled" in out
    assert SessionStore().list_sessions() == []


def test_runtime_commands_report_malformed_trusted_settings(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "bad.yaml"
    settings.write_text("disabled_runtimes: nope\n", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    code, out = _main(monkeypatch, capsys, "runtime", "list", "--workspace", str(tmp_path))
    assert code == 2
    assert "runtime selection refused" in out and "disabled_runtimes" in out


# -- garuda run --runtime <acp> ---------------------------------------------------


def test_run_runtime_acp_holds_native_run_invariants(tmp_path, monkeypatch, capsys):
    from garuda.runtime.recovery import recover
    from garuda.runtime.session import validate_authority_snapshot
    from garuda.workspace.lease import LeaseStore

    ws = _git_workspace(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    # strict-v1 rejects a session/new without an absolute cwd: the workspace
    # must be what the harness is rooted at.
    _install_shim(tmp_path / "bin", profile="strict-v1", report_cwd=True)
    _trusted_settings(tmp_path, monkeypatch)

    code, out = _main(
        monkeypatch, capsys, "run", "-t", "hello via cli", "--workspace", str(ws),
        "--runtime", "fakeacp",
    )
    assert code == 0, out
    assert "completed" in out and "not verified" in out
    # The harness session is rooted at the workspace, not Garuda's cwd.
    assert f"done: hello via cli (cwd={ws})" in out
    store = SessionStore()
    (meta,) = store.list_sessions()
    session_id = meta["session_id"]
    unified = store.load_unified(session_id)
    (segment,) = unified.segments
    assert (segment.runtime_id, segment.kind, segment.native_session_id) == (
        "fakeacp", "acp", "fake-s1"
    )
    validate_authority_snapshot(segment.capabilities)
    (child,) = meta["runtime_children"]
    assert child["state"] == "exited" and _pid_gone(child["pid"])
    assert meta["status"] == "completed" and meta["verified"] is False
    assert meta["workspace"] == str(ws)
    assert meta["baseline"]["commit"] and meta["delta_attribution"]
    assert LeaseStore().holders_of(ws) == []
    report = recover(store, session_id)
    assert report.state.value == "external"


def test_run_runtime_acp_enforces_the_deadline(tmp_path, monkeypatch, capsys):
    """#701: --deadline-sec on an ACP run cancels the turn, reaps the agent,
    and settles the session failed with the deadline as its typed cause."""
    from garuda.workspace.lease import LeaseStore

    ws = _git_workspace(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="slow")  # the turn sleeps 60 s
    _trusted_settings(tmp_path, monkeypatch)

    started = time.monotonic()
    with _hard_deadline(50):
        code, out = _main(
            monkeypatch, capsys, "run", "-t", "take your time", "--workspace", str(ws),
            "--runtime", "fakeacp", "--deadline-sec", "2",
        )
    assert time.monotonic() - started < 40
    assert code == 1, out
    assert "exceeded --deadline-sec 2" in out
    (meta,) = SessionStore().list_sessions()
    assert meta["status"] == "failed" and meta["state"]["outcome"] == "failed"
    assert (meta["stop_reason"], meta["error_code"]) == ("deadline", "run.deadline_exceeded")
    assert meta["limits"] == {"deadline_sec": 2.0}
    (child,) = meta["runtime_children"]
    assert child["state"] == "exited" and _pid_gone(child["pid"])
    assert LeaseStore().holders_of(ws) == []


def test_run_runtime_acp_records_a_deadline_it_met(tmp_path, monkeypatch, capsys):
    ws = _git_workspace(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)

    code, out = _main(monkeypatch, capsys, "run", "-t", "quick", "--workspace", str(ws),
                      "--runtime", "fakeacp", "--deadline-sec", "60")
    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    assert meta["status"] == "completed" and meta["limits"] == {"deadline_sec": 60.0}
    assert "stop_reason" not in meta


@pytest.mark.parametrize("flags", [["--max-turns", "1"], ["--deadline-sec", "0"],
                                   ["--deadline-sec", "nan"], ["--deadline-sec", "-5"]])
def test_run_runtime_acp_refuses_limits_it_cannot_enforce(tmp_path, monkeypatch, capsys, flags):
    """#701: an ACP run refuses --max-turns and an invalid deadline up front."""
    ws = _git_workspace(tmp_path)
    marker = tmp_path / "launched.txt"
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", marker=marker)
    _trusted_settings(tmp_path, monkeypatch)

    code, out = _main(monkeypatch, capsys, "run", "-t", "must not run", "--workspace", str(ws),
                      "--runtime", "fakeacp", *flags)
    assert code == 2, out
    assert flags[0] in out
    assert not marker.exists()
    assert SessionStore().list_sessions() == []


def test_run_runtime_acp_refuses_a_foreign_mutating_lease(tmp_path, monkeypatch, capsys):
    from garuda.workspace.lease import LeaseStore

    ws = _git_workspace(tmp_path)
    marker = tmp_path / "launched.txt"
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", marker=marker)
    _trusted_settings(tmp_path, monkeypatch)
    LeaseStore().acquire(ws, "someone-else", mode="mutating")

    code, out = _main(
        monkeypatch, capsys, "run", "-t", "must not run", "--workspace", str(ws),
        "--runtime", "fakeacp",
    )
    assert code == 1
    assert "mutably held" in out
    assert not marker.exists()
    assert SessionStore().list_sessions() == []
    assert [h.session_id for h in LeaseStore().holders_of(ws)] == ["someone-else"]


@pytest.mark.parametrize(("profile", "expected_exit"), [
    ("streaming", 0), ("remote-error", 1), ("streaming", 3),
], ids=["success", "remote-error", "no-edits-refusal"])
def test_run_runtime_acp_json_keeps_diagnostics_off_stdout(
        tmp_path, monkeypatch, capsys, profile, expected_exit, fake_acp_confinement):
    ws = _git_workspace(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile=profile)
    _trusted_settings(tmp_path, monkeypatch)
    argv = [
        "garuda", "run", "-t", "hello via CLI", "--workspace", str(ws), "--runtime", "fakeacp", "--json",
    ]
    if expected_exit == 3:
        from garuda.interfaces import main as interface

        fake_acp_confinement("fakeacp")
        check = interface._finish_no_edits

        def file_changed_before_check(*args):
            (ws / "a.txt").write_text("concurrent edit\n", encoding="utf-8")
            return check(*args)

        monkeypatch.setattr(interface, "_finish_no_edits", file_changed_before_check)
        argv.append("--no-edits")
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == expected_exit
    output = capsys.readouterr()
    rows = [json.loads(line) for line in output.out.splitlines()]
    if expected_exit:
        assert ("synthetic internal failure" if expected_exit == 1 else "changed: a.txt") in output.err
        assert not rows
    else:
        (meta,) = SessionStore().list_sessions()
        assert {row["session_id"] for row in rows} == {meta["session_id"]}
        assert [row["seq"] for row in rows] == list(range(len(rows)))
        chunks = [row for row in rows if row["kind"] == "message" and "chunk" in row["payload"]]
        assert {row["turn"] for row in chunks} == {1}
        assert "".join(row["payload"]["chunk"] for row in chunks) == "hello, world"
        assert "not verified" in output.err


def test_run_runtime_acp_json_survives_an_unserializable_event_payload(tmp_path, monkeypatch, capsys):
    from garuda.acp.adapter import AcpRuntime

    ws = _git_workspace(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="streaming")
    _trusted_settings(tmp_path, monkeypatch)
    poll = AcpRuntime.poll_events

    async def poisoned(self, cursor):
        events, end = await poll(self, cursor)
        for event in events:
            if event.kind.value == "message":
                event.payload.update(score=float("nan"), limit=float("inf"), blob=object())
        return events, end

    monkeypatch.setattr(AcpRuntime, "poll_events", poisoned)
    monkeypatch.setattr(sys, "argv", [
        "garuda", "run", "-t", "hello via CLI", "--workspace", str(ws), "--runtime", "fakeacp", "--json",
    ])

    with pytest.raises(SystemExit) as exited:
        main()
    output = capsys.readouterr()
    assert exited.value.code == 0, output.err

    def no_constants(token):
        raise AssertionError(f"non-standard JSON token {token} on stdout")

    rows = [json.loads(line, parse_constant=no_constants) for line in output.out.splitlines()]
    chunks = [row for row in rows if row["kind"] == "message" and "chunk" in row["payload"]]
    assert "".join(row["payload"]["chunk"] for row in chunks) == "hello, world"
    assert all(row["payload"]["score"] is None and row["payload"]["limit"] is None
               and isinstance(row["payload"]["blob"], str) for row in chunks)
    (meta,) = SessionStore().list_sessions()
    assert meta["status"] == "completed"
    assert meta["final_message"] == "hello, world"


def test_strict_json_value_coerces_keys_the_way_json_dumps_does():
    from garuda.interfaces.main import _strict_json_value

    keys = {None: 1, True: 2, False: 3, 7: 4, 1.5: 5, 1e20: 6, "s": 7}
    expected = json.loads(json.dumps(keys))
    assert _strict_json_value(keys) == expected
    assert _strict_json_value({**keys, "blob": object()}).keys() == {*expected, "blob"}
    non_finite = _strict_json_value({float("nan"): 1, float("inf"): 2, float("-inf"): 3})
    assert non_finite == {"NaN": 1, "Infinity": 2, "-Infinity": 3}
    json.dumps(non_finite, allow_nan=False)


def test_run_runtime_acp_headless_approvals_are_denied_and_audited(
    tmp_path, monkeypatch, capsys
):
    ws = _git_workspace(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="approval")
    _trusted_settings(tmp_path, monkeypatch)

    with _hard_deadline(60):
        code, out = _main(
            monkeypatch, capsys, "run", "-t", "rm -rf build", "--workspace", str(ws),
            "--runtime", "fakeacp",
        )
    assert code == 0, out
    (meta,) = SessionStore().list_sessions()
    approvals = [v for k, v in meta.items() if k.startswith("approval:")]
    assert [(a["outcome"], a["runtime_id"]) for a in approvals] == [("deny", "fakeacp")]
    assert "denied" in out


# -- garuda runtime handoff ---------------------------------------------------------


async def test_handoff_preview_mutates_nothing(tmp_path, monkeypatch):
    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")

    preview = cmd_handoff_preview(store, "s1", "codex")
    assert "--confirm" in preview
    assert store.load_unified("s1").handoff["state"] == "none"


def test_handoff_preview_resolves_a_unique_displayed_session_prefix(monkeypatch, capsys):
    store = SessionStore()
    session_id = "preview-session-unique"
    store.begin(session_id, task="move it", model="m", agent="a", workspace="w")
    store.ensure_unified(session_id)

    code, output = _main(
        monkeypatch,
        capsys,
        "runtime",
        "handoff",
        "--session",
        "preview-",
        "--to",
        "codex",
    )

    assert code == 0
    assert f"handoff preview: {session_id} -> codex" in output
    assert store.load_unified(session_id).handoff["state"] == "none"


def test_runtime_handoff_preview_refuses_ambiguous_prefix_without_mutating(monkeypatch, capsys):
    store = SessionStore()
    session_ids = ("ambiguous-first", "ambiguous-second")
    for session_id in session_ids:
        store.begin(session_id, task="move it", model="m", agent="a", workspace="w")
        store.ensure_unified(session_id)

    code, output = _main(
        monkeypatch,
        capsys,
        "runtime",
        "handoff",
        "--session",
        "ambiguous-",
        "--to",
        "codex",
    )

    assert code == 2
    assert "Ambiguous session prefix" in output
    assert all(store.load_unified(session_id).handoff["state"] == "none" for session_id in session_ids)


def test_handoff_confirm_then_recover_end_to_end(tmp_path, monkeypatch, capsys):
    """`garuda runtime handoff --confirm` moves ownership, delivers the package
    as the target's first prompt, closes and reaps the target, and leaves a
    session that recovery reports external and native resume refuses."""
    from garuda.interfaces.runtime_cli import cmd_resume
    from garuda.runtime.recovery import RecoveryError
    from garuda.runtime.session import validate_authority_snapshot
    from garuda.workspace.lease import LeaseStore

    ws = _git_workspace(tmp_path)
    _native_session(ws, "src-1")
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="strict-v1", report_cwd=True)
    _trusted_settings(tmp_path, monkeypatch)

    code, out = _main(
        monkeypatch, capsys, "runtime", "handoff", "--session", "src-1", "--to", "fakeacp",
        "--workspace", str(ws), "--confirm",
    )
    assert code == 0, out
    assert "handoff acknowledged: src-1 -> fakeacp" in out
    # The fake echoes its prompt: the package reached the target as a turn.
    reply = out.split("target reply: ", 1)[1].splitlines()[0]
    assert reply.startswith("done: ") and "move it" in reply
    assert f"(cwd={ws})" in out

    store = SessionStore()
    meta = store.load_meta("src-1")
    unified = store.load_unified("src-1")
    assert [s.runtime_id for s in unified.segments] == ["native", "fakeacp"]
    assert unified.active.native_session_id == "fake-s1"
    validate_authority_snapshot(unified.active.capabilities)
    assert unified.handoff["state"] == "acknowledged"
    assert unified.handoff["target_state"] == "closed"
    assert unified.handoff["delivered_turn"] == 1
    assert unified.handoff["baseline_commit"]
    (child,) = meta["runtime_children"]
    assert child["runtime_id"] == "fakeacp"
    assert child["state"] == "exited" and _pid_gone(child["pid"])
    assert LeaseStore().holders_of(ws) == []
    assert (store.session_dir("src-1") / "handoff.md").is_file()

    code, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", "src-1", "--json")
    assert code == 0
    report = json.loads(out)
    assert report["state"] == "external"
    assert "explicit new handoff" in " ".join(report["notes"])

    with pytest.raises(RecoveryError, match="external runtime"):
        asyncio.run(
            cmd_resume(store=store, session_id="src-1", task="take it back", **_native_stack(ws))
        )
    # #698: preview and --confirm agree, refusing an externally owned source up front.
    before = store.load_meta("src-1")
    for confirm in ((), ("--confirm",)):
        code, out = _main(
            monkeypatch, capsys, "runtime", "handoff", "--session", "src-1", "--to", "fakeacp",
            "--workspace", str(ws), *confirm,
        )
        assert code == 2
        assert out.startswith("Error: runtime.handoff_external_source: session src-1 is owned "
                              "by external runtime 'fakeacp'")
        assert "garuda run --resume src-1 --as <runtime>" in out
        assert "explicit new handoff" not in out and "handoff preview" not in out
    assert store.load_meta("src-1") == before
    assert store.load_unified("src-1").active.runtime_id == "fakeacp"

    # `external` is not a dead end: with the target recorded stopped and its
    # child reaped, an explicit reclaim returns ownership to the native tenure.
    meta = store.load_meta("src-1")
    handoff, children = meta["handoff"], meta["runtime_children"]
    # No process evidence the target stopped (no retired child, no terminal
    # target state): refused.
    store.update_meta(
        "src-1",
        {"handoff": {**handoff, "target_state": "delivered"}, "runtime_children": []},
    )
    code, out = _main(monkeypatch, capsys, "runtime", "reclaim", "--session", "src-1")
    assert code == 1 and "cannot prove it stopped" in out
    store.update_meta("src-1", {"handoff": handoff, "runtime_children": children})
    # A live lease naming the session: refused by the recovery pass.
    issuer = LeaseStore()
    held = issuer.acquire(ws, "src-1", mode="mutating")
    code, out = _main(monkeypatch, capsys, "runtime", "reclaim", "--session", "src-1")
    assert code == 1 and "reclaim refused" in out
    issuer.release(ws, "src-1", epoch=held.epoch)
    assert store.load_unified("src-1").active.runtime_id == "fakeacp"
    code, out = _main(monkeypatch, capsys, "runtime", "reclaim", "--session", "src-1")
    assert code == 0 and "ownership reclaimed by native" in out, out
    unified = store.load_unified("src-1")
    assert unified.active.kind == "native"
    assert [s.runtime_id for s in unified.segments] == ["native", "fakeacp", "native"]
    assert unified.handoff["reclaimed_from"] == "fakeacp"
    code, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", "src-1", "--json")
    assert code == 0 and json.loads(out)["state"] == "resumable"
    code, out = _main(monkeypatch, capsys, "runtime", "reclaim", "--session", "src-1")
    assert code == 1 and "not externally owned" in out


def test_handoff_refuses_a_foreign_mutating_lease_before_moving(
    tmp_path, monkeypatch, capsys
):
    from garuda.workspace.lease import LeaseStore

    ws = _git_workspace(tmp_path)
    _native_session(ws, "src-leased")
    marker = tmp_path / "launched.txt"
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", marker=marker)
    _trusted_settings(tmp_path, monkeypatch)
    LeaseStore().acquire(ws, "someone-else", mode="mutating")

    code, out = _main(
        monkeypatch, capsys, "runtime", "handoff", "--session", "src-leased", "--to",
        "fakeacp", "--workspace", str(ws), "--confirm",
    )
    assert code == 1 and "mutably held" in out
    assert not marker.exists()
    unified = SessionStore().load_unified("src-leased")
    assert unified.handoff["state"] == "none"
    assert [s.runtime_id for s in unified.segments] == ["native"]


def test_handoff_launches_the_exact_checked_executable(tmp_path, monkeypatch, capsys):
    """A PATH change after the pre-transaction check cannot swap the binary."""
    from garuda.runtime.native import NativeGarudaRuntime

    ws = _git_workspace(tmp_path)
    _native_session(ws, "src-path")
    marker = tmp_path / "launched.txt"
    checked, swapped = tmp_path / "bin-a", tmp_path / "bin-b"
    _install_shim(checked, marker=marker)
    _install_shim(swapped, marker=marker)
    _on_path(monkeypatch, checked)
    _trusted_settings(tmp_path, monkeypatch)
    original_resume = NativeGarudaRuntime.resume

    async def _resume_then_swap_path(self, **kwargs):
        info = await original_resume(self, **kwargs)
        os.environ["PATH"] = str(swapped) + os.pathsep + os.environ["PATH"]
        return info

    monkeypatch.setattr(NativeGarudaRuntime, "resume", _resume_then_swap_path)
    code, out = _main(
        monkeypatch, capsys, "runtime", "handoff", "--session", "src-path", "--to", "fakeacp",
        "--workspace", str(ws), "--confirm",
    )
    assert code == 0, out
    assert marker.read_text(encoding="utf-8").split() == [str(checked)]


def test_handoff_target_startup_failure_rolls_back_to_the_source(tmp_path, monkeypatch, capsys):
    ws = _git_workspace(tmp_path)
    _native_session(ws, "src-fail")
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="version-mismatch")
    _trusted_settings(tmp_path, monkeypatch)

    code, out = _main(
        monkeypatch, capsys, "runtime", "handoff", "--session", "src-fail", "--to", "fakeacp",
        "--workspace", str(ws), "--confirm",
    )
    assert code == 1 and "target startup failed" in out
    unified = SessionStore().load_unified("src-fail")
    assert unified.handoff["state"] == "failed"
    assert [s.runtime_id for s in unified.segments] == ["native"]
    code, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", "src-fail")
    assert code == 0 and "resumable" in out


async def test_handoff_delivery_timeout_is_a_target_failure(tmp_path, monkeypatch):
    """A hung target is bounded: it is closed and recorded failed, and the
    source is not resumed over whatever the target may have done."""
    from garuda.interfaces.runtime_cli import cmd_handoff_confirm
    from garuda.runtime.handoff import HandoffDeliveryError
    from garuda.workspace.lease import LeaseStore

    ws = _git_workspace(tmp_path)
    from garuda.core.events import EventStore
    from garuda.interfaces.runner import run_agent_task

    assert (
        await run_agent_task(
            task="move it", events=EventStore(session_id="src-slow"), store=SessionStore(),
            **_native_stack(ws),
        )
    ).success
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="slow")
    _trusted_settings(tmp_path, monkeypatch)
    store = SessionStore()
    with pytest.raises(HandoffDeliveryError, match="deadline"):
        await asyncio.wait_for(
            cmd_handoff_confirm(
                store, "src-slow", "fakeacp", workspace=str(ws), delivery_timeout=0.5
            ),
            60,
        )
    unified = store.load_unified("src-slow")
    assert unified.active.runtime_id == "fakeacp"
    assert unified.handoff["target_state"] == "failed"
    (child,) = store.load_meta("src-slow")["runtime_children"]
    assert child["state"] == "exited" and _pid_gone(child["pid"])
    assert LeaseStore().holders_of(ws) == []


async def test_handoff_confirm_refuses_unknown_disabled_and_native(tmp_path):
    from garuda.interfaces.runtime_cli import cmd_handoff_confirm
    from garuda.runtime import RegistryError

    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace=str(tmp_path))
    store.ensure_unified("s1")
    store.checkpoint_messages("s1", [])
    extra = {"runtime_id": "fakeacp", "kind": "acp", "command": [SHIM], "version": "1"}

    def _catalog(disabled=frozenset()):
        return configured_catalog(
            global_settings={"runtimes": [extra]}, project_settings={}, disabled=disabled
        )

    with pytest.raises(RegistryError, match="unknown runtime"):
        await cmd_handoff_confirm(store, "s1", "ghost", catalog=_catalog())
    with pytest.raises(RegistryError, match="disabled"):
        await cmd_handoff_confirm(
            store, "s1", "fakeacp", catalog=_catalog(frozenset({"fakeacp"}))
        )
    refused = await cmd_handoff_confirm(store, "s1", "native", catalog=_catalog())
    assert "resume" in refused
    assert store.load_unified("s1").handoff["state"] == "none"


async def test_handoff_confirm_refuses_missing_executable_before_moving(tmp_path):
    from garuda.acp.catalog import AcpUnavailableError
    from garuda.interfaces.runtime_cli import cmd_handoff_confirm

    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace=str(tmp_path))
    store.ensure_unified("s1")
    store.checkpoint_messages("s1", [])
    missing = {
        "runtime_id": "ghost",
        "kind": "acp",
        "command": ["definitely-not-installed-xyz"],
        "version": "1",
        "setup": "Install ghost.",
    }
    catalog = configured_catalog(
        global_settings={"runtimes": [missing]}, project_settings={}, disabled=frozenset()
    )
    with pytest.raises(AcpUnavailableError, match="Install ghost"):
        await cmd_handoff_confirm(store, "s1", "ghost", catalog=catalog)
    assert store.load_unified("s1").handoff["state"] == "none"


async def test_handoff_refuses_a_session_without_an_absolute_workspace():
    from garuda.interfaces.runtime_cli import cmd_handoff_confirm

    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace=".")
    store.ensure_unified("s1")
    with pytest.raises(ValueError, match="--workspace"):
        await cmd_handoff_confirm(store, "s1", "fakeacp")


# -- resume and recover -------------------------------------------------------------


async def test_resume_command_continues_a_persisted_session(tmp_path):
    from garuda.core.events import EventStore
    from garuda.interfaces.runner import run_agent_task
    from garuda.interfaces.runtime_cli import cmd_resume

    (tmp_path / "ws").mkdir()
    store = SessionStore()
    first = await run_agent_task(
        task="first task",
        events=EventStore(session_id="rs-first"),
        store=store,
        **_native_stack(tmp_path / "ws", "one"),
    )
    assert first.success
    summary = await cmd_resume(
        store=store, session_id="rs-first", task="second task",
        **_native_stack(tmp_path / "ws", "two"),
    )
    assert "resumed rs-first as " in summary
    assert "success" in summary


async def test_recover_command_reports():
    store = SessionStore()
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")
    store.checkpoint_messages("s1", [])
    text = cmd_recover(store, "s1")
    assert "resumable" in text
    report = json.loads(cmd_recover(store, "s1", as_json=True))
    assert report["state"] == "resumable"


@pytest.mark.parametrize("case", ["cli", "dashboard", "unsettled", "quarantined", "role-run"])
def test_recover_of_a_flow_points_to_the_resume_that_accepts_it(case):
    """#899: a dashboard-owned flow is resumed by the dashboard, never `garuda flow resume`."""
    from garuda.flows import engine
    from garuda.runtime.session_state import finished

    store = SessionStore()
    if case != "cli":
        from garuda.core.session_publication import protect_session

        directory = store.session_dir("f1")
        directory.mkdir(mode=0o700, parents=True)
        protect_session(directory / "meta.json", kind="owned-flow")
    store.begin("f1", task="t", model="flow", agent="flow:one", workspace="w")
    meta = {"kind": "flow", "flow": {"name": "one"}, "flow_state": "stopped",
            "status": "failed", "state": finished(success=False),
            "flow_stop": {"code": engine.INTERRUPTED_STOP, "step": "only", "message": "stopped"}}
    if case != "cli":
        meta["flow_operation"] = {"operation_id": "op", "session_id": "f1"}
    if case == "quarantined":
        meta["flow_state"] = "quarantined"
    if case == "role-run":
        meta["launch_kind"] = "run"
    store.update_meta("f1", meta)
    if case == "unsettled":
        flow_dir = engine.flow_dir(store, "f1")
        flow_dir.mkdir(parents=True)
        (flow_dir / engine.JOURNAL).write_text(json.dumps(
            {"event": "intent", "index": 0, "step": "only", "attempt": 1}) + "\n")
    text = cmd_recover(store, "f1")
    assert "session f1: flow" in text
    if case == "cli":
        assert "continue it with `garuda flow resume f1`" in text
        return
    assert "garuda flow resume f1" not in text, text
    if case == "dashboard":
        assert "Resume on its session page" in text and "POST /api/scenarios/resume" in text
        assert '"session_id": "f1"' in text
        return
    assert "/api/scenarios/resume" not in text, text
    expected = {"unsettled": "cannot be resumed now (flow.interrupted_unsettled)",
                "quarantined": "cannot be resumed now (flow.quarantined)",
                "role-run": "neither `garuda flow resume` nor the dashboard's Resume"}[case]
    assert expected in text, text


def test_reclaim_rechecks_live_children_inside_the_write(tmp_path, monkeypatch):
    """A child recorded after the recovery pass (e.g. a concurrent launch) is
    still seen: the live-child check runs inside the locked ownership write."""
    from garuda.runtime import recovery
    from garuda.runtime.session import RuntimeSegment

    store = SessionStore(tmp_path / "sessions")
    store.begin("r1", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.checkpoint_messages("r1", [])
    store.ensure_unified("r1")
    store.record_handoff(
        "r1",
        state="acknowledged",
        attempts=1,
        target_state="closed",
        active_segment=RuntimeSegment(runtime_id="ext", kind="acp", native_session_id="x"),
    )
    store.update_meta("r1", {"runtime_children": [{"runtime_id": "ext", "state": "live"}]})
    monkeypatch.setattr(
        recovery,
        "recover",
        lambda *_a, **_k: recovery.RecoveryReport(
            session_id="r1", state=recovery.RestartState.EXTERNAL, resume_session_id="r1"
        ),
    )
    with pytest.raises(recovery.RecoveryError, match="still has a live recorded child"):
        recovery.reclaim_native(store, "r1")
    assert store.load_unified("r1").active.runtime_id == "ext"


async def test_shared_acp_launch_binds_the_compiled_role_and_complete_wire_request(
    tmp_path, monkeypatch
):
    import hashlib

    from garuda.agents import role_agent
    from garuda.interfaces.runtime_cli import run_acp_task
    from garuda.runtime.roles import RolePlan
    from tests.test_acp_prompt_views import CANARY, conversation_http

    ws = _git_workspace(tmp_path)
    definitions = ws / ".agent" / "agents"
    definitions.mkdir(parents=True)
    (definitions / "lean.yaml").write_text(
        "version: 1\ninstructions: {text: '" + CANARY + " café 🦅'}\n", encoding="utf-8"
    )
    bound = role_agent.bind(RolePlan(role="coder", runtime_id="fakeacp", kind="acp",
                                     profile="lean", permissions="auto"), str(ws))
    capture = tmp_path / "wire.json"
    _install_shim(tmp_path / "bin", profile="resume", state_file=capture)
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    store = SessionStore()
    result = await run_acp_task("Fix the fixture", runtime_id="fakeacp", workspace=str(ws),
                                store=store, role_plan=bound, emit=lambda _line: None)
    assert result["status"] == "completed"
    (wire_text,) = json.loads(capture.read_text())["prompts"]
    assert CANARY in wire_text and wire_text.endswith("Fix the fixture")
    assert wire_text.startswith("[garuda] The following are user-supplied role instructions")
    sid = result["session_id"]
    store.update_meta(sid, {"agent": "later", "agent_digest": "f" * 64})
    (segment,) = conversation_http(store, ws, sid)["agent"]["segments"]
    assert segment["name"] == "lean" and segment["digest"] == bound.agent_digest
    assert segment["runtime"] == "fakeacp" and segment["kind"] == "acp_execution"
    assert segment["prompts"] == [{"digest": hashlib.sha256(wire_text.encode()).hexdigest(),
                                    "chars": len(wire_text)}]


@pytest.mark.parametrize("reference", ["diagnostic-cli", "latest"])
def test_selected_diagnostics_use_the_public_cli_without_execution_setup(tmp_path, monkeypatch, capsys, reference):
    from garuda.core.events import EventStore, EventType
    from garuda.core.failure_evidence import DIAGNOSTIC_DIR

    store = SessionStore(tmp_path / "sessions")
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(store.root))
    store.begin("diagnostic-cli", task="not exported", model="m", agent="a", workspace=str(tmp_path))
    foreign = SessionStore(tmp_path / "foreign")
    foreign.begin("foreign", task="t", model="m", agent="a", workspace=str(tmp_path / "other"))
    foreign.update_meta("foreign", {"updated_at": "9999-01-01"})
    (store.root / "foreign").symlink_to(foreign.session_dir("foreign"), target_is_directory=True)
    EventStore(session_id="diagnostic-cli", persist_path=store.events_path("diagnostic-cli")).append(
        EventType.SESSION_END,
        {"success": False, "reason": "model_error", "error": "token=cli-secret failed safely"},
    )
    def no_execution_setup(*_args, **_kwargs):
        raise AssertionError("diagnostic inspection must not configure runtimes or models")

    monkeypatch.setattr("garuda.interfaces.main._configured_catalog", no_execution_setup)
    monkeypatch.setattr("garuda.agents.setup.prepare_agent_run", no_execution_setup)
    session_dir = store.session_dir("diagnostic-cli")
    before = {p.relative_to(session_dir): p.read_bytes() for p in session_dir.rglob("*") if p.is_file()}
    args = ("runtime", "support", "--session", reference, "--select", "causes")
    code, text = _main(monkeypatch, capsys, *args, "--preview")
    assert code == 0
    preview = json.loads(text)
    code, text = _main(monkeypatch, capsys, *args, "--export")
    assert code == 0 and json.loads(text) == json.loads(preview["json"])
    assert "cli-secret" not in text and "not exported" not in text
    assert before == {p.relative_to(session_dir): p.read_bytes()
                      for p in session_dir.rglob("*") if p.is_file()}
    code, text = _main(monkeypatch, capsys, *args, "--capture", "--confirm-digest", preview["digest"])
    assert code == 0
    capture = json.loads(text)
    snapshot = session_dir / DIAGNOSTIC_DIR / (capture["snapshot_id"] + ".json")
    assert json.loads(snapshot.read_text())["preview"]["json"] == preview["json"]


@pytest.mark.parametrize("flags", [["--max-turns", "1"], ["--deadline-sec", "0"],
                                   ["--deadline-sec", "nan"], ["--max-run-tokens", "5000"],
                                   ["--max-run-cost-usd", "1"]])
def test_bg_run_runtime_acp_refuses_limits_at_admission(tmp_path, monkeypatch, capsys, flags):
    """#712: `--bg` refuses an unenforceable ACP limit before queueing, with the
    foreground run's message, instead of queueing a session that later fails."""
    from garuda.interfaces import bg_sessions

    ws = _git_workspace(tmp_path)
    marker = tmp_path / "launched.txt"
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", marker=marker)
    _trusted_settings(tmp_path, monkeypatch)
    spawned = []

    def no_worker(session_id, **_kwargs):
        spawned.append(session_id)
        return type("NoWorker", (), {"pid": os.getpid(), "args": ["worker", session_id]})()

    monkeypatch.setattr(bg_sessions, "_spawn_worker", no_worker)
    argv = ("run", "-t", "must not run", "--workspace", str(ws), "--runtime", "fakeacp", *flags)
    foreground = _main(monkeypatch, capsys, *argv)
    background = _main(monkeypatch, capsys, *argv, "--bg")
    assert foreground[0] == background[0] == 2, background
    assert flags[0] in background[1]
    assert background[1].strip() == foreground[1].strip()
    assert not spawned and not marker.exists()
    assert SessionStore().list_sessions() == []


_ROUTES = {
    "default": "routing:\n  default_runtime: fakeacp\n",
    "rule": ("routing:\n  rules:\n    - id: build-on-fake\n      runtime: fakeacp\n"
             "      when: {agents: [build]}\n"),
}


def _routed_settings(tmp_path, monkeypatch, route: str) -> None:
    settings = _trusted_settings(tmp_path, monkeypatch)
    settings.write_text(settings.read_text(encoding="utf-8") + route, encoding="utf-8")


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize("flags", [["--max-turns", "1"], ["--deadline-sec", "nan"],
                                   ["--max-run-cost-usd", "1"]])
def test_bg_run_routed_to_acp_refuses_limits_at_admission(tmp_path, monkeypatch, capsys, route,
                                                          flags):
    """#724: a `--bg` run that names no runtime but is routed to an ACP runtime
    by trusted selection (default or rule) refuses an unenforceable limit at
    admission, like the foreground run, instead of queueing a worker that fails."""
    from garuda.interfaces import bg_sessions

    ws = _git_workspace(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin")
    _routed_settings(tmp_path, monkeypatch, _ROUTES[route])
    spawned = []

    def no_worker(session_id, **_kwargs):
        spawned.append(session_id)
        return type("NoWorker", (), {"pid": os.getpid(), "args": ["worker", session_id]})()

    monkeypatch.setattr(bg_sessions, "_spawn_worker", no_worker)
    argv = ("run", "-t", "must not run", "--workspace", str(ws), *flags)
    foreground = _main(monkeypatch, capsys, *argv)
    background = _main(monkeypatch, capsys, *argv, "--bg")
    assert foreground[0] == background[0] == 2, background
    assert "'fakeacp'" in background[1] and flags[0] in background[1]
    assert background[1].strip() == foreground[1].strip()
    assert not spawned
    assert SessionStore().list_sessions() == []


def test_bg_run_limits_still_admitted_when_routing_stays_native(tmp_path, monkeypatch, capsys):
    """#724: the admission prediction refuses only an ACP route; a run routing
    leaves native keeps its enforceable limits and is queued."""
    from garuda.interfaces import bg_sessions

    ws = _git_workspace(tmp_path)
    _routed_settings(tmp_path, monkeypatch, "routing:\n  default_runtime: native\n")
    spawned = []
    monkeypatch.setattr(bg_sessions, "_spawn_worker", lambda session_id, **_kw: spawned.append(
        session_id) or type("NoWorker", (), {"pid": os.getpid(), "args": ["w", session_id]})())
    code, out = _main(monkeypatch, capsys, "run", "-t", "x", "--workspace", str(ws),
                      "--max-turns", "1", "--bg")
    assert code == 0, out
    assert len(spawned) == 1


async def test_bg_worker_refusal_settles_a_classifier_routed_session(tmp_path, monkeypatch):
    """#724: when an enabled classifier makes the route unknowable at admission,
    the run is queued; the worker's ACP limit refusal then settles the session
    as `refused`, with the foreground message, not a bare `failed`."""
    import argparse

    from garuda.interfaces import bg_sessions
    from garuda.runtime.queue import QueueStore

    ws = _git_workspace(tmp_path)
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin")
    # No model binding: the worker's classifier is skipped and the default
    # (fakeacp) selected — a decision admission cannot make for it.
    _routed_settings(tmp_path, monkeypatch,
                     "routing:\n  default_runtime: fakeacp\n  classifier: true\n")
    args = build_parser().parse_args(
        ["run", "-t", "must not run", "--workspace", str(ws), "--max-turns", "1", "--bg"])
    store = SessionStore(tmp_path / "sessions")
    queue = QueueStore(tmp_path / "queue")
    spawned = []
    session_id = bg_sessions.launch(
        argparse.Namespace(**vars(args)), store=store, queue=queue,
        spawn=lambda sid: spawned.append(sid) or type(
            "NoWorker", (), {"pid": os.getpid(), "args": ["w", sid]})())
    assert spawned == [session_id]
    code = await bg_sessions.run_worker_async(session_id, store=store, queue=queue)
    assert code == 2
    meta = store.load_meta(session_id)
    assert meta["status"] == "refused"
    assert (meta["state"]["work"], meta["state"]["outcome"]) == ("stopped", "refused")
    assert meta["background_refusal"] == "run.acp_limit_unenforceable"
    assert "--max-turns" in meta["refusal_reason"] and "'fakeacp'" in meta["refusal_reason"]
