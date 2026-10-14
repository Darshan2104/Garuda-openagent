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
    return {"version": 2, "parent_id": context.parent_id, "phase_id": context.phase_id,
            "workspace": context.workspace,
            "workspace_identity": list(context.workspace_identity),
            "owner": {key: lease[key] for key in ("pid", "identity", "pgid", "epoch")},
            "candidate": context.candidate, "evidence_digest": context.evidence_digest,
            "request_digest": request.identity, "authority_digest": request.authority_digest,
            "ceiling_digest": request.ceiling_digest,
            "sources": [source.document() for source in request.sources],
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
        phase = read_record(directory, PHASE, versions=(2,))
        if phase is None:
            raise CorruptRecord("flow verification phase intent is missing")
        self.validate_phase(phase)
        return phase

    def validate_phase(self, phase):
        keys = {"version", "parent_id", "phase_id", "workspace", "workspace_identity",
                "owner", "candidate", "evidence_digest", "request_digest",
                "authority_digest", "ceiling_digest", "sources", "checks"}
        if (not isinstance(phase, dict) or set(phase) != keys
                or type(phase["version"]) is not int or phase["version"] != 2
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
        sources = phase["sources"]
        if (not isinstance(sources, list) or len(sources) > 32
                or any(not isinstance(source, dict) or set(source) != {"path", "digest"}
                       or not isinstance(source["path"], str)
                       or not os.path.isabs(source["path"])
                       or "\0" in source["path"]
                       or (source["digest"] is not None
                           and not _hex(source["digest"], 64)) for source in sources)
                or [source["path"] for source in sources] != sorted({
                    source["path"] for source in sources})):
            raise CorruptRecord("flow verification source identities are invalid")
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

    def committed(self, reference, published_receipts, published_phase):
        """Read a published commit and recheck its terminal and parent receipt chain."""
        if (not isinstance(reference, dict) or set(reference) != {"commit_digest"}
                or not _hex(reference["commit_digest"], 64)):
            raise CorruptRecord("flow verification commit reference is invalid")
        directory = self._open()
        try:
            phase = self._phase(directory)
            if published_phase != {"phase_id": phase["phase_id"],
                                    "digest": digest(phase)}:
                raise CorruptRecord("published flow check phase differs from intent")
            document = read_record(directory, COMMIT)
            if (not isinstance(document, dict)
                    or set(document) != {"version", "phase_digest", "candidate",
                                         "evidence_digest", "checks", "verification"}
                    or type(document["version"]) is not int or document["version"] != 1
                    or digest(document) != reference["commit_digest"]
                    or document["phase_digest"] != digest(phase)
                    or document["candidate"] != phase["candidate"]
                    or document["evidence_digest"] != phase["evidence_digest"]
                    or not isinstance(document["checks"], list)
                    or len(document["checks"]) > len(phase["checks"])
                    or not isinstance(document["verification"], dict)):
                raise CorruptRecord("flow verification commit is not bound to its phase")
            results = []
            for index, row in enumerate(document["checks"]):
                check = phase["checks"][index]
                if (not isinstance(row, dict)
                        or set(row) not in ({"index", "check_id", "authority", "status"},
                                            {"index", "check_id", "authority", "status",
                                             "receipt_digest"})
                        or type(row["index"]) is not int or row["index"] != index
                        or row["check_id"] != check["check_id"]
                        or row["authority"] != check["authority"]
                        or not isinstance(row["status"], str)
                        or row["status"] not in TERMINAL | UNLAUNCHED):
                    raise CorruptRecord("flow verification check summary changed")
                if row["status"] in TERMINAL:
                    if not _hex(row.get("receipt_digest"), 64):
                        raise CorruptRecord("terminal flow check lost its receipt reference")
                    actual = CheckRecords(self.path.parent,
                                          self._binding(phase, index, check)).reconcile(
                                              full_result=True)
                    if (actual["classification"] != "terminal"
                            or actual["receipt_digest"] != row["receipt_digest"]
                            or actual["result"].get("status") != row["status"]
                            or actual["result"].get("authority") != row["authority"]):
                        raise CorruptRecord("terminal flow check receipt changed")
                    results.append(actual["result"])
                elif "receipt_digest" in row:
                    raise CorruptRecord("unlaunched flow check has a receipt reference")
                else:
                    results.append({"authority": row["authority"], "status": row["status"]})
            verdict = document["verification"]
            complete = len(results) == len(phase["checks"])
            if not isinstance(verdict.get("status"), str):
                valid = False
            elif verdict["status"] in {"passed", "failed"}:
                valid = complete and verdict == verification_from(results)
            elif verdict["status"] == "unavailable":
                valid = (verdict == {"status": "unavailable",
                                     "code": "verification.unsupported_supervisor"} and not results
                         or complete and verdict == verification_from(results))
            elif verdict["status"] == "invalidated":
                valid = (set(verdict) == {"status", "code"}
                         and isinstance(verdict["code"], str)
                         and verdict["code"] in {"verification.authority_changed",
                                                 "verification.candidate_changed",
                                                 "verification.check_changed_tree",
                                                 "verification.test_infra_changed"}
                         and (not complete or verdict["code"] in {
                             "verification.authority_changed", "verification.candidate_changed"}
                              or verdict == verification_from(results)))
            else:
                valid = False
            if not valid:
                raise CorruptRecord("flow verification commit verdict is not supported by receipts")
            if (not isinstance(published_receipts, list)
                    or len(published_receipts) != len(results)):
                raise CorruptRecord("published flow check receipts differ from commitment")
            for published, actual, summary in zip(published_receipts, results,
                                                  document["checks"], strict=True):
                if not isinstance(published, dict):
                    raise CorruptRecord("published flow check receipt is invalid")
                if summary["status"] in TERMINAL:
                    if published != actual:
                        raise CorruptRecord("published terminal check receipt changed")
                elif {key: published.get(key) for key in ("authority", "status")} != actual:
                    raise CorruptRecord("published unlaunched check status changed")
            return document
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
                            or actual["result"] != {key: result[key] for key in (
                                "authority", "status", "code", "fingerprint") if key in result}):
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
                        "verification.check_changed_tree", "verification.test_infra_changed"
                        } or set(verdict) != {"status", "code"}:
                    raise CorruptRecord("invalid flow verification invalidation")
                if (len(results) == len(phase["checks"])
                        and verdict.get("code") in {"verification.check_changed_tree",
                                                    "verification.test_infra_changed"}
                        and verdict != verification_from(results)):
                    raise CorruptRecord("flow invalidation differs from receipts")
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
