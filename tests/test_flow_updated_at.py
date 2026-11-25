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

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import engine

CONFIG = ("version: 1\nroles:\n  coder: {harness: fakeacp, permissions: yolo}\n"
          "flows:\n  one:\n    steps:\n      - {id: build, role: coder}\n")


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


@pytest.mark.parametrize("checks", [(), ("--check", "true")], ids=["unchecked", "checked"])
def test_a_completed_flow_advances_updated_at(repo, tmp_path, monkeypatch, capsys, checks):
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

    code, out = _main(monkeypatch, capsys, "flow", "run", "one", "-t", "add a greeting",
                      "--workspace", str(repo), *checks)
    assert code == 0, out
    flow_id = SessionStore().resolve("latest", kind="flow")
    meta = SessionStore().load_meta(flow_id)
    assert meta["flow_state"] == "completed"
    created, updated = _when(meta["created_at"]), _when(meta["updated_at"])
    assert updated > created, meta
    (receipt,) = engine.receipts(SessionStore(), flow_id)
    # The completion publication is the last write: no earlier than the step settled.
    assert updated >= _when(receipt["finished_at"]).replace(microsecond=0)
    assert dt.datetime.now(dt.timezone.utc) - updated < dt.timedelta(minutes=5)

    # The step receipt itself advanced the flow's recency, before completion.
    at = next(i for i, (kind, _) in enumerate(events) if kind == "receipt")
    after = [rec for kind, rec in events[at + 1:]
             if kind == "meta" and rec["session_id"] == flow_id]
    assert after and "updated_at" in after[0] and after[0].get("flow_state") != "completed", after
    assert _when(after[0]["updated_at"]) >= _when(receipt["finished_at"]).replace(microsecond=0)

    shown = json.loads(_main(monkeypatch, capsys, "sessions", "show", flow_id, "--json")[1])
    assert shown["updated_at"] == meta["updated_at"] and shown["created_at"] == meta["created_at"]
