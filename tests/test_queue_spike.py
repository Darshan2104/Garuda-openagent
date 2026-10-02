"""Durable queue and lock spike, proven with real processes (#153, plan task A.4).

A single-process fake queue is not evidence: every contention case here runs in
separate OS processes started through ``subprocess``, importing this checkout.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from garuda.runtime.queue_proto import (
    CorruptState,
    LockUnavailable,
    Owner,
    QueueStore,
    current_owner,
    owner_liveness,
)

ROOT = Path(__file__).resolve().parents[1]


def _python(code: str, *args: str, **kwargs) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )


WORKER = """
import json, sys, time
from garuda.runtime.queue_proto import QueueStore
root, scope, item, log = sys.argv[1:5]
store = QueueStore(root)
assert store.claim(scope, item, timeout=60)
with open(log, "a") as f:
    f.write(json.dumps({"item": item, "event": "start", "t": time.time()}) + "\\n")
time.sleep(0.15)
with open(log, "a") as f:
    f.write(json.dumps({"item": item, "event": "end", "t": time.time()}) + "\\n")
store.release(scope, item)
"""


def _max_overlap(log: Path) -> int:
    events = [json.loads(line) for line in log.read_text().splitlines()]
    events.sort(key=lambda e: (e["t"], 0 if e["event"] == "end" else 1))
    running = peak = 0
    for event in events:
        running += 1 if event["event"] == "start" else -1
        peak = max(peak, running)
    return peak


def test_concurrent_processes_never_exceed_the_ceiling(tmp_path):
    store = QueueStore(tmp_path / "q")
    store.configure("user:codex", 2)
    items = [store.enqueue("user:codex", f"job{i}") for i in range(8)]
    log = tmp_path / "log.jsonl"

    workers = [_python(WORKER, str(tmp_path / "q"), "user:codex", item, str(log)) for item in items]
    for worker in workers:
        assert worker.wait(timeout=120) == 0, worker.stderr.read()

    assert _max_overlap(log) <= 2
    assert len(log.read_text().splitlines()) == 16
    assert store.snapshot()["scopes"]["user:codex"]["claims"] == {}


def test_claims_happen_in_fifo_order_within_a_scope(tmp_path):
    store = QueueStore(tmp_path / "q")
    store.configure("s", 1)
    items = [store.enqueue("s", name) for name in ("first", "second", "third")]
    log = tmp_path / "log.jsonl"

    # Started in reverse: order must come from the queue, not from who asked first.
    workers = [_python(WORKER, str(tmp_path / "q"), "s", item, str(log)) for item in reversed(items)]
    for worker in workers:
        assert worker.wait(timeout=60) == 0, worker.stderr.read()

    starts = [json.loads(line)["item"] for line in log.read_text().splitlines() if '"start"' in line]
    assert starts == ["first", "second", "third"]


def test_a_cancelled_entry_is_skipped(tmp_path):
    store = QueueStore(tmp_path / "q")
    a, b = store.enqueue("s", "a"), store.enqueue("s", "b")
    assert store.cancel("s", a)
    assert store.try_claim("s", b)


def test_a_waiter_claims_soon_after_release(tmp_path):
    store = QueueStore(tmp_path / "q")
    holder = store.enqueue("s", "holder")
    assert store.try_claim("s", holder)
    store.enqueue("s", "waiter")
    waiter = _python(
        """
        import sys, time
        from garuda.runtime.queue_proto import QueueStore
        t0 = time.monotonic()
        ok = QueueStore(sys.argv[1]).claim("s", "waiter", timeout=30)
        print(ok, time.monotonic() - t0)
        """,
        str(tmp_path / "q"),
    )
    time.sleep(0.5)
    released_at = time.monotonic()
    store.release("s", holder)
    out, err = waiter.communicate(timeout=30)
    ok, _waited = out.split()
    assert ok == "True", err
    assert time.monotonic() - released_at < 2.0


def test_a_killed_owners_claim_is_reclaimed_only_after_expiry(tmp_path):
    root = tmp_path / "q"
    store = QueueStore(root, lease_ttl=0.5)
    store.enqueue("s", "victim")
    victim = _python(
        """
        import sys, time
        from garuda.runtime.queue_proto import QueueStore
        assert QueueStore(sys.argv[1]).claim("s", "victim", timeout=10)
        print("claimed", flush=True)
        time.sleep(60)
        """,
        str(root),
        start_new_session=True,
    )
    assert victim.stdout.readline().strip() == "claimed"
    os.killpg(victim.pid, signal.SIGKILL)
    victim.wait()
    store.enqueue("s", "next")

    assert not store.try_claim("s", "next")  # within TTL: not yet reclaimable
    time.sleep(0.6)
    assert store.try_claim("s", "next")  # expired and confirmed dead


def test_a_live_owner_past_its_ttl_keeps_its_claim(tmp_path):
    store = QueueStore(tmp_path / "q", lease_ttl=0.1)
    store.enqueue("s", "mine")
    assert store.try_claim("s", "mine")  # owned by this live test process
    store.enqueue("s", "other")
    time.sleep(0.2)

    assert not store.try_claim("s", "other")
    assert "mine" in store.snapshot()["scopes"]["s"]["claims"]


def test_a_reused_pid_is_not_mistaken_for_the_owner():
    me = current_owner()
    impostor = Owner(pid=me.pid, identity=me.identity + "-earlier-process", pgid=me.pgid, epoch="old")
    assert owner_liveness(me.to_dict()) is True
    assert owner_liveness(impostor.to_dict()) is False


def test_unknown_liveness_is_quarantined_not_taken_over(tmp_path):
    store = QueueStore(tmp_path / "q", lease_ttl=0.05, liveness=lambda owner: None)
    store.enqueue("s", "ghost")
    assert store.try_claim("s", "ghost")
    store.enqueue("s", "next")
    time.sleep(0.1)

    assert not store.try_claim("s", "next")
    assert store.snapshot()["scopes"]["s"]["claims"]["ghost"]["quarantined"] is True


def test_an_unlockable_store_refuses_instead_of_writing(tmp_path, monkeypatch):
    import fcntl

    store = QueueStore(tmp_path / "q")

    def broken(fd, op):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(fcntl, "flock", broken)
    with pytest.raises(LockUnavailable):
        store.enqueue("s", "x")
    assert not (tmp_path / "q" / "state.json").exists()


def test_a_symlinked_lock_or_state_file_refuses(tmp_path):
    root = tmp_path / "q"
    store = QueueStore(root)
    store.enqueue("s", "x")
    target = tmp_path / "elsewhere.json"
    target.write_text((root / "state.json").read_text())
    (root / "state.json").unlink()
    (root / "state.json").symlink_to(target)

    with pytest.raises(CorruptState):
        store.enqueue("s", "y")


def test_storage_is_owner_only(tmp_path):
    store = QueueStore(tmp_path / "q")
    store.enqueue("s", "x")
    assert oct((tmp_path / "q").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "q" / "state.json").stat().st_mode & 0o777) == "0o600"


def test_an_unavailable_workspace_gives_the_slot_back(tmp_path):
    store = QueueStore(tmp_path / "q")
    store.configure("s", 1)
    store.enqueue("s", "blocked")
    store.enqueue("s", "after")

    assert not store.claim_with_workspace("s", "blocked", lambda: False)
    state = store.snapshot()["scopes"]["s"]
    assert state["claims"] == {}
    assert [w["id"] for w in state["waiting"]] == ["blocked", "after"]
    assert store.claim_with_workspace("s", "blocked", lambda: True)
