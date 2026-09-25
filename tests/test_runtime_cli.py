"""Runtime CLI tests for issue #37 (P1.6).

Listing, inspection, explicit selection, handoff preview/confirm, and
recovery — in text and JSON — with the guarantee that previews mutate
nothing and switches need --confirm.
"""

import json
import sys

import pytest

from garuda.acp.catalog import adapter_for_manifest, builtin_manifest_dicts
from garuda.core.sessions import SessionStore
from garuda.interfaces.main import build_parser
from garuda.interfaces.runtime_cli import (
    cmd_handoff_preview,
    cmd_inspect,
    cmd_list,
    cmd_recover,
    load_configured_manifest_dicts,
)
from garuda.runtime.registry import parse_global_manifests


def _manifests():
    native = {
        "runtime_id": "native",
        "kind": "native",
        "version": "1.2.0",
        "description": "The in-process Garuda loop.",
    }
    return parse_global_manifests([native, *builtin_manifest_dicts()])


def test_parser_has_runtime_group_with_defaults():
    args = build_parser().parse_args(["run", "-t", "x"])
    assert args.runtime == "native"
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


def test_list_and_inspect_text_and_json():
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


def test_global_manifests_merge_and_validate():
    dicts = load_configured_manifest_dicts({"runtimes": []})
    assert len(dicts) == len(builtin_manifest_dicts()) + 1
    assert dicts[0]["runtime_id"] == "native"
    with pytest.raises(ValueError, match="must be a list"):
        load_configured_manifest_dicts({"runtimes": "nope"})


def test_product_registry_surfaces_and_refuses_disabled_runtime():
    from garuda.interfaces.runtime_cli import (
        acp_adapter_for_workspace,
        cmd_inspect_registry,
        cmd_list_registry,
        configured_registry,
    )

    extra = {
        "runtime_id": "blocked-agent",
        "kind": "acp",
        "command": ["blocked-agent", "acp"],
        "version": "1",
        "setup": "Install blocked-agent.",
    }
    registry = configured_registry(
        global_settings={"runtimes": [extra]},
        project_settings={},
        disabled=frozenset({"blocked-agent"}),
    )
    assert "disabled by user configuration" in cmd_list_registry(registry)
    assert "unavailable" in cmd_inspect_registry(registry, "blocked-agent")
    with pytest.raises(ValueError, match="disabled"):
        acp_adapter_for_workspace(
            ".",
            "blocked-agent",
            argv_override=_fake_argv(),
            disabled=frozenset({"blocked-agent"}),
            global_settings={"runtimes": [extra]},
            project_settings={},
        )


async def test_handoff_preview_mutates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")

    preview = cmd_handoff_preview(store, "s1", "codex")
    assert "--confirm" in preview
    assert store.load_unified("s1").handoff["state"] == "none"


def _fake_manifests():
    return [
        {
            "runtime_id": "fakevendor",
            "kind": "acp",
            "command": [
                sys.executable,
                "-m",
                "garuda.acp.fake_agent",
                "--profile",
                "success",
            ],
            "version": "1",
            "setup": "fake",
        }
    ]


