"""A failed release cannot restore dispatch through a retained claimant handle."""

import pytest

from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
from garuda.runtime.ownership import current_owner
from garuda.runtime.queue import QueueError, QueueStore, scope_for


@pytest.mark.parametrize("path", ["claiming-instance", "another-instance", "workspace-refusal"])
def test_failed_release_intent_revokes_direct_dispatch_but_preserves_the_slot(tmp_path, monkeypatch, path):
    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
    scope = scope_for("native")
    owner = current_owner()
    queue.enqueue(scope, "job", session_id="job", config_digest="config")
    releaser = QueueStore(queue.root, capacity=capacity) if path == "another-instance" else queue
    publish = releaser._publish
    before = []

    def fail_intent(state):
        if any(record["operation"] in ("release", "requeue")
               for record in state["pending"].values()):
            raise OSError("release intent interrupted")
        publish(state)

    monkeypatch.setattr(releaser, "_publish", fail_intent)
    with pytest.raises(OSError, match="release intent interrupted"):
        if path == "workspace-refusal":
            def refuse_workspace():
                before.append((queue.root / "state.json").read_bytes())
                return False

            queue.claim_with_workspace(scope, "job", refuse_workspace, owner=owner)
        else:
            assert queue.try_claim(scope, "job", owner)
            before.append((queue.root / "state.json").read_bytes())
            releaser.release(scope, "job", owner)
    assert (queue.root / "state.json").read_bytes() == before[0]
    assert capacity.holders("native") == ["job"]
    with pytest.raises(CapacityUnavailable):
        capacity.reserve("native", "job", 1, owner=owner)
    with pytest.raises(QueueError):
        queue.begin_dispatch(scope, "job", owner)
    assert (queue.root / "state.json").read_bytes() == before[0]
    assert capacity.holders("native") == ["job"]


def test_a_fully_committed_claim_can_still_begin_dispatch(tmp_path):
    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
    scope = scope_for("native")
    owner = current_owner()
    queue.enqueue(scope, "job", session_id="job", config_digest="config")
    assert queue.try_claim(scope, "job", owner)
    queue.begin_dispatch(scope, "job", owner)
    assert capacity.holders("native") == ["job"]
    assert queue.release(scope, "job", owner)
    assert capacity.holders("native") == []


@pytest.mark.parametrize("boundary", ["capacity-lock", "activation-publication"])
def test_a_captured_capacity_ticket_cannot_activate_after_release_intent_fails(tmp_path, monkeypatch, boundary):
    import contextlib
    import threading

    import garuda.runtime.capacity as capacity_module

    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
    scope = scope_for("native")
    owner = current_owner()
    queue.enqueue(scope, "job", session_id="job", config_digest="config")
    assert queue.try_claim(scope, "job", owner)
    before = (queue.root / "state.json").read_bytes()
    captured, resume = threading.Event(), threading.Event()
    lock = capacity_module.exclusive_lock

    @contextlib.contextmanager
    def pause_before_lock(root):
        captured.set()
        assert resume.wait(10), "release did not finish"
        with lock(root) as fd:
            yield fd

    if boundary == "capacity-lock":
        monkeypatch.setattr(capacity_module, "exclusive_lock", pause_before_lock)
    else:
        publish = capacity_module.write_locked_document

        def pause_after_publication(fd, path, document):
            publish(fd, path, document)
            captured.set()
            assert resume.wait(10), "release did not finish"

        monkeypatch.setattr(capacity_module, "write_locked_document", pause_after_publication)
    result = []

    def reserve():
        try:
            capacity.reserve("native", "job", 1, owner=owner)
        except CapacityUnavailable:
            result.append("refused")
        except BaseException as error:
            result.append(error)
        else:
            result.append("granted")

    def fail_intent(state):
        raise OSError("release intent interrupted")

    monkeypatch.setattr(queue, "_publish", fail_intent)
    thread = threading.Thread(target=reserve)
    thread.start()
    try:
        assert captured.wait(10), "capacity never captured its ticket"
        with pytest.raises(OSError, match="release intent interrupted"):
            queue.release(scope, "job", owner)
    finally:
        resume.set()
        thread.join(10)
    assert not thread.is_alive()
    assert result == ["refused"]
    assert (queue.root / "state.json").read_bytes() == before
    assert capacity.holders("native") == ["job"]
