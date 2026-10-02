"""The durable session queue, proven with real processes (#153 A.4, promoted by #167 D.1).

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

from garuda.runtime.queue import (
    CorruptState,
    LockUnavailable,
    Owner,
    QueueStore,
    current_owner,
    owner_liveness,
)

ROOT = Path(__file__).resolve().parents[1]


def ceilings(**limits):
    """Capacity ceilings come from the shared settings, never from the queue."""
    import yaml

    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({"capacity": limits}))


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
from garuda.runtime.queue import QueueStore
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
    ceilings(codex=2)
    store = QueueStore(tmp_path / "q")
    items = [store.enqueue("user:codex", f"job{i}") for i in range(8)]
    log = tmp_path / "log.jsonl"

    workers = [_python(WORKER, str(tmp_path / "q"), "user:codex", item, str(log)) for item in items]
    for worker in workers:
        assert worker.wait(timeout=120) == 0, worker.stderr.read()

    assert _max_overlap(log) <= 2
    assert len(log.read_text().splitlines()) == 16
    assert store.snapshot()["scopes"]["user:codex"]["claims"] == {}


def test_claims_happen_in_fifo_order_within_a_scope(tmp_path):
    ceilings(s=1)
    store = QueueStore(tmp_path / "q")
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
    ceilings(s=1)
    store = QueueStore(tmp_path / "q")
    holder = store.enqueue("s", "holder")
    assert store.try_claim("s", holder)
    store.enqueue("s", "waiter")
    waiter = _python(
        """
        import sys, time
        from garuda.runtime.queue import QueueStore
        queue = QueueStore(sys.argv[1])
        print("waiting", flush=True)
        t0 = time.monotonic()
        ok = queue.claim("s", "waiter", timeout=30)
        print(ok, time.monotonic() - t0)
        """,
        str(tmp_path / "q"),
    )
    assert waiter.stdout.readline().strip() == "waiting"  # imports done, polling begins
    time.sleep(0.5)
    released_at = time.monotonic()
    store.release("s", holder)
    out, err = waiter.communicate(timeout=30)
    ok, _waited = out.split()
    assert ok == "True", err
    assert time.monotonic() - released_at < 3.0


def test_a_killed_owners_claim_is_reclaimed_on_proof_of_death(tmp_path):
    root = tmp_path / "q"
    ceilings(s=1)
    store = QueueStore(root, lease_ttl=0.5)
    store.enqueue("s", "victim")
    victim = _python(
        """
        import sys, time
        from garuda.runtime.queue import QueueStore
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

    assert store.try_claim("s", "next")  # confirmed dead: the slot is taken at once
    assert "victim" not in store.snapshot()["scopes"]["s"]["claims"]


def test_a_live_owner_past_its_ttl_keeps_its_claim(tmp_path):
    ceilings(s=1)
    store = QueueStore(tmp_path / "q", lease_ttl=0.1)
    store.enqueue("s", "mine")
    assert store.try_claim("s", "mine")  # owned by this live test process
    store.enqueue("s", "other")
    time.sleep(0.2)

    assert not store.try_claim("s", "other")
    assert "mine" in store.snapshot()["scopes"]["s"]["claims"]


def test_a_reused_pid_is_not_mistaken_for_the_owner():
    me = current_owner()
    earlier = ("ps:Thu Jan 1 00:00:00 1970 earlier" if me.identity.startswith("ps:")
               else me.identity + "-earlier-process")  # another start time, same pid
    impostor = Owner(pid=me.pid, identity=earlier, pgid=me.pgid, epoch="old")
    assert owner_liveness(me.to_dict()) is True
    assert owner_liveness(impostor.to_dict()) is False


def test_unknown_liveness_is_quarantined_not_taken_over(tmp_path):
    ceilings(s=1)
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
    ceilings(s=1)
    store = QueueStore(tmp_path / "q")
    store.enqueue("s", "blocked")
    store.enqueue("s", "after")

    assert not store.claim_with_workspace("s", "blocked", lambda: False)
    state = store.snapshot()["scopes"]["s"]
    assert state["claims"] == {}
    assert [w["id"] for w in state["waiting"]] == ["blocked", "after"]
    assert store.claim_with_workspace("s", "blocked", lambda: True)


# --- D.1: the production store -------------------------------------------------------------


def _store(tmp_path, **kwargs):
    return QueueStore(tmp_path / "q", **kwargs)


