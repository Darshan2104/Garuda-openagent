"""The file-backed approval channel under attack (#157, plan task B.8)."""

import asyncio
import json
import os

import pytest

from garuda.acp import approval_channel as ac
from garuda.acp.approval_channel import FileApprovalChannel, write_answer
from garuda.acp.broker import ApprovalBroker
from garuda.core.permissions import PermissionEngine

SID = "11111111-2222-3333-4444-555555555555"


def _broker(tmp_path, *, runtime="native", timeout=3.0, answerer=None, engine=None):
    channel = FileApprovalChannel(tmp_path / "approvals", SID)
    broker = ApprovalBroker(engine=engine or PermissionEngine(), timeout_sec=timeout,
                            channel=channel, answerer=answerer)
    return broker, channel, broker.handler(session_id=SID, runtime_id=runtime)


async def _parked(channel, count=1):
    for _ in range(200):
        pending = channel.pending()
        if len(pending) >= count:
            return pending[count - 1]
        await asyncio.sleep(0.01)
    raise AssertionError("nothing was parked")


def _decision(channel, approval_id):
    return json.loads((channel.directory / f"{approval_id}.decision.json").read_text())


def _forge(channel, slot, **overrides):
    """Write the answer file for ``slot`` by hand (owner-only, exclusive)."""
    request = json.loads((channel.directory / f"{slot}.request.json").read_text())
    answer = {"version": 1, "session_id": SID, "approval_id": slot,
              "request_digest": request["digest"], "nonce": request["nonce"], "allow": True,
              **overrides}
    path = channel.directory / f"{slot}.answer.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, json.dumps(answer).encode())
    os.close(fd)


async def test_a_bound_file_answer_is_the_one_decision(tmp_path):
    _broker_, channel, handle = _broker(tmp_path)
    task = asyncio.ensure_future(handle("bash(rm build)"))
    request = await _parked(channel)
    assert oct(os.stat(channel.directory / f"{request['approval_id']}.request.json").st_mode)[-3:] == "600"

    write_answer(channel.directory, SID, request["approval_id"], allow=True)
    decision = await task

    assert bool(decision) is True
    assert _decision(channel, request["approval_id"])["via"] == "file"
    assert channel.unacknowledged() == []  # native: delivered as it returned
    with pytest.raises(FileExistsError):  # a second writer
        write_answer(channel.directory, SID, request["approval_id"], allow=False)


@pytest.mark.parametrize("attack, overrides, reason", [
    ("foreign session", {"session_id": "99999999-2222-3333-4444-555555555555"}, "another session"),
    ("foreign request", {"approval_id": "apr-0-0"}, "another request"),
    ("modified request", {"request_digest": "0" * 64}, "replayed or modified"),
    ("replayed nonce", {"nonce": "0" * 32}, "nonce"),
    ("no decision", {"allow": "yes"}, "no allow/deny"),
])
async def test_mismatched_answers_deny(tmp_path, attack, overrides, reason):
    _b, channel, handle = _broker(tmp_path)
    task = asyncio.ensure_future(handle("bash(deploy)"))
    request = await _parked(channel)
    _forge(channel, request["approval_id"], **overrides)

    assert bool(await task) is False, attack
    assert reason in _decision(channel, request["approval_id"])["reason"]


async def test_an_answer_replayed_from_an_earlier_request_denies(tmp_path):
    _b, channel, handle = _broker(tmp_path)
    first = asyncio.ensure_future(handle("bash(one)"))
    request = await _parked(channel)
    write_answer(channel.directory, SID, request["approval_id"], allow=True)
    assert bool(await first)

    second = asyncio.ensure_future(handle("bash(two)"))
    later = await _parked(channel)
    old = (channel.directory / f"{request['approval_id']}.answer.json").read_bytes()
    replay = json.loads(old)
    replay["approval_id"] = later["approval_id"]  # retargeted, still bound to the old request
    path = channel.directory / f"{later['approval_id']}.answer.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, json.dumps(replay).encode())
    os.close(fd)

    assert bool(await second) is False
    assert "replayed or modified" in _decision(channel, later["approval_id"])["reason"]


async def test_a_modified_request_cannot_be_answered(tmp_path):
    _b, channel, handle = _broker(tmp_path, timeout=0.5)
    task = asyncio.ensure_future(handle("bash(ls)"))
    request = await _parked(channel)
    path = channel.directory / f"{request['approval_id']}.request.json"
    tampered = {**json.loads(path.read_text()), "action": "bash(rm -rf /)"}
    path.chmod(0o600)
    path.write_text(json.dumps(tampered))

    with pytest.raises(ValueError, match="modified"):
        write_answer(channel.directory, SID, request["approval_id"], allow=True)
    assert bool(await task) is False  # times out: nothing valid ever answered


async def test_a_symlinked_answer_denies(tmp_path):
    _b, channel, handle = _broker(tmp_path)
    task = asyncio.ensure_future(handle("bash(x)"))
    request = await _parked(channel)
    elsewhere = tmp_path / "planted.json"
    elsewhere.write_text("{}")
    os.symlink(elsewhere, channel.directory / f"{request['approval_id']}.answer.json")

    assert bool(await task) is False
    assert "not a regular file" in _decision(channel, request["approval_id"])["reason"]


