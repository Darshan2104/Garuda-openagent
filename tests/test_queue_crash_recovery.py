"""Queue admission and release across independent durable stores (#216).

Use the ordinary launch guard as the observer: a queue-only race test cannot
prove that foreground, SDK and web callers honor an interrupted transaction.
"""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
from garuda.runtime.queue import CorruptState, QueueError, QueueStore
from garuda.workspace.lease import LeaseStore

ROOT = Path(__file__).resolve().parents[1]


def stores(tmp_path):
    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
    queue.enqueue("u:native", "job", user="u", harness="native",
                  session_id="job", config_digest="config-a")
    return queue, capacity


def launch_guard(tmp_path, capacity, holder):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return WorkspaceLeaseGuard(str(workspace), holder, capacity_key="native",
                               capacity_store=capacity, capacity_ceiling=1,
                               lease_store=LeaseStore(tmp_path / "leases"))


def interrupt_publication(monkeypatch, queue, *, claimed, message):
    """Interrupt the actual rename, for both pathname and anchored writers."""
    publish = os.replace

    def interrupt(src, dst, **kwargs):
        target = Path(dst)
        directory_fd = kwargs.get("dst_dir_fd")
        at_queue = (os.fstat(directory_fd).st_ino == queue.root.stat().st_ino
                    if directory_fd is not None else target.parent == queue.root)
        if at_queue and target.name == "state.json":
            candidate = Path(src)
            if not candidate.is_absolute():
                candidate = queue.root / candidate
            document = json.loads(candidate.read_text())
            if bool(document["scopes"]["u:native"]["claims"]) == claimed:
                raise OSError(message)
        publish(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", interrupt)


def test_a_failed_claim_publication_cannot_authorize_the_ordinary_launch_guard(tmp_path, monkeypatch):
    queue, capacity = stores(tmp_path)
    interrupt_publication(monkeypatch, queue, claimed=True, message="queue publication interrupted")
    with pytest.raises((QueueError, OSError), match="publication interrupted"):
        queue.try_claim("u:native", "job")
    assert [(e["id"], e["state"]) for e in queue.entries()] == [("job", "queued")]
    assert capacity.holders("native") == ["job"]

    guard = launch_guard(tmp_path, capacity, "job")
    try:
        with pytest.raises(CapacityUnavailable):
            guard.acquire()
        assert not (tmp_path / "leases").exists()
    finally:
        asyncio.run(guard.release())


def test_pending_admission_is_visible_but_cannot_be_rebound_or_activated(tmp_path, monkeypatch):
    queue, capacity = stores(tmp_path)
    interrupt_publication(monkeypatch, queue, claimed=True, message="claim interrupted")
    with pytest.raises(QueueError):
        queue.try_claim("u:native", "job")
    files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    observed = QueueStore(queue.root, capacity=capacity).entries()
    assert [(row["id"], row["quarantined"], row["pending_operation"]) for row in observed] == [
        ("job", True, "claim")]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files} == files
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == set(files)
    assert queue.cancel("u:native", "job")
    with pytest.raises(QueueError, match="unresolved transaction"):
        queue.enqueue("u:native", "job", user="u", harness="native",
                      session_id="replacement", config_digest="replacement-config")
    assert QueueStore(queue.root, capacity=capacity).entries()[0]["state"] == "quarantined"
    assert capacity.holders("native") == ["job"]


@pytest.mark.parametrize("field", ["transaction", "queue_root", "binding_digest", "activation_ready",
                                  "pid", "identity", "pgid", "epoch"])
