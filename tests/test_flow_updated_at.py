"""A flow session's ``updated_at`` advances with its state (#763).

``begin`` stamps ``created_at`` and ``updated_at`` together, and the store's
merge only defaults ``updated_at``, so a flow that ran, wrote receipts and
completed used to report ``updated_at == created_at`` forever. Every flow
publication and every step receipt now advances it; ``created_at`` never moves.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import uuid

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import engine

CONFIG = ("version: 1\nroles:\n  coder: {harness: fakeacp, permissions: yolo}\n"
          "flows:\n  one:\n    steps:\n      - {id: build, role: coder}\n"
          "  three:\n    steps:\n      - {id: gather, role: coder}\n"
          "      - {id: sum, role: coder}\n      - {id: final, role: coder}\n")

# The #776 repro shape: three steps handing typed artifacts forward.
THREE = {"version": 1, "roles": {"c": {"harness": "native"}},
         "flows": {
             "one": {"steps": [{"id": "gather", "role": "c", "outputs": ["notes"]}]},
             "three": {"steps": [
                 {"id": "gather", "role": "c", "outputs": ["notes"]},
                 {"id": "sum", "role": "c", "inputs": ["notes"], "outputs": ["summary"]},
                 {"id": "final", "role": "c", "inputs": ["summary", "notes"],
                  "outputs": ["findings"]}]}}}
KINDS = {"gather": "notes", "sum": "summary", "final": "findings"}


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (path / "a.txt").write_text("a\n")
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)
    return path


def _when(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


@pytest.mark.parametrize("flow", ["one", "three"])
@pytest.mark.parametrize("checks", [(), ("--check", "true")], ids=["unchecked", "checked"])
def test_a_completed_flow_advances_updated_at(repo, tmp_path, monkeypatch, capsys, checks, flow):
    from tests.test_runtime_cli import _install_shim, _main, _on_path, _trusted_settings

    if checks and sys.platform != "linux":
        pytest.skip("owned flow checks require Linux")
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(CONFIG)

    # Record every flow-session meta write and when each step receipt lands.
    events: list[tuple[str, dict]] = []
    real_update, real_write = SessionStore.update_meta, engine._write_once

    def update_meta(self, session_id, updates):
        events.append(("meta", {"session_id": session_id, **updates}))
        return real_update(self, session_id, updates)

    def write_once(path, record):
        real_write(path, record)
        if path.parent.name == "receipts":
            events.append(("receipt", dict(record)))

    monkeypatch.setattr(SessionStore, "update_meta", update_meta)
    monkeypatch.setattr(engine, "_write_once", write_once)

    code, out = _main(monkeypatch, capsys, "flow", "run", flow, "-t", "add a greeting",
                      "--workspace", str(repo), *checks)
    assert code == 0, out
    flow_id = SessionStore().resolve("latest", kind="flow")
    meta = SessionStore().load_meta(flow_id)
    assert meta["flow_state"] == "completed"
    created, updated = _when(meta["created_at"]), _when(meta["updated_at"])
    assert updated > created, meta
    flow_receipts = engine.receipts(SessionStore(), flow_id)
    assert len(flow_receipts) == {"one": 1, "three": 3}[flow]
    receipt = flow_receipts[-1]
    # The completion publication is the last write: no earlier than the step settled.
    assert updated >= _when(receipt["finished_at"]).replace(microsecond=0)
    assert dt.datetime.now(dt.timezone.utc) - updated < dt.timedelta(minutes=5)

    # The last step receipt itself advanced the flow's recency, before completion.
    at = max(i for i, (kind, _) in enumerate(events) if kind == "receipt")
    after = [rec for kind, rec in events[at + 1:]
             if kind == "meta" and rec["session_id"] == flow_id]
    assert after and "updated_at" in after[0] and after[0].get("flow_state") != "completed", after
    assert _when(after[0]["updated_at"]) >= _when(receipt["finished_at"]).replace(microsecond=0)

    shown = json.loads(_main(monkeypatch, capsys, "sessions", "show", flow_id, "--json")[1])
    assert shown["updated_at"] == meta["updated_at"] and shown["created_at"] == meta["created_at"]


def _stale(store, flow_id) -> str:
    """Pin ``updated_at`` back to ``created_at``, as the #776 report observed."""
    created = store.load_meta(flow_id)["created_at"]
    store.update_meta(flow_id, {"updated_at": created})
    return created


