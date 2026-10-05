"""`garuda doctor --recover-project-ids` (#157, plan task B.1)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from garuda.core import project_recovery as recovery
from garuda.core.project_identity import ProjectKeyMissing
from garuda.core.project_recovery import RecoveryRefused, recover_project_ids
from garuda.core.sessions import SessionStore
from garuda.workspace.lease import LeaseStore


def _repo(path: Path, history: str = "original") -> Path:
    """A repository with one commit; ``history`` makes its root commit its own."""
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, env=env)
    (path / "README").write_text(f"{path.name}: {history}\n")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True, capture_output=True,
                   env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", history], check=True,
                   capture_output=True, env=env)
    return path


def _begin(store, sid, ws, **kw):
    store.begin(sid, task="do the thing", model="m", agent="a", workspace=str(ws), **kw)
    return store.load_meta(sid)


@pytest.fixture
def lost(tmp_path):
    """Two projects with named sessions, then the key is lost."""
    store = SessionStore(tmp_path / "sessions")
    a, b = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    old_a = _begin(store, "sa", a, name="alpha")["project_id"]
    old_b = _begin(store, "sb", b, name="beta")["project_id"]
    (tmp_path / "sessions" / ".identity" / "key").unlink()
    return store, a, b, old_a, old_b


def _leases(tmp_path):
    return LeaseStore(tmp_path / "leases")


def test_recovery_restores_identity_names_and_new_sessions(tmp_path, lost):
    store, a, b, old_a, old_b = lost
    with pytest.raises(ProjectKeyMissing):
        _begin(store, "blocked", a)

    report = recover_project_ids(store.root, leases=_leases(tmp_path))

    assert set(report.mapped) == {old_a, old_b} and report.unmapped == []
    meta = store.load_meta("sa")
    assert meta["project_id"] == report.mapped[old_a]
    assert meta["previous_project_ids"] == [old_a]
    assert store.resolve("alpha", workspace=a) == "sa"
    assert _begin(store, "sa2", a)["project_id"] == meta["project_id"]  # new sessions join it


def test_a_moved_or_replaced_repository_stays_unmapped(tmp_path, lost):
    store, a, b, old_a, old_b = lost
    shutil.rmtree(b)
    _repo(b, history="someone else's")  # same path, a different repository

    report = recover_project_ids(store.root, leases=_leases(tmp_path))

    assert report.unmapped == [old_b] and old_a in report.mapped
    assert store.load_meta("sb")["project_id"] == old_b
    assert store.resolve("sb") == "sb"  # still reachable by full id


def test_a_different_history_in_the_same_directory_stays_unmapped(tmp_path, lost):
    """The directory (and its inode) survive; the repository inside is another."""
    store, a, b, old_a, old_b = lost
    inode = os.stat(b).st_ino
    shutil.rmtree(b / ".git")
    (b / "README").unlink()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "u",
           "GIT_AUTHOR_EMAIL": "u@u", "GIT_COMMITTER_NAME": "u", "GIT_COMMITTER_EMAIL": "u@u"}
    subprocess.run(["git", "init", "-q", str(b)], check=True, capture_output=True, env=env)
    (b / "OTHER").write_text("unrelated\n")
    subprocess.run(["git", "-C", str(b), "add", "OTHER"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(b), "commit", "-qm", "unrelated"], check=True,
                   capture_output=True, env=env)
    assert os.stat(b).st_ino == inode

    report = recover_project_ids(store.root, leases=_leases(tmp_path))

    assert report.unmapped == [old_b] and old_a in report.mapped


def test_recovery_refuses_while_a_session_may_be_running(tmp_path, lost):
    store, a, *_ = lost
    leases = _leases(tmp_path)
    leases.acquire(a, "sa", "mutating")  # owned by this live process

    with pytest.raises(RecoveryRefused, match="may still be running"):
        recover_project_ids(store.root, leases=leases)
    assert not (store.root / ".identity" / "key").exists()


def test_an_interrupted_recovery_resumes_without_mixing_keys(tmp_path, lost, monkeypatch):
    store, a, b, old_a, old_b = lost
    real_apply = recovery._apply

    def crash_after_first_session(root, plan):
        first = next(iter(plan["sessions"]))
        real_apply(root, {**plan, "sessions": {first: plan["sessions"][first]}, "mapping": {}})
        raise KeyboardInterrupt("crash mid-apply")

    monkeypatch.setattr(recovery, "_apply", crash_after_first_session)
    with pytest.raises(KeyboardInterrupt):
        recover_project_ids(store.root, leases=_leases(tmp_path))
    monkeypatch.undo()
    assert (store.root / ".identity" / "recovery.json").exists()

    report = recover_project_ids(store.root, leases=_leases(tmp_path))

    assert report.resumed is True
    ids = {store.load_meta("sa")["project_id"], store.load_meta("sb")["project_id"]}
    assert ids == set(report.mapped.values())
    assert store.resolve("beta", workspace=b) == "sb"
    assert not (store.root / ".identity" / "recovery.json").exists()


@pytest.mark.parametrize("phase", ["key", "before-alias", "after-alias"])
def test_a_crash_after_publishing_the_key_still_finishes(tmp_path, lost, monkeypatch, phase):
    store, a, *_ = lost
    real_write = recovery.write_document
    calls = {"n": 0}

    def crash_on_published(path, doc):
        if phase == "key" and doc.get("state") == "published" and path.name == "recovery.json":
            calls["n"] += 1
            raise KeyboardInterrupt("crash after os.replace(key)")
        if path.name == "project-aliases.json" and phase in {"before-alias", "after-alias"}:
            calls["n"] += 1
            if phase == "after-alias":
                real_write(path, doc)
            raise KeyboardInterrupt("crash around alias publication")
        return real_write(path, doc)

    monkeypatch.setattr(recovery, "write_document", crash_on_published)
    with pytest.raises(KeyboardInterrupt):
        recover_project_ids(store.root, leases=_leases(tmp_path))
    monkeypatch.undo()
    from garuda.interfaces.web.routes import DashboardContext
    from tests.test_web_routes import PORT, TOKEN, call

    assert calls["n"] == 1
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store)
    assert call(ctx, "/api/usage", query="range=24h").status == 400  # no mixed epoch report
    report = recover_project_ids(store.root, leases=_leases(tmp_path))
    assert report.resumed and store.resolve("alpha", workspace=a) == "sa"
    assert call(ctx, "/api/usage", query="range=24h").status == 200


def test_repeating_a_finished_recovery_is_a_noop(tmp_path, lost):
    store, *_ = lost
    recover_project_ids(store.root, leases=_leases(tmp_path))
    before = json.dumps(store.load_meta("sa"), sort_keys=True)

    assert recover_project_ids(store.root, leases=_leases(tmp_path)).noop is True
    assert json.dumps(store.load_meta("sa"), sort_keys=True) == before


def test_the_doctor_command_reports_and_refuses(tmp_path, lost, monkeypatch, capsys):
    import garuda.interfaces.main as main

    store, *_ = lost
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(store.root))
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    args = main.build_parser().parse_args(["doctor", "--recover-project-ids"])

    assert main.run_doctor(args) == 0
    assert "Recovered 2 project(s)" in capsys.readouterr().out
    assert main.run_doctor(args) == 0
    assert "nothing to recover" in capsys.readouterr().out


@pytest.mark.parametrize("epochs, unmapped", [(1, False), (2, False), (1, True)])
def test_recovery_keeps_one_usage_group_without_rewriting_ledger(tmp_path, lost, epochs, unmapped):
    import time

    from garuda.interfaces.web.routes import DashboardContext
    from garuda.observability.ledger import Ledger
    from tests.test_web_routes import PORT, TOKEN, body, call

    store, a, b, old_a, old_b = lost
    if unmapped:
        shutil.rmtree(b)
    ledger = Ledger()
    now = time.time()
    ids = [old_a]

    def record(key, project, tokens):
        ledger.append({"kind": "native_model_call", "key": key, "time": now - 60,
                       "project_id": project, "total_tokens": tokens, "cost_usd": tokens / 110})

    record("before", old_a, 11)
    record("unrelated", old_b, 7)
    mapped_b = old_b
    for epoch in range(epochs):
        if epoch:
            (store.root / ".identity" / "key").unlink()
        before = {p.name: p.read_bytes() for p in ledger.files()}
        report = recover_project_ids(store.root, leases=_leases(tmp_path))
        assert {p.name: p.read_bytes() for p in ledger.files()} == before
        ids.append(_begin(store, f"after-{epoch}", a)["project_id"])
        mapped_b = report.mapped.get(mapped_b, mapped_b)
        record(f"after-{epoch}", ids[-1], 22 + 11 * epoch)
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store)
    response = call(ctx, "/api/usage", query="range=24h")
    assert response.status == 200
    usage = body(response)
    key = (store.root / ".identity" / "key").read_text()
    assert key not in response.body.decode()
    assert str(a) not in response.body.decode() and str(b) not in response.body.decode()
    projects = {r["project_id"]: r for r in usage["by_project"]}
    assert set(projects) == {ids[-1], mapped_b}
    expected = 33 if epochs == 1 else 66
    assert projects[ids[-1]]["native_calls"] == epochs + 1
    assert projects[ids[-1]]["total_tokens"] == expected
    assert projects[ids[-1]]["cost"]["known_usd"] == expected / 110
    assert projects[mapped_b]["total_tokens"] == 7
    assert usage["measures"]["native_calls"] == epochs + 2
    assert usage["measures"]["total_tokens"] == expected + 7
    # Export and storage retain the original opaque ids, never recovery key/path data.
    export = call(ctx, "/api/usage/export", query="range=24h&format=json")
    assert export.status == 200
    exported = body(export)
    assert {r["project_id"] for r in exported["rows"]} == {*ids, old_b}
    assert str(a) not in export.body.decode() and str(b) not in export.body.decode()
    before = {p.name: p.read_bytes() for p in ledger.files()}
    assert recover_project_ids(store.root, leases=_leases(tmp_path)).noop
    assert body(call(ctx, "/api/usage", query="range=24h"))["by_project"] == usage["by_project"]
    assert {p.name: p.read_bytes() for p in ledger.files()} == before


@pytest.mark.parametrize("damage", ["future", "key", "chain", "cycle", "extra", "symlink"])
def test_usage_refuses_unverifiable_aliases_without_exposing_source(tmp_path, lost, damage):
    from garuda.interfaces.web.routes import DashboardContext
    from tests.test_web_routes import PORT, TOKEN, body, call

    store, _, _, old_a, _ = lost
    report = recover_project_ids(store.root, leases=_leases(tmp_path))
    path = store.root / ".identity" / "project-aliases.json"
    private = "PRIVATE-ALIAS-MANIFEST-CANARY"
    doc = json.loads(path.read_text())
    new = report.mapped[old_a]
    if damage == "future":
        doc["version"] = 2
    elif damage == "key":
        doc["key_digest"] = "a" * 64
    elif damage == "chain":
        doc["aliases"][new] = "p1_" + "b" * 32
    elif damage == "cycle":
        doc["aliases"][new] = old_a
    elif damage == "extra":
        doc["source"] = private
    if damage == "symlink":
        other = tmp_path / private
        path.rename(other)
        path.symlink_to(other)
    else:
        path.write_text(json.dumps(doc))
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store)
    response = call(ctx, "/api/usage", query="range=24h")
    assert response.status == 400
    assert private not in response.body.decode() and str(store.root) not in response.body.decode()
    assert body(response)["error"]
    # Raw exports stay readable: they do not merge identity groups or consume aliases.
    assert call(ctx, "/api/usage/export", query="range=24h&format=json").status == 200


def test_a_name_collision_refuses_before_staging_a_recovery_epoch(tmp_path, lost):
    store, _, _, old_a, old_b = lost
    a_meta = store.load_meta("sa")
    # Two historical identity records now claim the same verified repository/name.
    store.update_meta("sb", {"project_path": a_meta["project_path"],
                             "project_fs_id": a_meta["project_fs_id"], "name": "alpha"})
    names = store.root / ".names"
    (names / old_b / "beta").rename(names / old_b / "alpha")
    before = {sid: store.load_meta(sid) for sid in ("sa", "sb")}
    with pytest.raises(RecoveryRefused, match="name conflicts"):
        recover_project_ids(store.root, leases=_leases(tmp_path))
    assert {sid: store.load_meta(sid) for sid in ("sa", "sb")} == before
    ident = store.root / ".identity"
    assert not any((ident / name).exists() for name in (
        "key", "key.next", "recovery.json", "project-aliases.json"))
    assert (names / old_a / "alpha").is_dir() and (names / old_b / "alpha").is_dir()


@pytest.mark.parametrize("damage", ["key", "key_digest", "aliases", "epoch"])
def test_an_altered_or_unbound_epoch_cannot_apply_session_changes(tmp_path, lost, monkeypatch,
                                                                 damage):
    store, _, _, old_a, old_b = lost
    real_apply = recovery._apply

    def crash_before_apply(*_args):
        raise KeyboardInterrupt("staged but not applied")

    monkeypatch.setattr(recovery, "_apply", crash_before_apply)
    with pytest.raises(KeyboardInterrupt):
        recover_project_ids(store.root, leases=_leases(tmp_path))
    monkeypatch.setattr(recovery, "_apply", real_apply)
    ident = store.root / ".identity"
    if damage == "key":
        (ident / "key.next").write_text("a" * 64)
    else:
        journal = ident / "recovery.json"
        plan = json.loads(journal.read_text())
        plan.pop(damage, None)  # historical/unbound pending record, never guess authority
        journal.write_text(json.dumps(plan))
    before = {sid: store.load_meta(sid) for sid in ("sa", "sb")}
    with pytest.raises(RecoveryRefused):
        recover_project_ids(store.root, leases=_leases(tmp_path))
    assert {sid: store.load_meta(sid) for sid in ("sa", "sb")} == before
    assert not (ident / "key").exists() and not (ident / "project-aliases.json").exists()
    assert (ident / "key.next").exists() and (ident / "recovery.json").exists()
    names = store.root / ".names"
    assert (names / old_a / "alpha").is_dir() and (names / old_b / "beta").is_dir()