def test_a_modified_reservation_cannot_activate_or_release_the_committed_claim(tmp_path, field):
    queue, capacity = stores(tmp_path)
    assert queue.try_claim("u:native", "job")
    record = capacity.root / (hashlib.sha256(b"native").hexdigest()[:32] + ".json")
    document = json.loads(record.read_bytes())
    slot = document["slots"]["job"]
    if field in slot["queue"]:
        slot["queue"][field] = False if field == "activation_ready" else "different-binding"
    else:
        slot["owner"][field] = 1 if field in ("pid", "pgid") else "different-owner"
    record.write_text(json.dumps(document))
    preserved = record.read_bytes()
    with pytest.raises(CapacityUnavailable, match="committed launch authority"):
        capacity.reserve("native", "job", 1)
    with pytest.raises(CapacityUnavailable, match="replacement slot"):
        queue.release("u:native", "job")
    assert record.read_bytes() == preserved
    assert capacity.holders("native") == ["job"]


@pytest.mark.parametrize("field,value", [("user", None), ("harness", None), ("id", "")])
def test_partial_current_waiters_refuse_before_journal_or_capacity_mutation(tmp_path, field, value):
    queue, capacity = stores(tmp_path)
    record = queue.root / "state.json"
    document = json.loads(record.read_bytes())
    document["scopes"]["u:native"]["waiting"][0][field] = value
    record.write_text(json.dumps(document))
    preserved = record.read_bytes()
    with pytest.raises(CorruptState, match="binding"):
        queue.entries()
    with pytest.raises(CorruptState, match="binding"):
        queue.try_claim("u:native", "job")
    assert record.read_bytes() == preserved
    assert not capacity.root.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a real POSIX fork")
def test_a_fork_cannot_activate_its_parents_committed_queue_ticket(tmp_path):
    # Fork in a cold, single-threaded process, not pytest's threaded host.
    code = """
        import os, sys
        from pathlib import Path
        from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
        from garuda.runtime.queue import QueueStore, QueueError
        from garuda.runtime.ownership import Owner
        root = Path(sys.argv[1])
        capacity = CapacityStore(root / 'capacity')
        queue = QueueStore(root / 'queue', capacity=capacity, ceiling=lambda _: 1)
        queue.enqueue('u:native', 'job', user='u', harness='native',
                      session_id='job', config_digest='config-a')
        assert queue.try_claim('u:native', 'job')
        owner = Owner(**queue.entries()[0]['worker'])
        child = os.fork()
        if child == 0:
            try:
                try:
                    capacity.reserve('native', 'job', 1, owner=owner)
                except CapacityUnavailable:
                    pass
                else:
                    os._exit(41)
                try:
                    queue.begin_dispatch('u:native', 'job', owner)
                except QueueError:
                    pass
                else:
                    os._exit(42)
                os._exit(0)
            except BaseException:
                os._exit(43)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert capacity.holders('native') == ['job']
        reservation = capacity.reserve('native', 'job', 1, owner=owner)
        capacity.release(reservation)
        assert capacity.holders('native') == ['job']
        assert queue.release('u:native', 'job')
    """
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(code), str(tmp_path)],
                            env=dict(os.environ, PYTHONPATH=str(ROOT)),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("holder", ["foreground", "job"], ids=["another-launch", "same-session-retry"])
def test_a_failed_queue_release_publication_keeps_the_slot_from_ordinary_launches(tmp_path, monkeypatch, holder):
    queue, capacity = stores(tmp_path)
    assert queue.try_claim("u:native", "job")
    interrupt_publication(monkeypatch, queue, claimed=False, message="queue release interrupted")
    with pytest.raises((QueueError, OSError), match="release interrupted"):
        queue.release("u:native", "job")
    assert [(e["id"], e["state"]) for e in queue.entries()] == [("job", "running")]

    guard = launch_guard(tmp_path, capacity, holder)
    try:
        with pytest.raises(CapacityUnavailable):
            guard.acquire()
        assert capacity.holders("native") == ["job"]
        assert not (tmp_path / "leases").exists()
    finally:
        asyncio.run(guard.release())

    from garuda.runtime.ownership import Owner

    with pytest.raises(QueueError):
        queue.begin_dispatch("u:native", "job", Owner(**queue.entries()[0]["worker"]))


