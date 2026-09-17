"""Runtime CLI tests for issue #37 (P1.6).

Listing, inspection, explicit selection, handoff preview/confirm, and
recovery — in text and JSON — with the guarantee that previews mutate
nothing and switches need --confirm.
"""

import json
import sys

import pytest

from garuda.acp.catalog import builtin_manifest_dicts
from garuda.core.sessions import SessionStore
from garuda.interfaces.main import build_parser
from garuda.interfaces.runtime_cli import (
    cmd_handoff_confirm,
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


async def test_handoff_preview_mutates_nothing_confirm_prepares(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin("s1", task="move it", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")

    preview = cmd_handoff_preview(store, "s1", "codex")
    assert "--confirm" in preview
    assert store.load_unified("s1").handoff["state"] == "none"

    from garuda.context.pack import ContextPackManager

    manager = ContextPackManager(store.session_dir("s1"))
    done = await cmd_handoff_confirm(store, "s1", "codex", pack_manager=manager)
    assert "prepared" in done
    assert store.load_unified("s1").handoff["state"] == "prepared"
    assert (store.session_dir("s1") / "handoff.md").is_file()


async def test_recover_command_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")
    text = cmd_recover(store, "s1")
    assert "resumable" in text
    report = json.loads(cmd_recover(store, "s1", as_json=True))
    assert report["state"] == "resumable"


async def test_acp_run_streams_through_fake():
    manifests = {m.runtime_id: m for m in _manifests()}
    assert manifests["claude"].command == ("claude-agent-acp",)
    import garuda.interfaces.runtime_cli as cli

    fake_manifest = parse_global_manifests(
        [
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
    )[0]
    summary = await cli.run_acp_task(fake_manifest, "hello via cli")
    assert summary["turn"] == 1
    assert summary["events"] >= 2
