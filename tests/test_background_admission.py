"""Background admission uses the canonical capacity lane without discarding aliases."""

import argparse
import json
import os
from pathlib import Path

import pytest
import yaml

from garuda.agents.setup import prepare_runtime_catalog
from garuda.core.sessions import SessionStore
from garuda.interfaces import bg_sessions
from garuda.runtime.capacity import CapacityStore
from garuda.runtime.queue import QueueStore, scope_for


def _environment(tmp_path, target):
    workspace = tmp_path / "workspace"
    settings = workspace / ".agent" / "settings.yaml"
    settings.parent.mkdir(parents=True)
    settings.write_text(yaml.safe_dump({"runtime_refs": [{"alias": "quick", "runtime_id": target}]}))
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(
        yaml.safe_dump({"capacity": {target: 1}}))
    assert prepare_runtime_catalog(workspace).registry.get("quick").runtime_id == target
    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity)
    store = SessionStore(tmp_path / "sessions")
    args = argparse.Namespace(task="test", file=None, workspace=str(workspace), runtime="quick",
                              agent="build", model=None, name=None, resume=None, trajectory=None,
                              json=False, bg=True)
    return settings, capacity, queue, store, args


def _launch(queue, store, args):
    return bg_sessions.launch(args, queue=queue, store=store, spawn=lambda sid: type(
        "NoWorker", (), {"pid": os.getpid(), "args": ["worker", sid]})())


@pytest.mark.parametrize("target", ["native", "codex"])
def test_a_runtime_alias_cannot_claim_a_second_lane_while_canonical_capacity_is_full(tmp_path, target):
    _, capacity, queue, store, args = _environment(tmp_path, target)
    foreground = capacity.reserve(target, "foreground", 1)
    session = _launch(queue, store, args)
    assert not queue.try_claim(scope_for("quick"), session), "an alias created its own capacity lane"
    assert not queue.try_claim(scope_for(target), session)
    capacity.release(foreground)
    assert queue.try_claim(scope_for(target), session)
    assert capacity.holders(target) == [session]
    launch = json.loads((store.session_dir(session) / bg_sessions.LAUNCH_FILE).read_bytes())
    assert launch["args"]["runtime"] == "quick"  # Keep the original reference for downstream resolution.
    assert queue.release(scope_for(target), session)


@pytest.mark.parametrize("runtime_refs", [[{"alias": "quick", "runtime_id": "codex"}], []])
async def test_changed_alias_refusal_allows_the_next_job_to_use_its_original_lane(tmp_path, runtime_refs):
    settings, capacity, queue, store, args = _environment(tmp_path, "native")
    session = _launch(queue, store, args)
    follower = _launch(queue, store, argparse.Namespace(**{**vars(args), "runtime": "native"}))
    settings.write_text(yaml.safe_dump({"runtime_refs": runtime_refs}))
    called = []

    async def runner(args):
        called.append((args._session_id, capacity.holders("native")))
        return 0

    with pytest.raises(bg_sessions.BackgroundRefused):
        await bg_sessions.run_worker_async(session, queue=queue, store=store, runner=runner)
    assert not called
    assert [row["id"] for row in queue.entries()] == [follower]
    refused = store.load_meta(session)
    assert refused["state"]["outcome"] == "refused" and refused["state"]["work"] == "stopped"
    assert refused["state"]["verification"] == {"status": "unavailable"}
    assert await bg_sessions.run_worker_async(follower, queue=queue, store=store, runner=runner) == 0
    assert called == [(follower, [follower])]
    assert queue.entries() == [] and capacity.holders("native") == []


@pytest.mark.parametrize("ambiguity", ["worker", "args", "claimed"])
async def test_changed_alias_preserves_an_unproved_or_claimed_admission(tmp_path, ambiguity):
    settings, _, queue, store, args = _environment(tmp_path, "native")
    session = _launch(queue, store, args)
    if ambiguity == "worker":
        worker = store.load_meta(session)["worker"]
        store.update_meta(session, {"worker": {**worker, "identity": "different-start"}})
    elif ambiguity == "args":
        path = store.session_dir(session) / bg_sessions.LAUNCH_FILE
        launch = json.loads(path.read_text())
        launch["args"]["task"] = "different task"
        bg_sessions._write_private(path, launch)
    else:
        assert queue.try_claim(scope_for("native"), session)
    settings.write_text(yaml.safe_dump({"runtime_refs": []}))
    before = (queue.root / "state.json").read_bytes()

    async def runner(_):
        pytest.fail("this admission must never dispatch")

    with pytest.raises(bg_sessions.BackgroundRefused):
        await bg_sessions.run_worker_async(session, queue=queue, store=store, runner=runner)
    assert (queue.root / "state.json").read_bytes() == before
    assert store.load_meta(session)["state"]["work"] == "queued"


