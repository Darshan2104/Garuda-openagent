"""Lease tests for issue #26 (P0.16).

Concurrent acquisition, heartbeat loss, stale recovery across owners, corrupt
leases failing closed, user files untouched throughout, and worktree
isolation keys.
"""

import threading

import pytest

from garuda.workspace.lease import (
    LeaseConflictError,
    LeaseError,
    LeaseStore,
    create_worktree,
    is_worktree,
    workspace_key,
)


def _store(tmp_path) -> LeaseStore:
    return LeaseStore(tmp_path / "leases")


def test_concurrent_mutating_acquisition_one_wins(tmp_path):
    store = _store(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    winners: list[str] = []
    errors: list[BaseException] = []

    def contender(session_id: str) -> None:
        try:
            store.acquire(workspace, session_id, "mutating")
            winners.append(session_id)
        except (LeaseConflictError, LeaseError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=contender, args=(f"s{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert len(errors) == 7
    assert all(isinstance(e, LeaseConflictError) for e in errors)


def test_read_only_shares_and_never_blocks_mutation_end(tmp_path):
    store = _store(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store.acquire(workspace, "mutator", "mutating")
    store.acquire(workspace, "reader-a", "read-only")
    store.acquire(workspace, "reader-b", "read-only")
    holders = {h.session_id: h.mode for h in store.holders_of(workspace)}
    assert holders == {"mutator": "mutating", "reader-a": "read-only", "reader-b": "read-only"}
    store.release(workspace, "mutator")
    store.acquire(workspace, "mutator-2", "mutating")
    assert {h.session_id for h in store.holders_of(workspace)} == {
        "reader-a",
        "reader-b",
        "mutator-2",
    }


def test_heartbeat_loss_and_stale_recovery(tmp_path):
    store = _store(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lease = store.acquire(workspace, "old", "mutating", ttl_sec=1000)
    assert not lease.is_stale()
    stale = store.acquire(workspace, "old", "mutating", ttl_sec=1000, now=lease.heartbeat_at)
    assert stale.session_id == "old"

    # Another session cannot steal a live lease...
    with pytest.raises(LeaseConflictError, match="mutably held"):
        store.acquire(workspace, "new", "mutating")
    # ...but heartbeat loss past TTL recovers safely, recording the takeover.
    recovered = store.acquire(workspace, "new", "mutating", now=lease.heartbeat_at + 1001)
    assert recovered.stolen_from == "old"
    assert [h.session_id for h in store.holders_of(workspace)] == ["new"]
    # The old owner's heartbeat now fails closed instead of clobbering.
    with pytest.raises(LeaseConflictError):
        store.heartbeat(workspace, "old")


def test_corrupt_lease_fails_closed(tmp_path):
    store = _store(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    key = workspace_key(workspace)
    import hashlib

    path = store.root / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LeaseError, match="refusing"):
        store.acquire(workspace, "s1", "mutating")
    with pytest.raises(LeaseError, match="refusing"):
        store.holders_of(workspace)


def test_conflicts_never_touch_user_files(tmp_path):
    store = _store(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    precious = workspace / "work.txt"
    precious.write_text("do not touch", encoding="utf-8")
    store.acquire(workspace, "owner", "mutating")
    with pytest.raises(LeaseConflictError):
        store.acquire(workspace, "intruder", "mutating")
    with pytest.raises(LeaseConflictError):
        store.release(workspace, "intruder")
    assert precious.read_text(encoding="utf-8") == "do not touch"


def test_worktree_keys_isolate_parallel_runs(tmp_path):
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    assert workspace_key(main) != workspace_key(linked)
    assert workspace_key(str(main) + "/.") == workspace_key(main)
    store = _store(tmp_path)
    store.acquire(main, "s-main", "mutating")
    store.acquire(linked, "s-linked", "mutating")
    assert [h.session_id for h in store.holders_of(main)] == ["s-main"]
    assert [h.session_id for h in store.holders_of(linked)] == ["s-linked"]


def test_worktree_helpers_against_real_git(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert is_worktree(repo) is False
    helix = tmp_path / "plain"
    helix.mkdir()
    assert is_worktree(helix) is False
    with pytest.raises(LeaseError):
        create_worktree(repo, tmp_path / "wt")
    assert workspace_key(helix) == str(helix.resolve())


def test_heartbeat_and_release_edges(tmp_path):
    store = _store(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(LeaseError, match="session_id is required"):
        store.acquire(workspace, "", "mutating")
    with pytest.raises(LeaseError, match="mode must be"):
        store.acquire(workspace, "s", "dancing")
    store.acquire(workspace, "s", "mutating")
    refreshed = store.heartbeat(workspace, "s")
    assert refreshed.session_id == "s"
    store.release(workspace, "s")
    assert store.holders_of(workspace) == []
    store.release(workspace, "s")
