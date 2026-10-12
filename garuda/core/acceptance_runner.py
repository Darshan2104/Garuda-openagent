"""Internal parent-owned async acceptance; flow publication remains with #313."""

from __future__ import annotations

import asyncio
import shlex
import sys
import time
import uuid
from dataclasses import dataclass, field

from garuda.core.acceptance import _tail, evaluate_receipt, infra_changed, verification_from
from garuda.core.acceptance_request import CheckContext, CheckOwner, FrozenCheckRequest
from garuda.core.acceptance_work import AcceptanceWorkError, owned_call
from garuda.runtime.process_supervision import CleanupUncertain, LinuxProcessSupervisor, ProcessRequest
from garuda.workspace.lease import LeaseError


class CheckExecutionUncertain(Exception):
    """The owning flow must retain quarantine and must not replay these checks."""


class _Invalidated(Exception):
    def __init__(self, code):
        self.code = code


@dataclass
class CheckPhaseResult:
    receipts: list[dict] = field(default_factory=list)
    receipt_refs: list[dict] = field(default_factory=list)
    verification: dict = field(default_factory=dict)


@dataclass
class _Progress:
    result: CheckPhaseResult = field(default_factory=CheckPhaseResult)
    cancellation_published: bool = False


async def _settled(work):
    """Only bounded owned cleanup/publication may outlive caller cancellation."""
    task = asyncio.create_task(work)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class AcceptanceRunner:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _snapshot(context, owner):
        if owner.session_id != context.parent_id or owner.workspace != context.workspace:
            raise LeaseError("acceptance belongs to a different parent or workspace")
        return owner.snapshot_ownership()

    async def _probe(self, request, context, owner, permissions, cwd="."):
        try:
            request.validate_ceiling(permissions)
        except ValueError as exc:
            raise _Invalidated("verification.authority_changed") from exc
        try:
            return await owned_call("context", {
                "snapshot": self._snapshot(context, owner), "workspace": context.workspace,
                "workspace_identity": context.workspace_identity, "cwd": cwd,
                "sources": [source.document() for source in request.sources]})
        except AcceptanceWorkError as exc:
            if str(exc) == "AuthorityChanged":
                raise _Invalidated("verification.authority_changed") from exc
            raise

    def _binding(self, request, context, owner, check, index):
        snapshot = self._snapshot(context, owner)
        lease = snapshot["lease"]
        return {"parent_id": context.parent_id, "phase_id": context.phase_id,
                "check_id": check.identity, "check_authority": check.authority,
                "attempt_id": str(uuid.uuid5(uuid.UUID(context.phase_id), f"{index}:{check.identity}")),
                "workspace": context.workspace,
                "workspace_identity": {"device": context.workspace_identity[0],
                                       "inode": context.workspace_identity[1]},
                "owner": {key: lease[key] for key in ("pid", "identity", "pgid", "epoch")},
                "candidate": context.candidate, "evidence_digest": context.evidence_digest,
                "authority_digest": request.authority_digest, "ceiling_digest": request.ceiling_digest,
                "request_digest": request.identity}

    async def _record(self, data, process, receipt, progress):
        facts = process.result
        if facts is None:
            raise CleanupUncertain("check has no terminal subtree evidence")
        document = {"version": 1, "binding": data["binding"], "supervisor": facts.supervisor,
                    "command": facts.command, "cleanup": facts.cleanup, "outcome": facts.outcome,
                    "execution": {"exit_code": facts.exit_code, "duration_s": facts.duration_s},
                    "result": receipt}
        reference = await owned_call("terminal", {**data, "receipt": document})
        progress.result.receipts.append(receipt)
        progress.result.receipt_refs.append({"attempt_id": data["binding"]["attempt_id"],
                                             "digest": reference["receipt_digest"]})

    @staticmethod
    def _base(check):
        text = check.run if isinstance(check.run, str) else shlex.join(check.run)
        return {"run": _tail(text)[:512], "authority": check.authority, "cwd": check.cwd,
                "mode": check.mode, "started_at": time.time()}

    async def _one(self, request, context, owner, permissions, check, index, progress):
        before = await self._probe(request, context, owner, permissions, check.cwd)
        if before["fingerprint"] != context.candidate:
            raise _Invalidated("verification.candidate_changed")
        command = check.run if isinstance(check.run, str) else shlex.join(check.run)
        allowed, _ = await permissions.evaluate_acceptance({"command": command, "cwd": before["cwd"]})
        if not allowed:
            progress.result.receipts.append({**self._base(check), "status": "refused",
                                             "reason": "effective check permission ceiling refused"})
            return
        data = {"session_dir": str(self.store.session_dir(context.parent_id)),
                "binding": self._binding(request, context, owner, check, index)}
        base = self._base(check)
        prepared = await owned_call("prepare", {**data, "summary": base["run"],
                                                "limits": request.limits.document()})
        if not prepared["created"]:
            existing = await owned_call("reconcile", data)
            if existing["classification"] != "terminal":
                raise CheckExecutionUncertain("existing check has no settled terminal evidence")
            progress.result.receipts.append(existing["result"])
            progress.result.receipt_refs.append({"attempt_id": data["binding"]["attempt_id"],
                                                 "digest": existing["receipt_digest"]})
            return
        async with await LinuxProcessSupervisor().prepare() as process:
            await owned_call("transition", {**data, "expected": "prepared", "phase": "launch-authorized",
                                             "fields": {"supervisor": process.supervisor}})
            try:
                current = await self._probe(request, context, owner, permissions, check.cwd)
                if current["fingerprint"] != context.candidate:
                    raise _Invalidated("verification.candidate_changed")
                try:
                    request.validate_ceiling(permissions)
                except ValueError as exc:
                    raise _Invalidated("verification.authority_changed") from exc
                self._snapshot(context, owner)
                command = await process.activate(ProcessRequest(
                    check.run, current["cwd"], check.env, request.limits.timeout,
                    request.limits.term_grace, request.limits.kill_grace))
                if command is not None:
                    await owned_call("transition", {**data, "expected": "launch-authorized", "phase": "active",
                                                     "fields": {"command": command}})
                facts = await process.wait()
                after = await self._probe(request, context, owner, permissions, check.cwd)
                touched, count = [], 0
                for start in range(0, len(context.changed_paths), 1024):
                    chunk = infra_changed(check.run, list(context.changed_paths[start:start + 1024]))
                    count += len(chunk)
                    touched.extend(chunk[:max(0, 200 - len(touched))])
                    await asyncio.sleep(0)
                output = _tail(facts.stdout_tail + facts.stderr_tail)
                code = facts.exit_code
                if facts.outcome in {"timeout", "launch-failed"}:
                    code, output = None, "timed out" if facts.outcome == "timeout" else "could not launch check"
                receipt = evaluate_receipt(base, before=context.candidate, after=after["fingerprint"],
                                           exit_code=code, output=output, changed=touched,
                                           duration_s=facts.duration_s, run=check.run)
                if facts.outcome == "cancelled":
                    receipt.update(status="cancelled", reason="check cancelled after proved cleanup")
                if count:
                    receipt.update(infra_changed_total=count, infra_changed_complete=count <= 200)
                await self._record(data, process, receipt, progress)
                if after["fingerprint"] != context.candidate:
                    raise _Invalidated("verification.check_changed_tree")
            except _Invalidated as exc:
                await process.close()
                if not progress.result.receipt_refs or progress.result.receipt_refs[-1]["attempt_id"] != data["binding"]["attempt_id"]:
                    receipt = {**base, "status": "void", "code": exc.code,
                               "reason": "candidate or authority changed", "fingerprint": context.candidate}
                    await self._record(data, process, receipt, progress)
                raise
            except asyncio.CancelledError:
                await process.close()
                receipt = {**base, "status": "cancelled", "fingerprint": context.candidate,
                           "reason": "check cancelled after proved cleanup",
                           "output_tail": _tail(process.result.stdout_tail + process.result.stderr_tail)}
                await _settled(self._record(data, process, receipt, progress))
                progress.cancellation_published = True
                raise

    async def run(self, request: FrozenCheckRequest, context: CheckContext,
                  owner: CheckOwner, permissions) -> CheckPhaseResult:
        request.document()
        context.validate()
        self._snapshot(context, owner)
        progress = _Progress()
        if sys.platform != "linux":
            progress.result.verification = {"status": "unavailable", "code": "verification.unsupported_supervisor"}
            return progress.result
        try:
            async with asyncio.timeout(request.limits.phase_timeout):
                for index, check in enumerate(request.checks):
                    if check.mode != "host":
                        progress.result.receipts.append({**self._base(check), "status": "skipped",
                                                         "reason": "Docker checks have no supported backend"})
                        continue
                    try:
                        request.validate_ceiling(permissions)
                    except ValueError as exc:
                        raise _Invalidated("verification.authority_changed") from exc
                    await self._one(request, context, owner, permissions, check, index, progress)
                if request.checks:
                    current = await self._probe(request, context, owner, permissions)
                    if current["fingerprint"] != context.candidate:
                        raise _Invalidated("verification.candidate_changed")
            progress.result.verification = verification_from(progress.result.receipts)
        except _Invalidated as exc:
            progress.result.verification = {"status": "invalidated", "code": exc.code}
        except asyncio.CancelledError:
            if not progress.cancellation_published:
                await _settled(owner.stop_heartbeat())
            raise
        except TimeoutError:
            if not progress.cancellation_published:
                await _settled(owner.stop_heartbeat())
                raise CheckExecutionUncertain("phase deadline left publication uncertain") from None
            progress.result.verification = {"status": "unavailable", "code": "verification.phase_timeout"}
        except Exception as exc:
            # Unexpected failures after authorization are publication uncertainty
            # too. Ordinary parent release must never erase that evidence.
            await _settled(owner.stop_heartbeat())
            raise CheckExecutionUncertain("check execution or publication requires reconciliation") from exc
        except BaseException:
            await _settled(owner.stop_heartbeat())
            raise
        return progress.result

    async def reconcile(self, binding: dict) -> dict:
        """Classify a recorded binding without launch, signals or ownership takeover.

        The flow publisher retains this original binding with its phase record.
        A terminal result is evidence to reconcile, not a published flow pass.
        """
        return await owned_call("reconcile", {
            "session_dir": str(self.store.session_dir(binding["parent_id"])), "binding": binding})
