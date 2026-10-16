"""Candidate-bound flow commitment consumes the actual acceptance producer."""

import json
import subprocess
import sys
import uuid

import pytest

from garuda.core.acceptance_request import CheckContext, FrozenCheck, FrozenCheckRequest
from garuda.core.acceptance_runner import AcceptanceRunner
from garuda.core.acceptance_work import AcceptanceWorkError
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.flows.verification_records import OwnedFlowVerification
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.strict_store import CorruptRecord
from garuda.workspace.flow_evidence import OwnedFlowEvidence
from garuda.workspace.lease import LeaseStore

linux = pytest.mark.skipif(sys.platform != "linux", reason="Linux owned check producer")


@pytest.fixture
async def case(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    parent_id = str(uuid.uuid4())
    store = SessionStore(tmp_path / "sessions")
    store.begin(parent_id, "task", "flow", "flow:pair", str(workspace))
    (store.session_dir(parent_id) / "flow").mkdir(mode=0o700)
    owner = WorkspaceLeaseGuard(str(workspace), parent_id,
                                lease_store=LeaseStore(tmp_path / "leases"), ttl_sec=1.2)
    owner.acquire()
    owner.start_heartbeat()
    try:
        yield tmp_path, workspace, store, owner
    finally:
        await owner.release()


@linux
@pytest.mark.asyncio
async def test_actual_check_receipt_is_required_for_a_candidate_bound_commit(case):
    root, workspace, store, owner = case
    evidence = OwnedFlowEvidence(store.session_dir(owner.session_id), owner.session_id,
                                 str(workspace), owner)
    await evidence.begin()
    reference, manifest = await evidence.finalize([])
    assert manifest["complete"] is True
    candidate = await evidence.version()
    info = workspace.stat()
    context = CheckContext(owner.session_id, str(uuid.uuid4()), str(workspace), candidate,
                           reference["manifest_digest"], (), True, (info.st_dev, info.st_ino))
    marker = root / "launched"
    permissions = PermissionEngine(mode="auto")
    check = FrozenCheck((sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"),
                        "user-request")
    request = FrozenCheckRequest.freeze([check], (), permissions)
    records = OwnedFlowVerification(store, owner, request, context, permissions)
    phase = await records.prepare()
    from garuda.flows.verification_records import FlowVerificationRecords

    direct = FlowVerificationRecords(store.session_dir(owner.session_id), owner.session_id)
    assert direct.reconcile_unfinished() == {
        "classification": "not-started",
        "checks": [{"index": 0, "authority": "user-request", "classification": "absent"}],
    }
    assert not marker.exists()
    result = await owner.race(AcceptanceRunner(store).run(request, context, owner, permissions))
    assert result.verification["status"] == "passed"
    recovery = direct.reconcile_unfinished()
    assert recovery["classification"] == "terminal-unpublished"
    assert recovery["checks"][0]["status"] == "passed"
    forged = [{"authority": "user-request", "status": "passed", "fingerprint": "f" * 16}]
    with pytest.raises(CorruptRecord, match="receipt disagrees"):
        direct.commit(phase["phase_digest"], forged, result.receipt_refs,
                      result.verification)
    commitment = await records.commit(phase["phase_digest"], result)
    assert direct.reconcile_unfinished() == {
        "classification": "commit-unpublished", "checks": []}
    document = json.loads((store.session_dir(owner.session_id) / "flow" /
                           "verification-commit.json").read_text())
    assert document["candidate"] == candidate
    assert document["evidence_digest"] == reference["manifest_digest"]
    assert document["checks"][0]["receipt_digest"] == result.receipt_refs[0]["digest"]
    assert len(commitment["commit_digest"]) == 64
    published_phase = {"phase_id": context.phase_id, "digest": phase["phase_digest"]}
    assert direct.committed(commitment, result.receipts, published_phase) == document
    with pytest.raises(CorruptRecord, match="published terminal check receipt changed"):
        direct.committed(commitment, [{**result.receipts[0], "status": "failed"}],
                         published_phase)
    with pytest.raises(CorruptRecord, match="published flow check phase differs"):
        direct.committed(commitment, result.receipts,
                         {**published_phase, "digest": "0" * 64})
    assert marker.exists()


@linux
@pytest.mark.asyncio
async def test_lost_actual_check_receipt_cannot_commit_a_pass(case):
    root, workspace, store, owner = case
    evidence = OwnedFlowEvidence(store.session_dir(owner.session_id), owner.session_id,
                                 str(workspace), owner)
    await evidence.begin()
    reference, _manifest = await evidence.finalize([])
    candidate = await evidence.version()
    info = workspace.stat()
    context = CheckContext(owner.session_id, str(uuid.uuid4()), str(workspace), candidate,
                           reference["manifest_digest"], (), True, (info.st_dev, info.st_ino))
    permissions = PermissionEngine(mode="auto")
    marker = root / "launched"
    check = FrozenCheck((sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"),
                        "user-request")
    request = FrozenCheckRequest.freeze([check], (), permissions)
    records = OwnedFlowVerification(store, owner, request, context, permissions)
    phase = await records.prepare()
    result = await owner.race(AcceptanceRunner(store).run(request, context, owner, permissions))
    receipt = next((store.session_dir(owner.session_id) / f"acceptance-{context.phase_id}").glob(
        "receipt-*.json"))
    receipt.unlink()
    with pytest.raises(AcceptanceWorkError, match="CorruptRecord"):
        await records.commit(phase["phase_digest"], result)
    assert not (store.session_dir(owner.session_id) / "flow" / "verification-commit.json").exists()


@linux
@pytest.mark.asyncio
async def test_tree_changing_check_commits_only_an_invalidated_verdict(case):
    _root, workspace, store, owner = case
    evidence = OwnedFlowEvidence(store.session_dir(owner.session_id), owner.session_id,
                                 str(workspace), owner)
    await evidence.begin()
    reference, _manifest = await evidence.finalize([])
    candidate = await evidence.version()
    info = workspace.stat()
    context = CheckContext(owner.session_id, str(uuid.uuid4()), str(workspace), candidate,
                           reference["manifest_digest"], (), True, (info.st_dev, info.st_ino))
    permissions = PermissionEngine(mode="auto")
    check = FrozenCheck((sys.executable, "-c",
                         f"open({str(workspace / 'check-mutated')!r}, 'w').close()"),
                        "user-request")
    request = FrozenCheckRequest.freeze([check], (), permissions)
    records = OwnedFlowVerification(store, owner, request, context, permissions)
    phase = await records.prepare()
    result = await owner.race(AcceptanceRunner(store).run(request, context, owner, permissions))
    assert result.verification == {"status": "invalidated",
                                   "code": "verification.check_changed_tree"}
    commitment = await records.commit(phase["phase_digest"], result)
    document = json.loads((store.session_dir(owner.session_id) / "flow" /
                           "verification-commit.json").read_text())
    assert document["verification"] == result.verification
    assert len(commitment["commit_digest"]) == 64


@linux
@pytest.mark.asyncio
async def test_real_infrastructure_change_commits_invalidated_verdict(case):
    _root, workspace, store, owner = case
    evidence = OwnedFlowEvidence(store.session_dir(owner.session_id), owner.session_id,
                                 str(workspace), owner)
    await evidence.begin()
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    reference, manifest = await evidence.finalize([])
    assert "pyproject.toml" in manifest["changed"]
    candidate = await evidence.version()
    info = workspace.stat()
    context = CheckContext(owner.session_id, str(uuid.uuid4()), str(workspace), candidate,
                           reference["manifest_digest"], tuple(manifest["changed"]), True,
                           (info.st_dev, info.st_ino))
    permissions = PermissionEngine(mode="auto")
    request = FrozenCheckRequest.freeze(
        [FrozenCheck((sys.executable, "-m", "pytest", "--version"), "user-request")],
        (), permissions)
    records = OwnedFlowVerification(store, owner, request, context, permissions)
    phase = await records.prepare()
    result = await owner.race(AcceptanceRunner(store).run(request, context, owner, permissions))
    assert result.receipts[0]["status"] == "unverified"
    assert result.verification == {"status": "invalidated",
                                   "code": "verification.test_infra_changed"}
    commitment = await records.commit(phase["phase_digest"], result)
    assert len(commitment["commit_digest"]) == 64


@pytest.mark.asyncio
async def test_phase_commit_refuses_an_invented_pass_without_check_records(case):
    _root, workspace, store, owner = case
    info = workspace.stat()
    context = CheckContext(owner.session_id, str(uuid.uuid4()), str(workspace), "a" * 16,
                           "b" * 64, (), True, (info.st_dev, info.st_ino))
    permissions = PermissionEngine(mode="auto")
    check = FrozenCheck("true", "user-request")
    request = FrozenCheckRequest.freeze([check], (), permissions)
    records = OwnedFlowVerification(store, owner, request, context, permissions)
    from garuda.flows.verification_records import FlowVerificationRecords

    direct = FlowVerificationRecords(store.session_dir(owner.session_id), owner.session_id)
    phase = direct.prepare(records.phase)
    with pytest.raises(CorruptRecord, match="receipt reference"):
        direct.commit(phase["phase_digest"], [{"authority": "user-request", "status": "passed"}],
                      [], {"status": "passed", "authority": "user-request"})
