"""Unified session tests for issue #13 (P0.6).

Round-trip, crash-mid-write, concurrent-meta-write, and migration fixtures:
legacy sessions resume, failed migration or writes leave the original readable,
and unknown future versions fail with an actionable error.
"""

import json
import threading

import pytest

import garuda.core.sessions as sessions_mod
from garuda.core.sessions import SessionStore
from garuda.runtime.session import (
    SESSION_SCHEMA_VERSION,
    RuntimeSegment,
    UnifiedSessionError,
    migrate_legacy_meta,
)


def _begin(store: SessionStore, session_id: str = "s1") -> None:
    store.begin(
        session_id, task="probe", model="m", agent="build", workspace="/tmp/ws"
    )


def test_round_trip_runtime_identity_and_cursors(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    unified = store.ensure_unified("s1")
    assert unified.schema_version == SESSION_SCHEMA_VERSION
    assert unified.active.runtime_id == "native"
    assert unified.active.native_session_id == "s1"
    assert unified.handoff["state"] == "none"

    store.attach_runtime_segment(
        "s1",
        RuntimeSegment(
            runtime_id="codex",
            kind="acp",
            native_session_id="acp-9",
            version="1.0",
            capabilities=frozenset({"prompt"}),
        ),
    )
    store.record_baseline("s1", {"commit": "abc", "dirty": False})
    store.record_handoff("s1", state="prepared", attempts=1, target_runtime="codex")
    store.advance_event_cursor("s1", 7)

    unified = store.load_unified("s1")
    assert [s.runtime_id for s in unified.segments] == ["native", "codex"]
    assert unified.active.native_session_id == "acp-9"
    assert unified.active.event_cursor == 7
    assert unified.baseline == {"commit": "abc", "dirty": False}
    assert unified.handoff["state"] == "prepared"
    assert unified.handoff["attempts"] == 1
    assert unified.handoff["target_runtime"] == "codex"
    assert unified.legacy["task"] == "probe"


def test_legacy_session_resumes_without_migration_on_disk(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    meta = store.load_meta("s1")
    assert "schema_version" not in meta
    unified = store.load_unified("s1")
    assert unified.active.native_session_id == "s1"
    assert store.load_meta("s1").get("schema_version") is None


def test_cursor_regression_and_bad_handoff_fail_closed(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    store.advance_event_cursor("s1", 3)
    with pytest.raises(ValueError, match="regress"):
        store.advance_event_cursor("s1", 2)
    with pytest.raises(ValueError, match="Unknown handoff state"):
        store.record_handoff("s1", state="teleported")
    with pytest.raises(ValueError):
        store.record_baseline("s1", ["not", "a", "mapping"])


def test_unknown_future_version_is_actionable(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.update_meta("s1", {"schema_version": SESSION_SCHEMA_VERSION + 99})
    with pytest.raises(UnifiedSessionError, match="upgrade Garuda"):
        store.load_unified("s1")


def test_migration_never_mutates_its_input():
    legacy = {"session_id": "s9", "task": "t"}
    snapshot = dict(legacy)
    upgraded = migrate_legacy_meta(legacy)
    assert legacy == snapshot
    assert upgraded["schema_version"] == SESSION_SCHEMA_VERSION
    assert upgraded["runtime_segments"][0]["native_session_id"] == "s9"
    with pytest.raises(UnifiedSessionError):
        migrate_legacy_meta({"task": "no id"})


def test_failed_write_leaves_original_readable(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    _begin(store)
    before = (store.session_dir("s1") / "meta.json").read_text(encoding="utf-8")

    def _boom(path, text):
        raise OSError("disk is gone")

    monkeypatch.setattr(sessions_mod, "_atomic_write_text", _boom)
    with pytest.raises(OSError):
        store.ensure_unified("s1")
    after = (store.session_dir("s1") / "meta.json").read_text(encoding="utf-8")
    assert after == before
    assert json.loads(after)["task"] == "probe"


def test_concurrent_meta_writes_lose_nothing(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            for _ in range(10):
                store.update_meta("s1", {f"field_{i}": i})
        except BaseException as exc:  # noqa: BLE001 - collected, then re-raised
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    store.advance_event_cursor("s1", 5)
    store.advance_event_cursor("s1", 5)
    meta = store.load_meta("s1")
    assert all(meta.get(f"field_{i}") == i for i in range(4))
    assert isinstance(meta["runtime_segments"], list) and meta["runtime_segments"]
