"""Real adapter → producer records → supervised children → retained parent lease."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from garuda.core.acceptance import fingerprint
from garuda.core.acceptance_request import (
    CheckContext,
    CheckLimits,
    FrozenCheck,
    FrozenCheckRequest,
    FrozenSource,
)
from garuda.core.acceptance_runner import AcceptanceRunner, CheckExecutionUncertain
from garuda.core.acceptance_work import AcceptanceWorkError, owned_call
from garuda.core.acceptance_worker import AuthorityChanged, _sources
from garuda.core.permissions import DelegatedPermissionEngine, PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.workspace.lease import LeaseError, LeaseStore

linux = pytest.mark.skipif(sys.platform != "linux", reason="Linux acceptance supervision")


async def _appears(path):
    deadline = asyncio.get_running_loop().time() + 8
    while not path.exists():
        assert asyncio.get_running_loop().time() < deadline, "real child did not reach its gate"
        await asyncio.sleep(0.01)


@pytest.fixture
async def case(tmp_path):
    root = tmp_path.resolve()
    ws = root / "workspace"
    ws.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    for argv in (("git", "init", "-q"), ("git", "commit", "--allow-empty", "-qm", "base")):
        subprocess.run(argv, cwd=ws, env=env, check=True)
    sid = str(uuid.uuid4())
    store = SessionStore(root / "sessions")
    leases = LeaseStore(root / "leases")
    owner = WorkspaceLeaseGuard(str(ws), sid, lease_store=leases, ttl_sec=1.2)
    owner.acquire()
    store.begin(sid, "check phase", "none", "flow", str(ws))
    owner.start_heartbeat()
    candidate = ((await owned_call("fingerprint", {"workspace": str(ws)}))["fingerprint"]
                 if sys.platform == "linux" else fingerprint(ws))
    info = ws.stat()
    context = CheckContext(sid, str(uuid.uuid4()), str(ws), candidate, "a" * 64, (), True,
                           (info.st_dev, info.st_ino))
    try:
        yield root, store, owner, context
    finally:
        await owner.release()


def _request(checks, permissions, *, sources=(), timeout=3):
    return FrozenCheckRequest.freeze(checks, sources, permissions,
                                    limits=CheckLimits(timeout, 20, 0.1, 0.5))


def _python(script, **kwargs):
    return FrozenCheck((sys.executable, "-c", script), "user-request", **kwargs)


def test_absent_source_snapshot_requires_continued_absence(tmp_path):
    path = tmp_path / "not-created" / "garuda.yaml"
    frozen = FrozenSource(str(path), None)
    assert _sources({"sources": [frozen.document()]}) == {"valid": True}
    path.parent.mkdir()
    path.write_text("version: 1\nchecks: [true]\n")
    with pytest.raises(AuthorityChanged, match="appeared"):
        _sources({"sources": [frozen.document()]})


@pytest.mark.asyncio
async def test_actual_unsupported_platform_has_no_check_launch(case):
    if sys.platform == "linux":
        pytest.skip("actual unsupported-platform control")
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    marker = root / "launched"
    request = _request([_python(f"open({str(marker)!r},'w').close()")], permissions)
    result = await AcceptanceRunner(store).run(request, context, owner, permissions)
    assert result.verification == {"status": "unavailable", "code": "verification.unsupported_supervisor"}
    assert not marker.exists()
    assert not (store.session_dir(context.parent_id) / f"acceptance-{context.phase_id}").exists()


@linux
@pytest.mark.asyncio
async def test_real_check_produces_bound_redacted_receipts_and_does_not_replay(case):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    marker = root / "launches"
    secret = "sk-fake-acceptance-secret-123456789"
    script = f"import os,sys;open({str(marker)!r},'a').write(str(os.getpid())+'\\n');print(os.environ['CHECK_SECRET']);print('done');sys.stdout.write(chr(0))"
    request = _request([_python(script, env=(("CHECK_SECRET", secret),))], permissions)
    runner = AcceptanceRunner(store)
    result = await runner.run(request, context, owner, permissions)
    assert result.verification["status"] == "passed"
    directory = store.session_dir(context.parent_id) / f"acceptance-{context.phase_id}"
    receipt = json.loads(next(directory.glob("receipt-*.json")).read_text())
    intent = json.loads(next(directory.glob("intent-*.json")).read_text())
    assert intent["phase"] == "terminal"
    assert receipt["binding"]["owner"]["pid"] == os.getpid()
    assert receipt["binding"]["candidate"] == context.candidate
    assert receipt["binding"]["authority_digest"] == request.authority_digest
    assert receipt["command"]["pid"] == int(marker.read_text().strip())
    assert receipt["execution"]["exit_code"] == 0
    assert receipt["cleanup"]["echild"] is True
    assert "\0" in receipt["result"]["output_tail"]
    assert secret not in "".join(path.read_text() for path in directory.glob("*.json"))
    raw = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(raw).hexdigest() == intent["receipt_digest"] == result.receipt_refs[0]["digest"]
    again = await runner.run(request, context, owner, permissions)
    assert again.verification["status"] == "passed"
    assert again.receipts == result.receipts
    assert len(marker.read_text().splitlines()) == 1
    assert (await runner.reconcile(receipt["binding"]))["classification"] == "terminal"


@linux
@pytest.mark.asyncio
async def test_parent_heartbeat_progresses_during_check_and_blocked_receipt_publication(case):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    entered, release = root / "entered", root / "release"
    script = f"import pathlib,time;pathlib.Path({str(entered)!r}).touch();exec(\"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)\")"
    task = asyncio.create_task(owner.race(AcceptanceRunner(store).run(
        _request([_python(script)], permissions), context, owner, permissions)))
    try:
        await _appears(entered)
        before = owner.leases.holders_of(context.workspace)[0].heartbeat_at
        for _ in range(10):
            await asyncio.sleep(0.05)
            assert not task.done()
        assert owner.leases.holders_of(context.workspace)[0].heartbeat_at > before
        directory = store.session_dir(context.parent_id) / f"acceptance-{context.phase_id}"
        lock = os.open(directory / ".lock", os.O_RDWR)
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(.01)
            release.touch()
            before = owner.leases.holders_of(context.workspace)[0].heartbeat_at
            for _ in range(10):
                await asyncio.sleep(.05)
                assert not task.done()
            assert owner.leases.holders_of(context.workspace)[0].heartbeat_at > before
            assert not list(directory.glob("receipt-*.json"))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
        assert (await task).verification["status"] == "passed"
    finally:
        release.touch()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", ["tool", "command", "path", "operand", "delegated"])
async def test_actual_check_ceiling_refuses_before_a_child_launch(case, ceiling):
    root, store, owner, context = case
    marker = root / "forbidden"
    kwargs = {"mode": "auto"}
    if ceiling in {"tool", "delegated"}:
        kwargs["tool_rules"] = {"bash": "deny"}
    elif ceiling == "command":
        kwargs["bash_rules"] = {"deny": ["forbidden"]}
    elif ceiling == "path":
        kwargs["path_rules"] = {"deny": [context.workspace]}
    else:
        kwargs["path_rules"] = {"deny": [str(root / "operand")]}
    permissions = PermissionEngine(**kwargs)
    if ceiling == "delegated":
        permissions = DelegatedPermissionEngine(PermissionEngine(mode="auto"), permissions)
    check = _python(f"open({str(marker)!r},'w').close()")
    if ceiling == "operand":
        check = replace(check, run=(*check.run, str(root / "operand")))
    request = _request([check], permissions)
    result = await AcceptanceRunner(store).run(request, context, owner, permissions)
    assert result.verification["status"] == "unavailable"
    assert result.receipts[0]["status"] == "refused"
    assert not marker.exists()


@linux
@pytest.mark.asyncio
async def test_docker_check_is_unavailable_without_host_fallback(case):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    marker = root / "forbidden"
    request = _request([_python(f"open({str(marker)!r},'w').close()", mode="docker")], permissions)
    result = await AcceptanceRunner(store).run(request, context, owner, permissions)
    assert result.verification["status"] == "unavailable"
    assert result.receipts[0]["status"] == "skipped"
    assert not marker.exists()


@linux
@pytest.mark.asyncio
async def test_tree_changing_check_voids_its_receipt_and_stops_later_checks(case):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    later = root / "later"
    request = _request([_python("open('changed','w').write('x')"),
                        _python(f"open({str(later)!r},'w').close()")], permissions)
    result = await AcceptanceRunner(store).run(request, context, owner, permissions)
    assert result.verification == {"status": "invalidated", "code": "verification.check_changed_tree"}
    assert result.receipts[0]["status"] == "void"
    assert len(result.receipt_refs) == 1
    assert not later.exists()


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "ceiling", "regex-flags"])
async def test_frozen_authority_change_invalidates_completed_check_without_launching_the_next(case, change):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto", bash_rules={"deny": [re.compile("LATER")]})
    source, entered, release, later = (root / name for name in ("source.yaml", "entered", "release", "later"))
    source.write_text("reviewed source")
    frozen = FrozenSource(str(source), hashlib.sha256(source.read_bytes()).hexdigest())
    script = f"import pathlib,time;pathlib.Path({str(entered)!r}).touch();exec(\"while not pathlib.Path({str(release)!r}).exists():time.sleep(.01)\")"
    request = _request([_python(script), _python(f"open({str(later)!r},'w').close()")], permissions, sources=[frozen])
    task = asyncio.create_task(AcceptanceRunner(store).run(request, context, owner, permissions))
    try:
        await _appears(entered)
        if change == "source":
            source.write_text("different source")
        elif change == "ceiling":
            permissions._tool_rules["bash"] = "deny"
        else:
            permissions._deny_bash[0] = re.compile("LATER", re.IGNORECASE)
        release.touch()
        result = await task
        assert result.verification == {"status": "invalidated", "code": "verification.authority_changed"}
        assert result.receipts[0]["status"] == "void"
        assert not later.exists()
    finally:
        release.touch()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@linux
@pytest.mark.asyncio
async def test_absent_frozen_source_appearing_refuses_check_launch(case):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    source = root / "new.yaml"
    source.write_text("new authority")
    marker = root / "launched"
    request = _request([_python(f"open({str(marker)!r},'w').close()")], permissions,
                       sources=[FrozenSource(str(source), None)])

    result = await AcceptanceRunner(store).run(request, context, owner, permissions)

    assert result.verification == {"status": "invalidated", "code": "verification.authority_changed"}
    assert not marker.exists()


@linux
@pytest.mark.asyncio
async def test_full_changed_path_input_reaches_the_async_infrastructure_guard(case):
    _, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    context = replace(context, changed_paths=tuple(f"src/{i}.py" for i in range(300)) + ("conftest.py",))
    request = _request([FrozenCheck(("pytest", "--version"), "user-request")], permissions)
    result = await AcceptanceRunner(store).run(request, context, owner, permissions)
    assert result.verification == {"status": "invalidated", "code": "verification.test_infra_changed"}
    assert result.receipts[0]["infra_changed"] == ["conftest.py"]


@linux
@pytest.mark.asyncio
async def test_lost_terminal_receipt_quarantines_the_parent_without_replay(case):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    marker = root / "launches"
    request = _request([_python(f"open({str(marker)!r},'a').write('x')")], permissions)
    runner = AcceptanceRunner(store)
    assert (await runner.run(request, context, owner, permissions)).verification["status"] == "passed"
    directory = store.session_dir(context.parent_id) / f"acceptance-{context.phase_id}"
    next(directory.glob("receipt-*.json")).unlink()
    with pytest.raises(CheckExecutionUncertain):
        await runner.run(request, context, owner, permissions)
    await owner.release()
    assert owner.leases.holders_of(context.workspace)
    assert marker.read_text() == "x"


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["cancel", "timeout", "supervisor-loss"])
async def test_actual_runner_settles_detached_writer_or_retains_quarantine(case, finish):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    marker, writes = root / "helper", root / "writes"
    script = f"""
