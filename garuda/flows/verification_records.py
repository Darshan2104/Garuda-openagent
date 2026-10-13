"""Immutable parent check-phase intent and candidate-bound verdict commitment.

The owner worker validates live lease, source and candidate facts before using
this store. These records are reconciliation evidence until the strict parent
metadata publisher references the final commit; they never grant launch alone.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

from garuda.core.acceptance import verification_from
from garuda.core.acceptance_records import CheckRecords, digest
from garuda.runtime.private_records import open_directory, read_record
from garuda.runtime.strict_store import CorruptRecord, StorageUnavailable, preserve_private_backup

PHASE = "verification-phase.json"
COMMIT = "verification-commit.json"
TERMINAL = {"passed", "failed", "void", "unverified", "cancelled"}
UNLAUNCHED = {"refused", "skipped"}


def _uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _hex(value, size):
    return (isinstance(value, str) and len(value) == size
            and all(char in "0123456789abcdef" for char in value))


def phase_document(request, context, owner):
    """Keep only identities needed to reconcile the original frozen request."""
    context.validate()
    request.document()
    snapshot = owner.snapshot_ownership()
    lease = snapshot["lease"]
    if (owner.session_id != context.parent_id or owner.workspace != context.workspace
            or lease["mode"] != "mutating"):
        raise ValueError("flow check phase has no matching mutating parent")
    return {"version": 1, "parent_id": context.parent_id, "phase_id": context.phase_id,
            "workspace": context.workspace,
            "workspace_identity": list(context.workspace_identity),
            "owner": {key: lease[key] for key in ("pid", "identity", "pgid", "epoch")},
            "candidate": context.candidate, "evidence_digest": context.evidence_digest,
            "request_digest": request.identity, "authority_digest": request.authority_digest,
            "ceiling_digest": request.ceiling_digest,
            "checks": [{"check_id": check.identity, "authority": check.authority,
                        "mode": check.mode} for check in request.checks]}


class FlowVerificationRecords:
    """Private immutable phase and commit records under the original flow."""

    def __init__(self, session_dir, parent_id):
        self.path = Path(session_dir) / "flow"
        if not _uuid(parent_id) or self.path.parent.name != parent_id:
            raise StorageUnavailable("verification records belong to another parent")
        self.parent_id = parent_id

    def _open(self):
        directory = open_directory(self.path)
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(directory)
            raise StorageUnavailable("verification record directory is not private")
        return directory

    def _phase(self, directory):
        phase = read_record(directory, PHASE)
        if phase is None:
            raise CorruptRecord("flow verification phase intent is missing")
        self.validate_phase(phase)
        return phase

    def validate_phase(self, phase):
        keys = {"version", "parent_id", "phase_id", "workspace", "workspace_identity",
                "owner", "candidate", "evidence_digest", "request_digest",
                "authority_digest", "ceiling_digest", "checks"}
        if (not isinstance(phase, dict) or set(phase) != keys
                or phase["parent_id"] != self.parent_id or not _uuid(phase["phase_id"])
                or not isinstance(phase["workspace"], str)
                or not os.path.isabs(phase["workspace"])
                or os.path.realpath(phase["workspace"]) != phase["workspace"]
                or not isinstance(phase["workspace_identity"], list)
                or len(phase["workspace_identity"]) != 2
                or any(type(value) is not int or value < 0
                       for value in phase["workspace_identity"])
                or not _hex(phase["candidate"], 16)
                or any(not _hex(phase[key], 64) for key in (
                    "evidence_digest", "request_digest", "authority_digest", "ceiling_digest"))):
            raise CorruptRecord("flow verification phase binding is invalid")
        owner = phase["owner"]
        if (not isinstance(owner, dict) or set(owner) != {"pid", "identity", "pgid", "epoch"}
                or any(type(owner[key]) is not int or owner[key] <= 1 for key in ("pid", "pgid"))
                or any(not isinstance(owner[key], str) or not owner[key]
                       or owner[key] == "unknown"
                       for key in ("identity", "epoch"))):
            raise CorruptRecord("flow verification owner is invalid")
        checks = phase["checks"]
        if (not isinstance(checks, list) or len(checks) > 32
                or any(not isinstance(check, dict) or set(check) != {"check_id", "authority", "mode"}
                       or not _hex(check["check_id"], 64)
                       or check["authority"] not in {"user-config", "trusted-project", "user-request"}
                       or check["mode"] not in {"host", "docker"} for check in checks)):
            raise CorruptRecord("flow verification check identities are invalid")

    def prepare(self, phase):
        self.validate_phase(phase)
        directory = self._open()
        try:
            if read_record(directory, COMMIT) is not None:
                raise CorruptRecord("flow verification already has a commit")
            raw = json.dumps(phase, sort_keys=True, allow_nan=False).encode()
            if len(raw) > 65_536:
                raise StorageUnavailable("flow verification intent exceeds its bound")
            preserve_private_backup(directory, PHASE, raw)
            return {"phase_digest": digest(phase)}
        finally:
            os.close(directory)


    @staticmethod
    def _binding(phase, index, check):
        return {"parent_id": phase["parent_id"], "phase_id": phase["phase_id"],
                "check_id": check["check_id"], "check_authority": check["authority"],
                "attempt_id": str(uuid.uuid5(uuid.UUID(phase["phase_id"]),
                                             f"{index}:{check['check_id']}")),
                "workspace": phase["workspace"],
                "workspace_identity": {"device": phase["workspace_identity"][0],
                                       "inode": phase["workspace_identity"][1]},
                "owner": phase["owner"], "candidate": phase["candidate"],
                "evidence_digest": phase["evidence_digest"],
                "authority_digest": phase["authority_digest"],
                "ceiling_digest": phase["ceiling_digest"],
                "request_digest": phase["request_digest"]}

    def commit(self, phase_digest, results, references, verdict):
        """Validate each launched check against its actual terminal record."""
        directory = self._open()
        try:
            phase = self._phase(directory)
            if digest(phase) != phase_digest:
                raise CorruptRecord("flow check request changed before commitment")
            if (not isinstance(results, list) or len(results) > len(phase["checks"])
                    or not isinstance(references, list) or not isinstance(verdict, dict)):
                raise CorruptRecord("invalid flow check result coverage")
            refs = {}
            for reference in references:
                if (not isinstance(reference, dict) or set(reference) != {"attempt_id", "digest"}
                        or not _uuid(reference["attempt_id"])
                        or not _hex(reference["digest"], 64)
                        or reference["attempt_id"] in refs):
                    raise CorruptRecord("invalid flow check receipt reference")
                refs[reference["attempt_id"]] = reference["digest"]
            summaries = []
            for index, result in enumerate(results):
                check = phase["checks"][index]
                if (not isinstance(result, dict) or result.get("authority") != check["authority"]
                        or result.get("status") not in TERMINAL | UNLAUNCHED):
                    raise CorruptRecord("flow check result differs from frozen order")
                binding = self._binding(phase, index, check)
                attempt_id = binding["attempt_id"]
                status = result["status"]
                if status in TERMINAL:
                    reference = refs.pop(attempt_id, None)
                    if reference is None:
                        raise CorruptRecord("terminal flow check has no receipt reference")
                    actual = CheckRecords(self.path.parent, binding).reconcile()
                    if (actual["classification"] != "terminal"
                            or actual["receipt_digest"] != reference
                            or actual["result"].get("status") != status
                            or actual["result"].get("authority") != check["authority"]):
                        raise CorruptRecord("flow check receipt disagrees with commitment")
                elif attempt_id in refs:
                    raise CorruptRecord("unlaunched flow check has a receipt reference")
                summaries.append({"index": index, "check_id": check["check_id"],
                                  "authority": check["authority"], "status": status,
                                  **({"receipt_digest": reference} if status in TERMINAL else {})})
            if refs:
                raise CorruptRecord("flow check has an unaccounted receipt reference")
            if verdict.get("status") in {"passed", "failed"}:
                if len(results) != len(phase["checks"]) or verdict != verification_from(results):
                    raise CorruptRecord("flow verification verdict is not derived from receipts")
            elif verdict.get("status") == "unavailable":
                unsupported = (verdict == {"status": "unavailable",
                                           "code": "verification.unsupported_supervisor"}
                               and not results and not references)
                if not unsupported and (len(results) != len(phase["checks"])
                                        or verdict != verification_from(results)):
                    raise CorruptRecord("flow unavailable verdict differs from receipts")
            elif verdict.get("status") == "invalidated":
                if verdict.get("code") not in {
                        "verification.authority_changed", "verification.candidate_changed",
                        "verification.check_changed_tree"} or set(verdict) != {"status", "code"}:
                    raise CorruptRecord("invalid flow verification invalidation")
            else:
                raise CorruptRecord("unsupported flow verification commitment")
            document = {"version": 1, "phase_digest": phase_digest, "candidate": phase["candidate"],
                        "evidence_digest": phase["evidence_digest"], "checks": summaries,
                        "verification": verdict}
            raw = json.dumps(document, sort_keys=True, allow_nan=False).encode()
            if len(raw) > 65_536:
                raise StorageUnavailable("flow verification commitment exceeds its bound")
            preserve_private_backup(directory, COMMIT, raw)
            return {"commit_digest": digest(document)}
        finally:
            os.close(directory)


class OwnedFlowVerification:
    """Async original-owner adapter for phase intent and final commitment."""

    def __init__(self, store, owner, request, context, permissions):
        self.owner, self.request, self.context = owner, request, context
        self.permissions = permissions
        self.phase = phase_document(request, context, owner)
        self.base = {"snapshot": owner.snapshot_ownership(),
                     "session_dir": str(store.session_dir(context.parent_id)),
                     "parent_id": context.parent_id, "phase": self.phase,
                     "sources": [source.document() for source in request.sources]}

    async def prepare(self):
        from garuda.core.acceptance_work import owned_call

        self.request.validate_ceiling(self.permissions)
        return await self.owner.race(owned_call("flow-verification-prepare", self.base,
                                                timeout=60))

    async def commit(self, phase_digest, result):
        from garuda.core.acceptance_work import owned_call

        try:
            self.request.validate_ceiling(self.permissions)
        except ValueError:
            if result.verification != {"status": "invalidated",
                                        "code": "verification.authority_changed"}:
                raise
        summaries = [{key: row[key] for key in ("authority", "status", "code", "fingerprint")
                      if key in row} for row in result.receipts]
        return await self.owner.race(owned_call("flow-verification-commit", {
            **self.base, "phase_digest": phase_digest, "results": summaries,
            "references": result.receipt_refs, "verdict": result.verification}, timeout=60))