def test_a_crashed_uncommitted_reservation_requires_queue_recovery_before_reuse(tmp_path):
    queue, capacity = stores(tmp_path)
    code = """
        import os, sys
        from pathlib import Path
        from garuda.runtime.capacity import CapacityStore
        from garuda.runtime.queue import QueueStore
        root = Path(sys.argv[1])
        replace = os.replace
        def interrupt(src, dst, **kwargs):
            replace(src, dst, **kwargs)
            directory_fd = kwargs.get('dst_dir_fd')
            at_capacity = ((root / 'capacity').exists()
                           and os.fstat(directory_fd).st_ino == (root / 'capacity').stat().st_ino
                           if directory_fd is not None else Path(dst).parent == root / 'capacity')
            if at_capacity:
                os._exit(73)
        os.replace = interrupt
        queue = QueueStore(root / 'queue', capacity=CapacityStore(root / 'capacity'),
                           ceiling=lambda _: 1)
        queue.try_claim('u:native', 'job')
    """
    worker = subprocess.run([sys.executable, "-c", textwrap.dedent(code), str(tmp_path)],
                            env=dict(os.environ, PYTHONPATH=str(ROOT)),
                            capture_output=True, text=True, timeout=30)
    assert worker.returncode == 73, worker.stderr
    assert capacity.holders("native") == ["job"]
    assert [(e["id"], e["state"]) for e in queue.entries()] == [("job", "queued")]
    with pytest.raises(CapacityUnavailable):
        capacity.reserve("native", "foreground", 1)

    queue.recover_pending()
    assert capacity.holders("native") == []
    assert [(e["id"], e["state"]) for e in queue.entries()] == [("job", "queued")]
    assert queue.try_claim("u:native", "job")
    assert queue.release("u:native", "job")


def test_a_dead_background_worker_with_a_separate_runtime_group_remains_quarantined(tmp_path):
    """D.2 dispatches a real separate-group child, as the ACP launcher does."""
    code = """
        import argparse, asyncio, json, os, subprocess, sys
        from pathlib import Path
        from garuda.core.sessions import SessionStore
        from garuda.interfaces import bg_sessions
        from garuda.runtime.capacity import CapacityStore
        from garuda.runtime.queue import QueueStore
        from garuda.runtime.recovery import _process_identity
        root = Path(sys.argv[1])
        store = SessionStore(root / 'sessions')
        queue = QueueStore(root / 'queue', capacity=CapacityStore(root / 'capacity'),
                           ceiling=lambda _: 1)
        args = argparse.Namespace(command='run', task='t', file=None, workspace=str(root),
                                  runtime=None, agent='build', model=None, name=None, resume=None,
                                  trajectory=None, json=False, bg=True)
        session = bg_sessions.launch(args, store=store, queue=queue,
                                    spawn=lambda sid: type('P', (), {'pid': os.getpid(),
                                                                    'args': ['worker', sid]})())
        async def runner(args):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                     start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / 'dispatch.json').write_text(json.dumps(
                {'session': session, 'pid': child.pid, 'identity': _process_identity(child.pid)}))
            os._exit(73)
        asyncio.run(bg_sessions.run_worker_async(session, store=store, queue=queue, runner=runner))
    """
    worker = subprocess.run([sys.executable, "-c", textwrap.dedent(code), str(tmp_path)],
                            env=dict(os.environ, PYTHONPATH=str(ROOT)), start_new_session=True,
                            capture_output=True, text=True, timeout=30)
    assert worker.returncode == 73, worker.stderr
    dispatch = json.loads((tmp_path / "dispatch.json").read_text())
    from garuda.runtime.recovery import _process_identity, same_process

    try:
        os.kill(dispatch["pid"], 0)  # an actual descendant outlives its worker
        capacity = CapacityStore(tmp_path / "capacity")
        queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
        (entry,) = queue.entries()
        queue.enqueue(entry["scope"], "next", session_id="next", config_digest="config-b")
        with pytest.raises(CapacityUnavailable):
            capacity.reserve("native", "foreground", 1)
        assert not queue.try_claim(entry["scope"], "next")
        queue.recover_pending()
        running = [e for e in queue.entries() if e["state"] == "running"]
        assert len(running) == 1 and running[0]["id"] == dispatch["session"]
        assert running[0]["quarantined"]
        assert capacity.holders("native") == [dispatch["session"]]
        os.kill(dispatch["pid"], 0)
    finally:
        if same_process(dispatch["identity"], _process_identity(dispatch["pid"])):
            os.killpg(dispatch["pid"], signal.SIGKILL)


