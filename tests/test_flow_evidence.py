"""Complete parent evidence across settled flow attempt boundaries (#313)."""

import subprocess
import sys
import uuid

import pytest

from garuda.core.acceptance_work import owned_call
from garuda.core.sessions import SessionStore
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.strict_store import CorruptRecord
from garuda.workspace.flow_evidence import FlowEvidenceStore
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
        evidence.boundary(0, "coder", 1)
        (workspace / "conftest.py").write_text("original\n")
        evidence.boundary(1, "coder", 2)
        reference = evidence.finalize([{"step": "coder", "attempt": 1},
                                       {"step": "coder", "attempt": 2}])
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
        evidence.boundary(0, "coder", 1)
        with pytest.raises(CorruptRecord, match="coverage"):
            evidence.finalize([{"step": "coder", "attempt": 1},
                               {"step": "reviewer", "attempt": 1}])
        evidence.boundary(1, "coder", 1)
        with pytest.raises(ValueError, match="duplicate"):
            evidence.finalize([{"step": "coder", "attempt": 1},
                               {"step": "coder", "attempt": 1}])
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
        evidence.boundary(0, "coder", 1)
        reference = evidence.finalize([{"step": "coder", "attempt": 1}])
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
        evidence.boundary(0, "coder", 1)
        reference = evidence.finalize([{"step": "coder", "attempt": 1}])
        changed = evidence.manifest(reference["manifest_digest"])["changed"]
        assert changed == ["conftest.py", "renamed.py"]
    finally:
        await owner.release()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux owned evidence worker")
@pytest.mark.asyncio
async def test_live_parent_worker_captures_the_original_attempt_boundaries(case):
    workspace, owner, evidence = case
    owner.start_heartbeat()
    base = {"snapshot": owner.snapshot_ownership(), "session_dir": str(evidence.session),
            "parent_id": owner.session_id, "workspace": str(workspace)}
    try:
        await owned_call("flow-evidence-begin", base)
        (workspace / "conftest.py").write_text("changed\n")
        await owned_call("flow-evidence-boundary", {**base, "ordinal": 0,
                                                    "step": "coder", "attempt": 1})
        (workspace / "conftest.py").write_text("original\n")
        await owned_call("flow-evidence-boundary", {**base, "ordinal": 1,
                                                    "step": "coder", "attempt": 2})
        reference = await owned_call("flow-evidence-finalize", {
            **base, "coverage": [{"step": "coder", "attempt": 1},
                                  {"step": "coder", "attempt": 2}]})
        assert evidence.manifest(reference["manifest_digest"])["changed"] == ["conftest.py"]
    finally:
        await owner.release()