def test_there_is_no_queue_only_capacity(tmp_path):
    assert not hasattr(_store(tmp_path), "configure")


def test_capacity_is_shared_with_foreground_launches(tmp_path):
    from garuda.runtime.capacity import CapacityStore, CapacityUnavailable

    ceilings(claude=1)
    capacity = CapacityStore()
    store = _store(tmp_path)
    scope = "u:claude"
    store.enqueue(scope, "bg")
    held = capacity.reserve("claude", "foreground-run", 1)  # a foreground run holds the slot
    assert not store.try_claim(scope, "bg")
    capacity.release(held)
    assert store.try_claim(scope, "bg")
    with pytest.raises(CapacityUnavailable):  # and now the queue's claim blocks a foreground run
        capacity.reserve("claude", "another-foreground-run", 1)
    assert store.release(scope, "bg")
    capacity.reserve("claude", "another-foreground-run", 1)  # released on every path


def test_ceilings_come_from_the_users_harness_settings(tmp_path):
    import yaml

    from garuda.config import garuda_yaml

    path = garuda_yaml.user_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "harnesses": {"codex": {"max_parallel": 1}}}))
    store = _store(tmp_path)
    store.enqueue("u:codex", "a")
    store.enqueue("u:codex", "b")
    assert store.try_claim("u:codex", "a")
    assert not store.try_claim("u:codex", "b")  # max_parallel: 1
    ceilings(codex=2)  # an explicit settings entry wins over garuda.yaml
    assert store.try_claim("u:codex", "b")


def test_entries_and_claims_are_bound_to_user_harness_session_worker_and_config(tmp_path):
    ceilings(claude=2)
    store = _store(tmp_path)
    store.enqueue("u:claude", "one", harness="claude", user="u", session_id="sess-1",
                  config_digest="abc")
    store.enqueue("u:claude", "two", harness="claude", user="u", session_id="sess-2",
                  config_digest="def")
    first, second = store.entries()
    assert (first["state"], first["position"], first["session_id"], first["config_digest"]) == \
        ("queued", 1, "sess-1", "abc")
    assert second["position"] == 2 and second["user"] == "u" and second["harness"] == "claude"
    owner = current_owner()
    assert store.try_claim("u:claude", "one", owner)
    running = [e for e in store.entries() if e["state"] == "running"][0]
    assert running["session_id"] == "sess-1" and running["config_digest"] == "abc"
    assert running["worker"]["pid"] == os.getpid() and running["worker"]["epoch"] == owner.epoch


def test_only_the_worker_that_claimed_may_release_or_heartbeat(tmp_path):
    store = _store(tmp_path)
    store.enqueue("s", "x")
    owner = current_owner()
    stranger = current_owner()
    assert store.try_claim("s", "x", owner)
    assert not store.heartbeat("s", "x", stranger) and not store.release("s", "x", stranger)
    assert store.heartbeat("s", "x", owner) and store.release("s", "x", owner)


def test_scopes_are_independent_queues(tmp_path):
    ceilings(a=1, b=1)
    store = _store(tmp_path)
    store.enqueue("u:a", "a1")
    store.enqueue("u:b", "b1")
    assert store.try_claim("u:b", "b1")  # not stuck behind another harness's queue
    assert store.try_claim("u:a", "a1")


def test_inspection_is_read_only_and_does_not_wait_for_the_lock(tmp_path):
    from garuda.runtime.strict_store import exclusive_lock

    store = _store(tmp_path)
    store.enqueue("s", "x")
    before = (tmp_path / "q" / "state.json").read_bytes()
    with exclusive_lock(tmp_path / "q"):  # a writer holds the lock
        assert [e["id"] for e in store.entries()] == ["x"]
    assert (tmp_path / "q" / "state.json").read_bytes() == before


# migration ------------------------------------------------------------------------------------


V1 = {"version": 1, "seq": 3, "scopes": {"u:claude": {
    "capacity": 2,
    "waiting": [{"id": "w1", "seq": 2}, {"id": "w2", "seq": 3}],
    "claims": {"c1": {"owner": {"pid": 1, "identity": "x", "pgid": 1, "epoch": "e"},
                      "epoch": "z", "heartbeat": 1.0}}}}}