@pytest.mark.parametrize("failure", ["load_meta", "retire_waiting"])
async def test_a_retirement_failure_keeps_the_refusal_and_its_admission(tmp_path, monkeypatch, capsys, failure):
    from garuda.runtime.strict_store import StorageError

    settings, _, queue, store, args = _environment(tmp_path, "native")
    session = _launch(queue, store, args)
    settings.write_text(yaml.safe_dump({"runtime_refs": []}))
    before = (queue.root / "state.json").read_bytes()

    def unavailable(*_args, **_kwargs):
        raise StorageError("storage unavailable")

    monkeypatch.setattr(store if failure == "load_meta" else queue, failure, unavailable)

    async def runner(_):
        pytest.fail("this admission must never dispatch")

    with pytest.raises(bg_sessions.BackgroundRefused, match="unknown runtime .quick."):
        await bg_sessions.run_worker_async(session, queue=queue, store=store, runner=runner)
    assert (queue.root / "state.json").read_bytes() == before
    assert "StorageError: storage unavailable" in capsys.readouterr().err
    assert type(store).load_meta(store, session)["state"]["work"] == "queued"


async def test_an_unchanged_alias_dispatches_through_its_canonical_shared_reservation(tmp_path):
    _, capacity, queue, store, args = _environment(tmp_path, "native")
    session = _launch(queue, store, args)
    observed = []

    async def runner(args):
        assert args.runtime == "quick"
        reservation = capacity.reserve("native", session, 1)
        observed.append(capacity.holders("native"))
        capacity.release(reservation)
        return 0

    assert await bg_sessions.run_worker_async(session, queue=queue, store=store, runner=runner) == 0
    assert observed == [[session]]
    assert capacity.holders("native") == []
    assert queue.entries() == []


@pytest.mark.parametrize("changed", ["args", "receipt"])
async def test_modified_launch_arguments_or_receipt_cannot_dispatch_the_admitted_job(tmp_path, changed):
    _, capacity, queue, store, args = _environment(tmp_path, "native")
    session = _launch(queue, store, args)
    launch = store.session_dir(session) / bg_sessions.LAUNCH_FILE
    document = json.loads(launch.read_bytes())
    if changed == "args":
        document["args"]["task"] = "a different task"
    else:
        document["config_digest"] = "a different receipt"
    launch.write_text(json.dumps(document))
    source = queue.root / "state.json"
    before = source.read_bytes()
    called = []

    async def runner(args):
        called.append(args)
        return 0

    with pytest.raises(bg_sessions.BackgroundRefused):
        await bg_sessions.run_worker_async(session, queue=queue, store=store, runner=runner)
    assert not called
    assert source.read_bytes() == before
    assert not capacity.root.exists()


@pytest.mark.parametrize("refusal", ["disabled", "unknown"])
def test_runtime_refusal_happens_before_background_session_or_worker_creation(tmp_path, refusal):
    _, _, queue, store, args = _environment(tmp_path, "codex")
    if refusal == "disabled":
        Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(
            yaml.safe_dump({"disabled_runtimes": ["codex"]}))
    else:
        args.runtime = "missing-runtime"
    spawned = []
    with pytest.raises(bg_sessions.BackgroundRefused):
        bg_sessions.launch(args, queue=queue, store=store, spawn=spawned.append)
    assert not spawned
    assert not store.root.exists()
    assert not queue.root.exists()


async def test_runner_receives_original_alias_for_narrowed_capability_resolution(tmp_path):
    settings, capacity, queue, store, args = _environment(tmp_path, "codex")
    settings.write_text(yaml.safe_dump({"runtime_refs": [{
        "alias": "quick", "runtime_id": "codex", "capabilities": ["prompt"]}]}))
    session = _launch(queue, store, args)
    observed = []

    async def runner(args):
        catalog = prepare_runtime_catalog(args.workspace)
        selected = catalog.registry.get(args.runtime)
        assert catalog.registry.get("codex").capabilities.supports("cancel")
        assert selected.capabilities.supports("prompt")
        assert not selected.capabilities.supports("cancel")
        reservation = capacity.reserve(selected.runtime_id, session, 1)
        observed.append(capacity.holders("codex"))
        capacity.release(reservation)
        return 0

    assert await bg_sessions.run_worker_async(session, queue=queue, store=store, runner=runner) == 0
    assert observed == [[session]]
    assert capacity.holders("codex") == []