@pytest.mark.parametrize("missing", ["session_id", "config_digest"])
def test_a_claim_without_frozen_runtime_bindings_cannot_begin_dispatch(tmp_path, missing):
    from garuda.runtime.ownership import current_owner

    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
    binding = {"session_id": "job", "config_digest": "cfg"}
    del binding[missing]
    queue.enqueue("u:native", "job", **binding)
    owner = current_owner()
    assert queue.try_claim("u:native", "job", owner)
    guard = launch_guard(tmp_path, capacity, "job")
    try:
        with pytest.raises(CapacityUnavailable):
            guard.acquire()
        with pytest.raises(QueueError):
            queue.begin_dispatch("u:native", "job", owner)
        assert not (tmp_path / "leases").exists()
    finally:
        asyncio.run(guard.release())
        queue.release("u:native", "job", owner)


@pytest.mark.parametrize("boundary", [
    "claim-intent", "reservation", "claim-publication", "capacity-commit", "claim-commit",
    "release-intent", "release-publication", "capacity-release", "release-commit", "activation",
])
def test_process_death_at_each_journal_boundary_cannot_grant_or_replay_work(tmp_path, boundary):
    queue, capacity = stores(tmp_path)
    code = """
        import json, os, sys
        from pathlib import Path
        from garuda.runtime.capacity import CapacityStore
        from garuda.runtime.ownership import current_owner
        from garuda.runtime.queue import QueueStore
        root, boundary = Path(sys.argv[1]), sys.argv[2]
        replace = os.replace
        releasing = False
        def interrupted(src, dst, **kwargs):
            replace(src, dst, **kwargs)
            path = Path(dst)
            if path.name == 'state.json':
                document = json.loads((root / 'queue' / 'state.json').read_text())
                pending = list(document['pending'].values())
                claim = document['scopes']['u:native']['claims'].get('job')
                if not releasing:
                    hit = ((boundary == 'claim-intent' and pending and pending[0]['phase'] == 'intent')
                           or (boundary == 'claim-publication' and pending and pending[0]['phase'] == 'published')
                           or (boundary == 'claim-commit' and not pending and claim))
                else:
                    hit = ((boundary == 'release-intent' and pending and pending[0]['phase'] == 'intent')
                           or (boundary == 'release-publication' and pending and pending[0]['phase'] == 'published')
                           or (boundary == 'release-commit' and not pending and not claim))
            elif (path.parent == root / 'capacity' or kwargs.get('dst_dir_fd') is not None
                  and os.fstat(kwargs['dst_dir_fd']).st_ino == (root / 'capacity').stat().st_ino):
                capacity_path = path if path.is_absolute() else root / 'capacity' / path
                slot = json.loads(capacity_path.read_text())['slots'].get('job')
                phase = slot['queue']['phase'] if slot else None
                hit = ((boundary == 'reservation' and phase == 'pending')
                       or (boundary == 'capacity-commit' and phase == 'selected')
                       or (boundary == 'activation' and phase == 'activated')
                       or (boundary == 'capacity-release' and releasing and slot is None))
            else:
                hit = False
            if hit:
                os._exit(73)
        os.replace = interrupted
        queue = QueueStore(root / 'queue', capacity=CapacityStore(root / 'capacity'),
                           ceiling=lambda _: 1)
        owner = current_owner()
        assert queue.try_claim('u:native', 'job', owner)
        if boundary == 'activation':
            queue.begin_dispatch('u:native', 'job', owner)
        elif boundary.startswith('release-') or boundary == 'capacity-release':
            releasing = True
            queue.release('u:native', 'job', owner)
    """
    worker = subprocess.run([sys.executable, "-c", textwrap.dedent(code), str(tmp_path), boundary],
                            env=dict(os.environ, PYTHONPATH=str(ROOT)),
                            capture_output=True, text=True, timeout=30)
    assert worker.returncode == 73, worker.stderr
    reserved = boundary not in {"claim-intent", "capacity-release", "release-commit"}
    assert capacity.holders("native") == (["job"] if reserved else [])
    if reserved:
        with pytest.raises(CapacityUnavailable):
            capacity.reserve("native", "foreground", 1)
    before = queue.entries()
    queue.recover_pending()
    after = queue.entries()
    if boundary == "activation":
        assert capacity.holders("native") == ["job"]
        assert len(after) == 1 and after[0]["state"] == "running" and after[0]["quarantined"]
    else:
        assert capacity.holders("native") == []
        expected = [("job", "queued")] if boundary in {"claim-intent", "reservation"} else []
        assert [(e["id"], e["state"]) for e in after] == expected
    assert all(e["state"] != "running" or e["quarantined"] for e in after)
    assert not (tmp_path / "leases").exists()  # reconciliation never activates a workspace
    assert {e["id"] for e in after} <= {e["id"] for e in before}