def _flow_writes(monkeypatch) -> list[dict]:
    from garuda.core import sessions

    writes: list[dict] = []
    real = sessions.merge_meta

    def merge_meta(meta_path, updates, **kw):
        writes.append({"session_id": meta_path.parent.name, **updates})
        return real(meta_path, updates, **kw)

    monkeypatch.setattr(sessions, "merge_meta", merge_meta)
    return writes


def _artifact_step(kind):
    async def launch(_launch):
        return engine.StepResult(str(uuid.uuid4()), True,
                                 f'<garuda-artifact type="{kind}">\nok\n</garuda-artifact>')
    return launch


def _runner(repo, flow, behaviours, **kw):
    resolved = gy.resolve(gy.parse(THREE), None)

    async def launcher(launch):
        return await behaviours[launch.step_id](launch)

    return engine.FlowRunner(SessionStore(), repo, flow, resolved.config["flows"][flow], resolved,
                             task="t", launcher=launcher, **kw)


@pytest.mark.parametrize("flow", ["one", "three"])
async def test_every_unchecked_flow_publication_advances_updated_at(repo, monkeypatch, flow):
    """#776: a 3-step unchecked flow must advance ``updated_at`` like a 1-step one."""
    writes = _flow_writes(monkeypatch)
    result = await _runner(repo, flow, {s: _artifact_step(k) for s, k in KINDS.items()}).run()
    assert result.completed
    meta = SessionStore().load_meta(result.flow_session)
    flow_writes = [w for w in writes if w["session_id"] == result.flow_session]
    # After `begin`, every flow-session write carries its own fresh stamp.
    begun = next(i for i, w in enumerate(flow_writes) if w.get("kind") == "flow")
    later = flow_writes[begun + 1:]
    assert later and all("updated_at" in w for w in later), later
    stamps = [_when(w["updated_at"]) for w in later]
    assert stamps == sorted(stamps)
    assert len(later) >= len(THREE["flows"][flow]["steps"]) + 1  # each receipt + completion
    created, updated = _when(meta["created_at"]), _when(meta["updated_at"])
    assert updated > created and updated == stamps[-1]
    last = engine.receipts(SessionStore(), result.flow_session)[-1]
    assert updated >= _when(last["recorded_at"])


async def test_a_resumed_three_step_flow_advances_updated_at(repo):
    async def fails(_launch):
        return engine.StepResult(str(uuid.uuid4()), False, "no artifact")

    store = SessionStore()
    first = await _runner(repo, "three", {**{s: _artifact_step(k) for s, k in KINDS.items()},
                                          "sum": fails}).run()
    assert not first.completed
    created = _stale(store, first.flow_session)
    resumed = await _runner(repo, "three", {s: _artifact_step(k) for s, k in KINDS.items()},
                            flow_session=first.flow_session).run(resume=True)
    assert resumed.completed
    meta = store.load_meta(first.flow_session)
    assert meta["created_at"] == created and _when(meta["updated_at"]) > _when(created)
    last = engine.receipts(store, first.flow_session)[-1]
    assert _when(meta["updated_at"]) >= _when(last["recorded_at"])


async def test_recovery_quarantine_advances_updated_at(repo):
    store = SessionStore()
    result = await _runner(repo, "three", {s: _artifact_step(k) for s, k in KINDS.items()}).run()
    created = _stale(store, result.flow_session)
    # A launched attempt the dead worker never settled.
    engine._append(engine.flow_dir(store, result.flow_session) / engine.JOURNAL,
                   {"event": "intent", "step": "final", "index": 2, "attempt": 2,
                    "at": engine._now()})
    assert engine.recover(store, result.flow_session) == ["final"]
    meta = store.load_meta(result.flow_session)
    assert meta["flow_state"] == "quarantined"
    assert meta["created_at"] == created and _when(meta["updated_at"]) > _when(created)
