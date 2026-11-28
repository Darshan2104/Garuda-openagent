"""Owned crash continuation without real children: host proof, malformed records,
the read model's resume check off Linux and its memoized dry proof."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from garuda.runtime import ownership
from tests.test_web_flow_resume import RESUME, _resume_body, _stopped_owned_flow
from tests.test_web_flow_starts import _body, _call
from tests.test_web_flow_starts import write_context as write_context  # noqa: F401 - fixture
from tests.test_web_resume import _flow

HERE = {"boot_id": "b" * 32, "pid_ns": "pid:[4026531836]", "machine_id": "m" * 32,
        "hostname": "here"}


def _identity(boot):
    return {"pid": 4242, "identity": f"linux:{boot}:123", "pgid": 4242}


def test_only_this_hosts_pid_namespace_proves_a_process_gone(monkeypatch):
    monkeypatch.setattr(ownership, "host_identity", lambda: dict(HERE))
    same = ownership.same_host_refusal
    assert same(HERE, [_identity(HERE["boot_id"])]) is None
    # This host after a reboot: same machine id, hostname and (initial) pid namespace.
    rebooted = {**HERE, "boot_id": "c" * 32}
    assert same(rebooted, [_identity("c" * 32)]) is None
    assert "pid namespace" in same({**HERE, "pid_ns": "pid:[4026532999]"},
                                   [_identity(HERE["boot_id"])])
    assert "another boot" in same({**rebooted, "machine_id": "n" * 32}, [_identity("c" * 32)])
    assert "another boot" in same({**rebooted, "hostname": "there"}, [_identity("c" * 32)])
    assert "no record" in same(None, [])
    assert "no record" in same({"boot_id": "", "pid_ns": "x"}, [])
    # Every identity must be from the recorded boot.
    assert "start identity" in same(HERE, [_identity("c" * 32)])
    assert "start identity" in same(HERE, [{"pid": 7, "identity": "darwin:Mon Jan 1", "pgid": 7}])
    monkeypatch.setattr(ownership, "host_identity", lambda: None)
    assert "cannot be read" in same(HERE, [])


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("line", [b"\xff\xfe not utf-8\n", b"[1]\n", b'"text"\n'])
async def test_malformed_step_records_refuse_as_a_record_mismatch(
        write_context, monkeypatch, line, running):
    ctx = write_context
    _start_id, session = await _stopped_owned_flow(ctx, monkeypatch)
    if running:  # as a hard-killed dashboard leaves it: the crash proof reads the journal
        ctx.store.update_meta(session, {"flow_state": "running", "status": "running"})
    with open(engine.flow_dir(ctx.store, session) / engine.JOURNAL, "ab") as handle:
        handle.write(line)
    body = _resume_body(session)
    response = await _call(ctx, RESUME, method="POST", body=body)
    assert response.status == 409, _body(response)
    # Never a malformed request (400) or a server error. A stopped flow gets the stop
    # check's own typed refusal; the crash proof's reading gives a record mismatch.
    allowed = ({"flow.resume_record_mismatch"} if running else
               {"flow.resume_record_mismatch", "flow.verification_reconciliation_required"})
    assert _body(response)["error"]["code"] in allowed, _body(response)
    assert ctx.flow_starts.intents.read(body["operation_id"]) is None


async def test_malformed_flow_metadata_is_never_a_server_error(write_context, monkeypatch):
    ctx = write_context
    _start_id, session = await _stopped_owned_flow(ctx, monkeypatch)
    ctx.store.update_meta(session, {"flow_state": "running", "status": "running",
                                    "flow": "not-a-mapping"})
    response = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
    assert response.status == 409, _body(response)
    assert _body(response)["error"]["code"] in {"flow.recovery_required",
                                                "flow.resume_record_mismatch"}


def test_off_linux_the_resume_check_says_what_the_gateway_says(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    stopped = _flow(store)
    running = _flow(store, state="running", stop=None)
    monkeypatch.setattr(read_model, "owned_supervisor_available", lambda: False)
    check = read_model.resume_check(store, stopped, store.load_meta(stopped))
    assert check["available"] is False and check["code"] == "starter.ownership_unavailable"
    unsettled = _flow(store, state="interrupted", stop="flow.owner_lost")
    check = read_model.resume_check(store, unsettled, store.load_meta(unsettled))
    assert check["code"] == "starter.ownership_unavailable"
    assert read_model.resume_check(store, running, store.load_meta(running)) is None


def test_the_read_models_dry_proof_is_memoized_per_flow_state(tmp_path, monkeypatch):
    from garuda.flows import crash

    store = SessionStore(tmp_path / "sessions")
    running = _flow(store, state="running", stop=None)
    calls = []

    def proved(_store, session, _meta, **kwargs):
        calls.append((session, kwargs))
        return {"dry": True, "step": "look", "attempt": 1}

    monkeypatch.setattr(read_model, "owned_supervisor_available", lambda: True)
    monkeypatch.setattr(crash, "prove", proved)
    read_model._DRY_PROOFS.clear()
    meta = store.load_meta(running)
    for _ in range(5):
        assert read_model.resume_check(store, running, meta)["available"] is True
    assert calls == [(running, {"owned": True, "dry": True})]
    # A new journal entry (another attempt, a settlement) recomputes at once.
    journal = engine.flow_dir(store, running) / engine.JOURNAL
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"event": "intent", "step": "look", "attempt": 1}) + "\n")
    read_model.resume_check(store, running, meta)
    assert len(calls) == 2
    # And so does the end of its time to live.
    monkeypatch.setattr(read_model, "DRY_PROOF_TTL_SEC", -1.0)
    read_model.resume_check(store, running, meta)
    assert len(calls) == 3
    assert uuid.UUID(running)


async def _held_resumes(monkeypatch):
    """Resume dispatches that start and then hold (the flow service is not reached twice)."""
    from garuda.flows.service import FlowExecutionService

    calls = []

    async def held(_service, *_args, **_kwargs):
        calls.append(1)
        await asyncio.Event().wait()

    monkeypatch.setattr(FlowExecutionService, "resume_prepared", held)
    return calls


async def test_a_duplicate_during_admission_gets_the_same_operation(write_context, monkeypatch):
    """Forced: the duplicate arrives while the first request is admitting, before its
    record exists. It waits for that admission and gets the same operation."""
    import threading

    from garuda.scenarios import compile as compile_module

    ctx = write_context
    _start_id, session = await _stopped_owned_flow(ctx, monkeypatch)
    calls = await _held_resumes(monkeypatch)
    real = compile_module.recompile_recorded
    entered, gate = threading.Event(), threading.Event()

    def gated(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert gate.wait(30), "the gate was never opened"
        return real(*args, **kwargs)

    monkeypatch.setattr(compile_module, "recompile_recorded", gated)
    body = _resume_body(session)
    first = asyncio.create_task(_call(ctx, RESUME, method="POST", body=body))
    assert await asyncio.to_thread(entered.wait, 30)
    assert ctx.flow_starts.intents.read(body["operation_id"]) is None  # not recorded yet
    second = asyncio.create_task(_call(ctx, RESUME, method="POST", body=body))
    for _ in range(50):  # the duplicate is waiting on the first admission
        await asyncio.sleep(0.01)
        if body["operation_id"] in ctx.flow_starts._resume_requests:
            break
    assert not second.done()
    gate.set()
    responses = await asyncio.gather(first, second)
    assert responses[0].status in {201, 202} and responses[1].status in {200, 202}, [_body(r) for r in responses]
    assert {_body(r)["operation_id"] for r in responses} == {body["operation_id"]}
    assert _body(responses[1])["duplicate"] is True
    assert len(calls) == 1  # dispatched once


async def test_a_duplicate_while_its_resume_runs_is_never_in_progress(write_context, monkeypatch):
    """Forced: the duplicate's first look misses the record (it was written just after),
    and the flow is already running here. It still gets the same operation; only a
    different operation is refused as in progress."""
    ctx = write_context
    _start_id, session = await _stopped_owned_flow(ctx, monkeypatch)
    calls = await _held_resumes(monkeypatch)
    body = _resume_body(session)
    first = await _call(ctx, RESUME, method="POST", body=body)
    assert first.status in {201, 202}, _body(first)
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.02)
    assert calls == [1]
    gateway = ctx.flow_starts
    real_read = gateway.intents.read
    missed = []

    def stale_once(operation_id):
        if operation_id == body["operation_id"] and not missed:
            missed.append(operation_id)
            return None  # as if read just before the first request recorded it
        return real_read(operation_id)

    monkeypatch.setattr(gateway.intents, "read", stale_once)
    duplicate = await _call(ctx, RESUME, method="POST", body=body)
    assert missed, "the stale read was not exercised"
    assert duplicate.status in {200, 202}, _body(duplicate)  # existing, or still admitting
    assert _body(duplicate)["operation_id"] == body["operation_id"]
    assert _body(duplicate)["duplicate"] is True
    other = await _call(ctx, RESUME, method="POST", body=_resume_body(session))
    assert other.status == 409, _body(other)
    assert _body(other)["error"]["code"] == "flow.resume_in_progress"
    assert calls == [1]