def test_concurrent_workspace_requeues_keep_the_original_durable_fifo(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    capacity = CapacityStore(tmp_path / "capacity")
    first = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 2)
    second = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 2)
    for item in ("first", "second", "third"):
        first.enqueue("u:native", item, session_id=item, config_digest="frozen")
    first_claimed, second_claimed, first_returned = Event(), Event(), Event()

    def unavailable_first():
        first_claimed.set()
        assert second_claimed.wait(10)
        return False

    def unavailable_second():
        second_claimed.set()
        assert first_returned.wait(10)
        return False

    def requeue_first():
        try:
            return first.claim_with_workspace("u:native", "first", unavailable_first)
        finally:
            first_returned.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(requeue_first)
        assert first_claimed.wait(10)
        b = workers.submit(second.claim_with_workspace, "u:native", "second", unavailable_second)
        assert not a.result(timeout=20) and not b.result(timeout=20)
    assert [(e["id"], e["seq"]) for e in first.entries()] == [
        ("first", 1), ("second", 2), ("third", 3)]
    assert capacity.holders("native") == []


def test_bound_version_two_waiters_migrate_without_replacing_prior_artifacts_or_capacity(tmp_path):
    capacity = CapacityStore(tmp_path / "capacity")
    foreground = capacity.reserve("native", "foreground", 1)
    root = tmp_path / "queue"
    root.mkdir(mode=0o700)
    state = {"version": 2, "seq": 1, "scopes": {"u:native": {
        "waiting": [{"id": "job", "seq": 1, "user": "u", "harness": "native",
                     "session_id": "job", "config_digest": "cfg", "enqueued_at": 0}], "claims": {}}}}
    raw = (json.dumps(state) + "\n").encode()
    source = root / "state.json"
    source.write_bytes(raw)
    source.chmod(0o600)
    prior = root / "state.json.v1"
    prior.write_bytes(b"prior source evidence")
    queue = QueueStore(root, capacity=capacity, ceiling=lambda _: 1)
    assert queue.snapshot()["version"] == 2 and source.read_bytes() == raw
    assert not queue.try_claim("u:native", "job")
    assert queue.snapshot()["version"] == 3
    assert (root / "state.json.v2").read_bytes() == raw
    assert prior.read_bytes() == b"prior source evidence"
    assert capacity.holders("native") == ["foreground"]
    capacity.release(foreground)
    assert queue.try_claim("u:native", "job")
    assert queue.release("u:native", "job")
    assert capacity.holders("native") == []