def test_a_version_1_document_is_read_in_place_and_migrated_on_the_first_write(tmp_path):
    root = tmp_path / "q"
    root.mkdir(mode=0o700)
    document = root / "state.json"
    document.write_text(json.dumps(V1))
    os.chmod(document, 0o600)
    store = _store(tmp_path)

    assert [e["id"] for e in store.entries() if e["state"] == "queued"] == ["w1", "w2"]
    assert json.loads(document.read_text())["version"] == 1  # inspection never migrates

    store.enqueue("u:claude", "w3")
    migrated = json.loads(document.read_text())
    assert migrated["version"] == 2 and "capacity" not in migrated["scopes"]["u:claude"]
    assert [w["id"] for w in migrated["scopes"]["u:claude"]["waiting"]] == ["w1", "w2", "w3"]
    assert json.loads((root / "state.json.v1").read_text()) == V1  # kept once, untouched
    assert "c1" in migrated["scopes"]["u:claude"]["claims"]


@pytest.mark.parametrize("content", [
    "{not json", "[1, 2]", json.dumps({"version": 99, "scopes": {}}),
    json.dumps({"version": 2, "seq": 0, "scopes": {"s": {"waiting": "no", "claims": {}}}}),
    json.dumps({"version": 2, "seq": 0, "scopes": {"s": {"waiting": [{"nope": 1}], "claims": {}}}}),
])
def test_a_corrupt_or_unknown_record_is_refused_and_left_in_place(tmp_path, content):
    root = tmp_path / "q"
    root.mkdir(mode=0o700)
    (root / "state.json").write_text(content)
    os.chmod(root / "state.json", 0o600)
    store = _store(tmp_path)
    for action in (lambda: store.enqueue("s", "x"), store.entries, lambda: store.try_claim("s", "x")):
        with pytest.raises(CorruptState):
            action()
    assert (root / "state.json").read_text() == content


# clock skew -------------------------------------------------------------------------------------


def test_a_clock_that_moved_cannot_take_a_live_owners_claim(tmp_path):
    ceilings(s=1)
    clock = [1_000.0]
    store = _store(tmp_path, lease_ttl=1.0, clock=lambda: clock[0])
    store.enqueue("s", "mine")
    store.enqueue("s", "other")
    assert store.try_claim("s", "mine")
    for jump in (10_000.0, -5_000.0):  # far forward, then far back
        clock[0] += jump
        assert not store.try_claim("s", "other")
    assert "mine" in store.snapshot()["scopes"]["s"]["claims"]


def test_a_dead_owner_is_reclaimed_even_when_its_heartbeat_is_in_the_future(tmp_path):
    ceilings(s=1)
    dead = {"pid": 2_000_000_000, "identity": "gone", "pgid": 1, "epoch": "e"}
    clock = [1_000.0]
    store = _store(tmp_path, lease_ttl=1.0, clock=lambda: clock[0],
                   liveness=lambda owner: False if owner.get("identity") == "gone" else True)
    store.enqueue("s", "ghost")
    assert store.try_claim("s", "ghost", Owner(**dead))
    clock[0] -= 100_000  # the clock now reads before the claim's heartbeat
    store.enqueue("s", "next")
    assert store.try_claim("s", "next")


def test_unknown_liveness_with_a_future_heartbeat_is_quarantined_not_taken(tmp_path):
    ceilings(s=1)
    clock = [1_000.0]
    store = _store(tmp_path, lease_ttl=1.0, clock=lambda: clock[0], liveness=lambda owner: None)
    store.enqueue("s", "ghost")
    assert store.try_claim("s", "ghost")
    clock[0] -= 100_000
    store.enqueue("s", "next")
    assert not store.try_claim("s", "next")
    assert store.snapshot()["scopes"]["s"]["claims"]["ghost"]["quarantined"] is True


def test_the_run_a_claim_starts_reserves_the_same_slot_not_a_second_one(tmp_path):
    from garuda.runtime.capacity import CapacityStore

    ceilings(claude=1)
    capacity = CapacityStore()
    store = _store(tmp_path)
    store.enqueue("u:claude", "sess", harness="claude", session_id="sess")
    assert store.try_claim("u:claude", "sess")
    # What the run guard does at the start of the run it was claimed for: same holder,
    # no owner given. It must find the claim's reservation, not collide with it.
    again = capacity.reserve("claude", "sess", 1)
    assert capacity.holders("claude") == ["sess"]
    assert again.owner == capacity.reserve("claude", "sess", 1).owner
    store.release("u:claude", "sess")
    assert capacity.holders("claude") == []
    # After release the holder is no longer adopted: a later reserve is an ordinary one.
    later = capacity.reserve("claude", "sess", 1)
    assert later.owner.epoch != again.owner.epoch