def _fake_argv(profile="success"):
    return [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", profile]


def _checkpoint(store, session_id="s1", workspace=None):
    """Handoff/recovery requires a durable transcript before resuming."""
    store.checkpoint_messages(session_id, [])
    if workspace is not None:
        from garuda.workspace.diff import capture_baseline

        store.record_baseline(session_id, capture_baseline(workspace).to_dict())


async def test_handoff_confirm_runs_the_transaction(tmp_path, monkeypatch):
    from garuda.context.pack import ContextPackManager
    from garuda.interfaces.runtime_cli import cmd_handoff_confirm

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")
    _checkpoint(store, workspace=tmp_path)

    manager = ContextPackManager(store.session_dir("s1"))
    done = await cmd_handoff_confirm(
        store, "s1", "fakevendor",
        manifests=_fake_manifests(),
        workspace=str(tmp_path),
        pack_manager=manager,
        target_argv_override=_fake_argv("success"),
        disabled=frozenset(),
    )
    assert "acknowledged" in done
    assert store.load_unified("s1").handoff["state"] == "acknowledged"
    assert (store.session_dir("s1") / "handoff.md").is_file()


async def test_handoff_confirm_failure_rolls_back_to_source(tmp_path, monkeypatch):
    from garuda.interfaces.runtime_cli import cmd_handoff_confirm
    from garuda.runtime.handoff import HandoffError

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")
    _checkpoint(store, workspace=tmp_path)
    with pytest.raises(HandoffError, match="target startup failed"):
        await cmd_handoff_confirm(
            store, "s1", "fakevendor",
            manifests=_fake_manifests(),
            workspace=str(tmp_path),
            target_argv_override=_fake_argv("version-mismatch"),
            disabled=frozenset(),
        )
    assert store.load_unified("s1").handoff["state"] == "failed"


async def test_handoff_confirm_refuses_unknown_disabled_and_native(tmp_path, monkeypatch):
    from garuda.interfaces.runtime_cli import cmd_handoff_confirm
    from garuda.runtime import RegistryError

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")
    _checkpoint(store)
    with pytest.raises(RegistryError, match="unknown runtime"):
        await cmd_handoff_confirm(
            store, "s1", "ghost", manifests=_fake_manifests(), disabled=frozenset()
        )
    with pytest.raises(RegistryError, match="disabled"):
        await cmd_handoff_confirm(
            store, "s1", "fakevendor",
            manifests=_fake_manifests(), disabled=frozenset({"fakevendor"}),
        )
    refused = await cmd_handoff_confirm(
        store, "s1", "native", manifests=_fake_manifests(), disabled=frozenset()
    )
    assert "resume" in refused
    assert store.load_unified("s1").handoff["state"] == "none"


async def test_handoff_confirm_refuses_missing_executable_before_moving(tmp_path, monkeypatch):
    from garuda.acp.catalog import AcpUnavailableError
    from garuda.interfaces.runtime_cli import cmd_handoff_confirm

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")
    _checkpoint(store)
    missing = [
        {
            "runtime_id": "ghost",
            "kind": "acp",
            "command": ["definitely-not-installed-xyz"],
            "version": "1",
            "setup": "Install ghost.",
        }
    ]
    with pytest.raises(AcpUnavailableError, match="Install ghost"):
        await cmd_handoff_confirm(
            store, "s1", "ghost", manifests=missing, disabled=frozenset()
        )
    assert store.load_unified("s1").handoff["state"] == "none"


async def test_resume_command_continues_a_persisted_session(tmp_path, monkeypatch):
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.interfaces.runtime_cli import cmd_resume
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    (tmp_path / "ws").mkdir()
    store = SessionStore()

    def _script(summary):
        return ScriptModel(
            responses=[
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="1", name="task_complete", arguments={"summary": summary})
                    ],
                )
            ]
        )

    def _stack(model):
        return dict(
            model=model,
            agent=DefaultAgent(),
            tools=tools_for_names(["task_complete"]),
            config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
            permissions=PermissionEngine(mode="yolo"),
            workspace=str(tmp_path / "ws"),
        )

    from garuda.interfaces.runner import run_agent_task

    first = await run_agent_task(
        task="first task",
        events=EventStore(session_id="rs-first"),
        store=store,
        **_stack(_script("one")),
    )
    assert first.success
    summary = await cmd_resume(
        store=store, session_id="rs-first", task="second task", **_stack(_script("two"))
    )
    assert "resumed rs-first as " in summary
    assert "success" in summary


async def test_recover_command_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")
    _checkpoint(store)
    text = cmd_recover(store, "s1")
    assert "resumable" in text
    report = json.loads(cmd_recover(store, "s1", as_json=True))
    assert report["state"] == "resumable"


async def test_acp_run_streams_through_fake(monkeypatch):
    manifests = {m.runtime_id: m for m in _manifests()}
    assert manifests["claude"].command == ("claude-agent-acp",)
    import garuda.interfaces.runtime_cli as cli

    monkeypatch.setattr(
        cli,
        "acp_adapter_for_workspace",
        lambda *_args, **_kwargs: (
            None,
            adapter_for_manifest(manifests["claude"], argv_override=_fake_argv("success")),
        ),
    )
    summary = await cli.run_acp_task("hello via cli", runtime_id="fakevendor")
    assert summary["turn"] == 1
    assert summary["events"] >= 2