@pytest.mark.parametrize("kind", ["claimed", "session_id", "config_digest"])
def test_version_two_unproved_work_refuses_upgrade_and_preserves_its_source(tmp_path, kind):
    root = tmp_path / "queue"
    root.mkdir(mode=0o700)
    entry = {"id": "job", "seq": 1, "user": "u", "harness": "native",
             "session_id": "job", "config_digest": "cfg", "enqueued_at": 0}
    claims = {}
    if kind == "claimed":
        from garuda.runtime.ownership import current_owner

        claims = {"job": {**entry, "owner": current_owner().to_dict(), "heartbeat": 0}}
        waiting = []
    else:
        del entry[kind]
        waiting = [entry]
    raw = json.dumps({"version": 2, "seq": 1, "scopes": {
        "u:native": {"waiting": waiting, "claims": claims}}}).encode()
    source = root / "state.json"
    source.write_bytes(raw)
    source.chmod(0o600)
    queue = QueueStore(root, capacity=CapacityStore(tmp_path / "capacity"), ceiling=lambda _: 1)
    assert queue.entries()  # diagnosis confers no mutation authority
    with pytest.raises(CorruptState, match="unproved activation|bindings"):
        queue.enqueue("u:native", "another")
    assert source.read_bytes() == raw
    assert not (root / "state.json.v2").exists()


@pytest.mark.parametrize("operation", ["reserve", "release"])
@pytest.mark.parametrize("versioned", [True, False], ids=["version-one", "pre-versioning"])
def test_nonempty_legacy_capacity_cannot_invent_the_reservations_origin(tmp_path, operation, versioned):
    from garuda.runtime.capacity import CapacityError, Reservation
    from garuda.runtime.ownership import Owner, current_owner

    root = tmp_path / "capacity"
    root.mkdir(mode=0o700)
    owner = (Owner(2_000_000_000, "gone", 1, "old")
             if operation == "reserve" else current_owner())
    source = root / f'{hashlib.sha256(b"native").hexdigest()[:32]}.json'
    document = {"key": "native", "slots": {
        "legacy": {"owner": owner.to_dict(), "reserved_at": 0}}}
    if versioned:
        document["version"] = 1
    raw = (json.dumps(document) + "\n").encode()
    source.write_bytes(raw)
    source.chmod(0o600)
    capacity = CapacityStore(root)
    assert capacity.holders("native") == ["legacy"]
    with pytest.raises(CapacityError, match="legacy|origin"):
        if operation == "reserve":
            capacity.reserve("native", "foreground", 1)
        else:
            capacity.release(Reservation("native", "legacy", owner))
    assert source.read_bytes() == raw
    assert not source.with_name(source.name + ".v1").exists()


@pytest.mark.parametrize("backup", ["absent", "matching", "different", "symlink"])
def test_empty_legacy_capacity_upgrades_only_after_a_verified_exact_archive(tmp_path, backup):
    from garuda.runtime.capacity import CapacityError

    root = tmp_path / "capacity"
    root.mkdir(mode=0o700)
    source = root / f'{hashlib.sha256(b"native").hexdigest()[:32]}.json'
    raw = b'{"version":1, "key":"native", "slots":{}}\n'
    source.write_bytes(raw)
    source.chmod(0o600)
    archive = source.with_name(source.name + ".v1")
    unrelated = tmp_path / "user-file"
    unrelated.write_bytes(raw)
    if backup == "symlink":
        archive.symlink_to(unrelated)
    elif backup in ("matching", "different"):
        archive.write_bytes(raw if backup == "matching" else b"prior evidence")
        archive.chmod(0o600)
    capacity = CapacityStore(root)
    assert capacity.holders("native") == [] and source.read_bytes() == raw
    if backup in ("different", "symlink"):
        with pytest.raises(CapacityError, match="backup"):
            capacity.reserve("native", "foreground", 1)
        assert source.read_bytes() == raw and unrelated.read_bytes() == raw
        if backup == "different":
            assert archive.read_bytes() == b"prior evidence"
    else:
        reservation = capacity.reserve("native", "foreground", 1)
        assert archive.read_bytes() == raw
        assert json.loads(source.read_bytes())["version"] == 2
        assert capacity.holders("native") == ["foreground"]
        capacity.release(reservation)
        assert capacity.holders("native") == []