import os,pathlib,signal,time
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0: os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))
    while True:
        with open({str(writes)!r}, 'a') as f: f.write('x')
        time.sleep(.005)
while True: time.sleep(.01)
"""
    task = asyncio.create_task(AcceptanceRunner(store).run(
        _request([_python(script)], permissions, timeout=3), context, owner, permissions))
    helper_fd = supervisor_fd = None
    try:
        await _appears(marker)
        await _appears(writes)
        pid = int(marker.read_text())
        helper_fd = os.pidfd_open(pid)
        directory = store.session_dir(context.parent_id) / f"acceptance-{context.phase_id}"
        intent_path = next(directory.glob("intent-*.json"))
        if finish == "cancel":
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif finish == "timeout":
            assert (await task).verification["status"] == "failed"
        else:
            intent = json.loads(intent_path.read_text())
            supervisor_fd = os.pidfd_open(intent["supervisor"]["pid"])
            signal.pidfd_send_signal(supervisor_fd, signal.SIGKILL)
            with pytest.raises(CheckExecutionUncertain):
                await task
            signal.pidfd_send_signal(helper_fd, 0)
            assert not list(directory.glob("receipt-*.json"))
            await owner.release()
            assert owner.leases.holders_of(context.workspace)
            return
        receipt = json.loads(next(directory.glob("receipt-*.json")).read_text())
        assert receipt["outcome"] == {"cancel": "cancelled", "timeout": "timeout"}[finish]
        assert receipt["cleanup"]["echild"] is True
        assert not Path(f"/proc/{pid}").exists()
        count = writes.stat().st_size
        await asyncio.sleep(.05)
        assert writes.stat().st_size == count
        await owner.release()
        assert not owner.leases.holders_of(context.workspace)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for fd in (helper_fd, supervisor_fd):
            if fd is not None:
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.close(fd)


@linux
@pytest.mark.asyncio
async def test_lost_authorization_ack_never_activates_or_replays_and_quarantines(case, monkeypatch):
    import garuda.core.acceptance_runner as adapter

    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    marker = root / "forbidden"

    async def lost_ack(operation, data, **kwargs):
        result = await owned_call(operation, data, **kwargs)
        if operation == "transition" and data["phase"] == "launch-authorized":
            raise AcceptanceWorkError("lost publication response")
        return result

    monkeypatch.setattr(adapter, "owned_call", lost_ack)
    runner = AcceptanceRunner(store)
    with pytest.raises(CheckExecutionUncertain):
        await runner.run(_request([_python(f"open({str(marker)!r},'w').close()")], permissions),
                         context, owner, permissions)
    intent = json.loads(next((store.session_dir(context.parent_id) / f"acceptance-{context.phase_id}").glob("intent-*.json")).read_text())
    assert intent["phase"] == "launch-authorized"
    assert (await runner.reconcile(intent["binding"]))["classification"] == "uncertain"
    assert not marker.exists()
    await owner.release()
    assert owner.leases.holders_of(context.workspace)


@linux
@pytest.mark.asyncio
async def test_delegated_ask_uses_the_actual_parent_broker_once(case):
    root, store, owner, context = case
    calls = []

    async def approve(action):
        calls.append(action)
        return True

    parent = PermissionEngine(mode="auto", tool_rules={"bash": "ask"})
    parent.install_approval_handler(approve)
    permissions = DelegatedPermissionEngine(PermissionEngine(mode="auto", tool_rules={"bash": "ask"}), parent)
    marker = root / "approved"
    result = await AcceptanceRunner(store).run(
        _request([_python(f"open({str(marker)!r},'w').close()")], permissions), context, owner, permissions)
    assert result.verification["status"] == "passed"
    assert marker.exists()
    assert len(calls) == 1


@linux
@pytest.mark.asyncio
async def test_explicit_new_phase_retains_both_actual_terminal_receipts(case):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    marker = root / "launches"
    request = _request([_python(f"open({str(marker)!r},'a').write('x')")], permissions)
    runner = AcceptanceRunner(store)
    second = replace(context, phase_id=str(uuid.uuid4()))
    for phase in (context, second):
        assert (await runner.run(request, phase, owner, permissions)).verification["status"] == "passed"
    assert marker.read_text() == "xx"
    receipts = list(store.session_dir(context.parent_id).glob("acceptance-*/receipt-*.json"))
    assert {json.loads(path.read_text())["binding"]["phase_id"] for path in receipts} == {context.phase_id, second.phase_id}


@linux
@pytest.mark.asyncio
async def test_competing_same_attempt_never_launches_twice(case):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    marker, release = root / "launches", root / "release"
    script = f"import pathlib,time;open({str(marker)!r},'a').write('x');exec(\"while not pathlib.Path({str(release)!r}).exists():time.sleep(.01)\")"
    request = _request([_python(script)], permissions)
    runner = AcceptanceRunner(store)
    first = asyncio.create_task(runner.run(request, context, owner, permissions))
    try:
        await _appears(marker)
        with pytest.raises(CheckExecutionUncertain):
            await runner.run(request, context, owner, permissions)
        release.touch()
        with pytest.raises(CheckExecutionUncertain):
            await first
        assert marker.read_text() == "x"
        await owner.release()
        assert owner.leases.holders_of(context.workspace)
    finally:
        release.touch()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@linux
@pytest.mark.asyncio
async def test_source_ancestor_replaced_by_link_invalidates_before_launch(case):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    directory = root / "definitions"
    directory.mkdir()
    source = directory / "checks.yaml"
    source.write_text("approved checks")
    frozen = FrozenSource(str(source), hashlib.sha256(source.read_bytes()).hexdigest())
    renamed = root / "moved-definitions"
    directory.rename(renamed)
    directory.symlink_to(renamed, target_is_directory=True)
    marker = root / "forbidden"
    result = await AcceptanceRunner(store).run(
        _request([_python(f"open({str(marker)!r},'w').close()")], permissions, sources=[frozen]),
        context, owner, permissions)
    assert result.verification == {"status": "invalidated", "code": "verification.authority_changed"}
    assert not marker.exists()


@pytest.mark.asyncio
async def test_copied_durable_lease_does_not_give_a_fresh_store_the_issued_capability(case):
    _, _, owner, context = case
    snapshot = owner.snapshot_ownership()
    foreign = LeaseStore(owner.leases.root)
    with pytest.raises(LeaseError):
        foreign.snapshot_issued(context.workspace, context.parent_id, epoch=snapshot["lease"]["epoch"])
    with pytest.raises(LeaseError):
        owner.leases.snapshot_issued(context.workspace, context.parent_id, epoch=str(uuid.uuid4()))


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize("record", ["intent", "registry", "policy"])
async def test_wrong_numeric_types_in_actual_producer_records_quarantine_without_replay(case, record):
    root, store, owner, context = case
    permissions = PermissionEngine(mode="auto")
    marker = root / "launches"
    request = _request([_python(f"open({str(marker)!r},'a').write('x')")], permissions)
    runner = AcceptanceRunner(store)
    assert (await runner.run(request, context, owner, permissions)).verification["status"] == "passed"
    parent = store.session_dir(context.parent_id)
    directory = parent / f"acceptance-{context.phase_id}"
    path = (next(directory.glob("intent-*.json")) if record == "intent" else
            directory / "request.json" if record == "registry" else
            parent / f"acceptance-policy-{context.phase_id}.json")
    document = json.loads(path.read_text())
    if record == "policy":
        document["directory"]["device"] = float(document["directory"]["device"])
    else:
        document["binding"]["owner"]["pid"] = float(document["binding"]["owner"]["pid"])
    path.write_text(json.dumps(document))
    with pytest.raises(CheckExecutionUncertain):
        await runner.run(request, context, owner, permissions)
    await owner.release()
    assert owner.leases.holders_of(context.workspace)
    assert marker.read_text() == "x"
