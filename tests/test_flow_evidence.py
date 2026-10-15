"""Complete parent evidence across settled flow attempt boundaries (#313)."""

import json
import subprocess
import sys
import uuid

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.strict_store import CorruptRecord
from garuda.workspace.flow_evidence import FlowEvidenceStore, OwnedFlowEvidence
from garuda.workspace.lease import LeaseStore


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def case(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    (workspace / "conftest.py").write_text("original\n")
    _git(workspace, "add", "conftest.py")
    _git(workspace, "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
         "commit", "-qm", "base")
    session_id = str(uuid.uuid4())
    store = SessionStore(tmp_path / "sessions")
    store.begin(session_id, "task", "flow", "flow:build-review", str(workspace))
    (store.session_dir(session_id) / "flow").mkdir(mode=0o700)
    owner = WorkspaceLeaseGuard(str(workspace), session_id,
                                lease_store=LeaseStore(tmp_path / "leases"))
    owner.acquire()
    evidence = FlowEvidenceStore(store.session_dir(session_id), session_id,
                                 str(workspace), owner.snapshot_ownership())
    return workspace, owner, evidence


@pytest.mark.asyncio
async def test_restored_infrastructure_remains_in_complete_parent_manifest(case):
    workspace, owner, evidence = case
    try:
        evidence.begin()
        (workspace / "conftest.py").write_text("changed\n")
        first = evidence.boundary(0, "coder", 1)
        (workspace / "conftest.py").write_text("original\n")
        second = evidence.boundary(1, "coder", 2)
        reference = evidence.finalize([
            {"step": "coder", "attempt": 1, "boundary_digest": first["boundary_digest"]},
            {"step": "coder", "attempt": 2, "boundary_digest": second["boundary_digest"]}])
        manifest = evidence.manifest(reference["manifest_digest"])
        assert manifest["complete"] is True
        assert manifest["changed"] == ["conftest.py"]
        assert manifest["final_changed"] == []
        assert [row["attempt"] for row in manifest["segments"]] == [1, 2]
    finally:
        await owner.release()


@pytest.mark.asyncio
async def test_missing_attempt_boundary_never_finalizes_complete_evidence(case):
    workspace, owner, evidence = case
    try:
        evidence.begin()
        (workspace / "a.py").write_text("a\n")
        first = evidence.boundary(0, "coder", 1)
        with pytest.raises(CorruptRecord, match="coverage"):
            evidence.finalize([{"step": "coder", "attempt": 1,
                                "boundary_digest": first["boundary_digest"]},
                               {"step": "reviewer", "attempt": 1,
                                "boundary_digest": "0" * 64}])
        second = evidence.boundary(1, "coder", 1)
        with pytest.raises(ValueError, match="duplicate"):
            evidence.finalize([{"step": "coder", "attempt": 1,
                                "boundary_digest": first["boundary_digest"]},
                               {"step": "coder", "attempt": 1,
                                "boundary_digest": second["boundary_digest"]}])
        assert not (evidence.path / "manifest.json").exists()
    finally:
        await owner.release()


@pytest.mark.asyncio
async def test_parent_manifest_retains_infrastructure_beyond_child_display_clip(case):
    workspace, owner, evidence = case
    try:
        evidence.begin()
        for index in range(220):
            (workspace / f"a-{index:03}.py").write_text("x\n")
        (workspace / "conftest.py").write_text("changed\n")
        boundary = evidence.boundary(0, "coder", 1)
        reference = evidence.finalize([{"step": "coder", "attempt": 1,
                                        "boundary_digest": boundary["boundary_digest"]}])
        changed = evidence.manifest(reference["manifest_digest"])["changed"]
        assert len(changed) == 221
        assert "conftest.py" in changed
    finally:
        await owner.release()


@pytest.mark.asyncio
async def test_rename_accounts_for_both_infrastructure_source_and_destination(case):
    workspace, owner, evidence = case
    try:
        evidence.begin()
        _git(workspace, "mv", "conftest.py", "renamed.py")
        boundary = evidence.boundary(0, "coder", 1)
        reference = evidence.finalize([{"step": "coder", "attempt": 1,
                                        "boundary_digest": boundary["boundary_digest"]}])
        changed = evidence.manifest(reference["manifest_digest"])["changed"]
        assert changed == ["conftest.py", "renamed.py"]
    finally:
        await owner.release()


@pytest.mark.asyncio
async def test_manifest_read_refuses_replaced_attempt_segment(case):
    workspace, owner, evidence = case
    try:
        evidence.begin()
        (workspace / "conftest.py").write_text("changed\n")
        boundary = evidence.boundary(0, "coder", 1)
        reference = evidence.finalize([{"step": "coder", "attempt": 1,
                                        "boundary_digest": boundary["boundary_digest"]}])
        path = evidence.path / "boundary-0.json"
        segment = json.loads(path.read_text())
        segment["changed"] = []
        path.write_text(json.dumps(segment))
        with pytest.raises(CorruptRecord, match="segment differs"):
            evidence.manifest(reference["manifest_digest"])
    finally:
        await owner.release()


@pytest.mark.asyncio
async def test_manifest_read_refuses_missing_attempt_segment(case):
    workspace, owner, evidence = case
    try:
        evidence.begin()
        (workspace / "conftest.py").write_text("changed\n")
        boundary = evidence.boundary(0, "coder", 1)
        reference = evidence.finalize([{"step": "coder", "attempt": 1,
                                        "boundary_digest": boundary["boundary_digest"]}])
        (evidence.path / "boundary-0.json").unlink()
        with pytest.raises(CorruptRecord, match="coverage"):
            evidence.manifest(reference["manifest_digest"])
    finally:
        await owner.release()


@pytest.mark.asyncio
async def test_finalization_refuses_a_boundary_not_matching_its_receipt_commitment(case):
    workspace, owner, evidence = case
    try:
        evidence.begin()
        (workspace / "conftest.py").write_text("changed\n")
        evidence.boundary(0, "coder", 1)
        with pytest.raises(CorruptRecord, match="segment differs"):
            evidence.finalize([{"step": "coder", "attempt": 1,
                                "boundary_digest": "0" * 64}])
        assert not (evidence.path / "manifest.json").exists()
    finally:
        await owner.release()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux owned evidence worker")
@pytest.mark.asyncio
async def test_live_parent_worker_captures_the_original_attempt_boundaries(case):
    workspace, owner, evidence = case
    owner.start_heartbeat()
    adapter = OwnedFlowEvidence(evidence.session, owner.session_id, str(workspace), owner)
    try:
        await adapter.begin()
        (workspace / "conftest.py").write_text("changed\n")
        first = await adapter.boundary(0, "coder", 1)
        (workspace / "conftest.py").write_text("original\n")
        second = await adapter.boundary(1, "coder", 2)
        reference, manifest = await adapter.finalize([
            {"step": "coder", "attempt": 1, "boundary_digest": first["boundary_digest"]},
            {"step": "coder", "attempt": 2, "boundary_digest": second["boundary_digest"]}])
        assert reference["changed_count"] == 1
        assert manifest["changed"] == ["conftest.py"]
    finally:
        await owner.release()
