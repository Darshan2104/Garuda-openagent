"""A context fork of an ACP harness or flow session names why it cannot fork (#770)."""

import json

import pytest

from garuda.core.session_fork import ForkSourceNotNative, prepare_context_fork
from garuda.core.session_records import ReadOnlySessions
from garuda.core.sessions import SessionStore
from garuda.runtime.session import RuntimeSegment

SOURCE = "5408e404-7b90-467f-918a-3aa11a2579aa"


def _acp_source(tmp_path, *, segment=True):
    """An ACP session as the harness path leaves it: meta, metrics and ACP events only."""
    store = SessionStore(tmp_path / "sessions")
    store.begin(
        SOURCE, task="describe calc", model="acp:claude", agent="claude", workspace=str(tmp_path),
        runtime_segment=RuntimeSegment(runtime_id="claude", kind="acp", version="0.110.1") if segment else None,
    )
    store.update_meta(SOURCE, {"status": "completed", "turns": 0})
    directory = store.session_dir(SOURCE)
    (directory / "acp-events.jsonl").write_text("")
    (directory / "metrics.json").write_text("{}")
    assert not (directory / "messages.json").exists()
    return store


def _assert_acp_refusal(text):
    assert "record is missing, unreadable or symlinked" not in text
    assert "fork.source_not_native" in text
    assert f"ACP session {SOURCE} (runtime claude) has no message history to fork" in text
    assert "native sessions only" in text


@pytest.mark.parametrize("segment", [True, False], ids=["unified", "legacy-model"])
def test_prepare_refuses_acp_source_with_typed_error(tmp_path, segment):
    store = _acp_source(tmp_path, segment=segment)
    with pytest.raises(ForkSourceNotNative) as caught:
        prepare_context_fork(ReadOnlySessions(store.root), SOURCE, 0)
    _assert_acp_refusal(str(caught.value))


def test_prepare_refuses_flow_source(tmp_path):
    store = _acp_source(tmp_path)
    store.update_meta(SOURCE, {"kind": "flow", "runtime_segments": []})
    with pytest.raises(ForkSourceNotNative, match="flow session .* has no message history"):
        prepare_context_fork(ReadOnlySessions(store.root), SOURCE, 0)


@pytest.mark.parametrize("reference", [SOURCE[:8], SOURCE])
def test_fork_preview_cli_names_acp_source(tmp_path, monkeypatch, capsys, reference):
    from garuda.interfaces import main

    store = _acp_source(tmp_path)
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(store.root))
    before = {str(p): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    args = main.build_parser().parse_args([
        "sessions", "fork-preview", reference, "--at", "0", "--workspace", str(tmp_path),
    ])
    assert main.run_sessions(args) == 2
    _assert_acp_refusal(capsys.readouterr().err)
    assert {str(p): p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before


async def test_run_fork_names_acp_source(tmp_path, monkeypatch, capsys):
    from garuda.interfaces import main

    store = _acp_source(tmp_path)
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(store.root))
    sessions = {p.name for p in store.root.iterdir()}
    args = main.build_parser().parse_args([
        "run", "--workspace", str(tmp_path), "--fork", SOURCE[:8], "--fork-at", "0", "-t", "alternative",
    ])
    from garuda.model.script_model import ScriptModel

    args.model = ScriptModel(responses=[])
    assert await main.run_task_guarded(args) == 2
    _assert_acp_refusal(capsys.readouterr().err)
    assert {p.name for p in store.root.iterdir()} == sessions
    assert json.loads((store.session_dir(SOURCE) / "meta.json").read_text())["status"] == "completed"