async def test_a_partial_answer_denies_and_a_failed_writer_leaves_nothing(tmp_path, monkeypatch):
    _b, channel, handle = _broker(tmp_path)
    task = asyncio.ensure_future(handle("bash(x)"))
    request = await _parked(channel)

    real_write = os.write

    def torn(fd, data):
        real_write(fd, data[:7])
        raise OSError("disk full")

    monkeypatch.setattr(ac.os, "write", torn)
    with pytest.raises(OSError):
        write_answer(channel.directory, SID, request["approval_id"], allow=True)
    monkeypatch.setattr(ac.os, "write", real_write)
    assert not (channel.directory / f"{request['approval_id']}.answer.json").exists()
    assert not list(channel.directory.glob(".*.tmp"))

    path = channel.directory / f"{request['approval_id']}.answer.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b'{"version": 1, "session_')
    os.close(fd)
    assert bool(await task) is False
    assert "not complete JSON" in _decision(channel, request["approval_id"])["reason"]


async def test_a_crash_before_the_directory_sync_still_yields_one_answer(tmp_path, monkeypatch):
    _b, channel, handle = _broker(tmp_path)
    task = asyncio.ensure_future(handle("bash(x)"))
    request = await _parked(channel)
    real_sync = ac._fsync_dir

    def crash(path):
        raise OSError("power lost")

    monkeypatch.setattr(ac, "_fsync_dir", crash)
    with pytest.raises(OSError):
        write_answer(channel.directory, SID, request["approval_id"], allow=True)
    monkeypatch.setattr(ac, "_fsync_dir", real_sync)

    assert bool(await task) is True  # the linked answer is complete or absent, never torn
    with pytest.raises(FileExistsError):
        write_answer(channel.directory, SID, request["approval_id"], allow=False)


def test_an_expired_request_cannot_be_answered(tmp_path):
    channel = FileApprovalChannel(tmp_path / "approvals", SID)
    request = type("R", (), {"approval_id": "apr-1-1", "action": "a", "family": "approval",
                             "runtime_id": "native"})()
    published = channel.publish(request, ceiling="c", expires_at=0.0)
    with pytest.raises(ValueError, match="expired"):
        write_answer(channel.directory, SID, "apr-1-1", allow=True)
    _forge(channel, "apr-1-1")
    allow, reason = channel.poll(published, ceiling_now="c")
    assert allow is False and "expired" in reason


async def test_a_ceiling_change_voids_the_answer(tmp_path):
    engine = PermissionEngine(mode="smart")
    _b, channel, handle = _broker(tmp_path, engine=engine)
    task = asyncio.ensure_future(handle("bash(x)"))
    request = await _parked(channel)
    engine._mode = "readonly"
    write_answer(channel.directory, SID, request["approval_id"], allow=True)

    assert bool(await task) is False
    assert "ceiling changed" in _decision(channel, request["approval_id"])["reason"]


async def test_a_terminal_and_a_file_answer_meet_at_one_decision(tmp_path):
    async def slow_yes(_request):
        await asyncio.sleep(0.3)
        return True

    _b, channel, handle = _broker(tmp_path, answerer=slow_yes)
    task = asyncio.ensure_future(handle("bash(x)"))
    request = await _parked(channel)
    write_answer(channel.directory, SID, request["approval_id"], allow=False)

    assert bool(await task) is False  # the file answered first
    await asyncio.sleep(0.4)  # the terminal's late yes changes nothing
    assert _decision(channel, request["approval_id"])["outcome"] == "deny"


async def test_a_crash_between_reservation_and_acknowledgement_is_never_resent(tmp_path):
    _b, channel, handle = _broker(tmp_path, runtime="codex")
    task = asyncio.ensure_future(handle("terminal: make deploy"))
    request = await _parked(channel)
    write_answer(channel.directory, SID, request["approval_id"], allow=True)
    decision = await task
    assert bool(decision) and decision.approval_id == request["approval_id"]
    # ... the process dies here, before the runtime acknowledged the answer.
    assert channel.unacknowledged() == [request["approval_id"]]

    restarted = ApprovalBroker(engine=PermissionEngine(), timeout_sec=1.0, channel=channel)
    allowed, reason = await restarted._park_and_wait(
        "approval", "terminal: make deploy", "codex", SID, request["approval_id"],
        deliver="deferred",
    )
    assert allowed is False and "never sent twice" in reason

    decision.acknowledge()  # had the runtime confirmed, delivery would read acknowledged
    assert channel.unacknowledged() == []


async def test_the_cli_lists_and_answers_a_parked_approval(tmp_path, monkeypatch, capsys):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.main import build_parser, run_approvals
    from garuda.interfaces.run_guard import session_approval_channel

    store = SessionStore()
    store.begin(SID, task="t", model="m", agent="a", workspace=str(tmp_path))
    channel = session_approval_channel(store, SID)
    broker = ApprovalBroker(engine=PermissionEngine(), timeout_sec=3.0, channel=channel)
    task = asyncio.ensure_future(broker.handler(session_id=SID)("bash(make release)"))
    request = await _parked(channel)
    parse = build_parser().parse_args

    assert run_approvals(parse(["approvals", "list", SID, "--workspace", str(tmp_path)])) == 0
    assert "bash(make release)" in capsys.readouterr().out
    answer = parse(["approvals", "answer", SID, request["approval_id"], "--deny",
                    "--workspace", str(tmp_path)])
    assert run_approvals(answer) == 0
    assert bool(await task) is False
    assert run_approvals(answer) == 2  # one answer only
