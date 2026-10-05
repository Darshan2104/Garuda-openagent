"""Effective roles must enter the same capacity lane their runtime will use."""

import argparse
import os
from pathlib import Path

import pytest

from garuda.config import garuda_yaml
from garuda.core.sessions import SessionStore
from garuda.interfaces import bg_sessions
from garuda.runtime.capacity import CapacityStore
from garuda.runtime.queue import QueueStore, scope_for


def environment(tmp_path, selected="default", harness="codex", extra=""):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_file = garuda_yaml.user_path()
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("version: 1\ndefaults: {role: worker}\nroles:\n"
                         f"  worker: {{harness: {harness}{extra}}}\n")
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(
        "capacity: {native: 1, codex: 1}\n")
    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity)
    store = SessionStore(tmp_path / "sessions")
    args = argparse.Namespace(task="test", file=None, workspace=str(workspace), runtime=None,
                              role="worker" if selected == "explicit" else None,
                              agent="build", model=None, name=None, resume=None,
                              trajectory=None, json=False, bg=True)
    return user_file, capacity, queue, store, args


def launch(queue, store, args):
    return bg_sessions.launch(args, queue=queue, store=store, spawn=lambda sid: type(
        "NoWorker", (), {"pid": os.getpid(), "args": ["worker", sid]})())


@pytest.mark.parametrize("selected", ["default", "explicit"])
def test_a_background_role_waits_for_its_runtime_capacity(tmp_path, selected):
    _, capacity, queue, store, args = environment(tmp_path, selected)
    foreground = capacity.reserve("codex", "foreground", 1)
    session = launch(queue, store, args)
    entry, = queue.entries()
    assert not queue.try_claim(entry["scope"], session), "role work claimed a different runtime's lane"
    assert capacity.holders("codex") == ["foreground"]
    assert capacity.holders("native") == []
    capacity.release(foreground)
    assert queue.try_claim(scope_for("codex"), session)
    assert capacity.holders("codex") == [session]
    assert queue.release(scope_for("codex"), session)


@pytest.mark.parametrize("change", ["runtime", "model"])
async def test_a_role_changed_while_queued_refuses_before_selection(tmp_path, change):
    user_file, capacity, queue, store, args = environment(tmp_path)
    session = launch(queue, store, args)
    user_file.write_text("version: 1\ndefaults: {role: worker}\nroles:\n"
                         + ("  worker: {harness: native}\n" if change == "runtime" else
                            "  worker: {harness: codex, model_id: changed-model}\n"))
    before = (queue.root / "state.json").read_bytes()
    called = []

    async def runner(args):
        called.append(args)
        return 0

    with pytest.raises(bg_sessions.BackgroundRefused):
        await bg_sessions.run_worker_async(session, queue=queue, store=store, runner=runner)
    assert not called
    assert (queue.root / "state.json").read_bytes() == before
    assert not capacity.root.exists()


def test_a_dynamic_role_fallback_refuses_before_creating_launch_state(tmp_path):
    _, capacity, queue, store, args = environment(
        tmp_path, extra=", fallback: [{harness: native}]")
    spawned = []
    with pytest.raises(bg_sessions.BackgroundRefused, match="fallback"):
        bg_sessions.launch(args, queue=queue, store=store, spawn=spawned.append)
    assert not spawned
    assert not store.root.exists()
    assert not queue.root.exists()
    assert not capacity.root.exists()


@pytest.mark.parametrize("changed_default", [False, True])
async def test_a_default_role_preserves_permissions_and_its_runtime_alias_at_dispatch(tmp_path, changed_default):
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.interfaces.main import _apply_role

    user_file, capacity, queue, store, args = environment(
        tmp_path, harness="quick", extra=", permissions: readonly")
    user_file.write_text(user_file.read_text() + "  other: {harness: native}\n")
    settings = Path(args.workspace) / ".agent" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text("runtime_refs: [{alias: quick, runtime_id: codex, capabilities: [prompt]}]\n")
    session = launch(queue, store, args)
    if changed_default:
        user_file.write_text(user_file.read_text().replace("role: worker", "role: other"))
    observed = []

    async def runner(received):
        assert (received.runtime, received.role) == ("quick", "worker")
        catalog = prepare_runtime_catalog(received.workspace)
        resolved = garuda_yaml.load_effective(received.workspace, cli_role=received.role,
                                              cli_runtime=received.runtime)
        plan = _apply_role(received, resolved, catalog)
        assert plan.runtime_id == "codex"
        assert received.permission_mode == "readonly"
        assert not catalog.registry.get(received.runtime).capabilities.supports("cancel")
        reservation = capacity.reserve(plan.runtime_id, session, 1)
        assert capacity.holders("native") == []
        observed.append(capacity.holders("codex"))
        capacity.release(reservation)
        return 0

    assert await bg_sessions.run_worker_async(session, queue=queue, store=store, runner=runner) == 0
    assert observed == [[session]]
    assert capacity.holders("codex") == []
