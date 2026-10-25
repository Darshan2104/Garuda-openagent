"""Durable, user-authorized continuation of settled quota-stopped flows.

The scheduler only invokes ``tick``. Flow execution, leases and step receipts
remain owned by the existing engine. A lost dispatch is never blindly replayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from garuda.config.agent_home import global_settings_path
from garuda.core.sessions import SessionStore, validate_session_ref
from garuda.runtime import strict_store as ss
from garuda.runtime.ownership import current_owner, owner_liveness

QUOTA_ERROR = re.compile(
    r"(?:usage|rate|quota)[ _-]*(?:limit)?[ _-]*(?:has been |was |is )?"
    r"(?:reached|exceeded|exhausted)|usage limit|quota exceeded|insufficient_quota",
    re.I)
NON_QUOTA = re.compile(r"cancelled|canceled|permission|denied|approval|internal error", re.I)


class RecoveryWaiting(ValueError):
    """Quota changed after reservation; no workflow step has launched."""


def default_root() -> Path:
    override = os.environ.get("GARUDA_RECOVERY_ROOT")
    if override:
        return Path(override).expanduser().absolute()
    return global_settings_path().expanduser().parent / "quota-recovery"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def next_check(now):
    local = datetime.fromtimestamp(now).replace(minute=27, second=0, microsecond=0)
    if local.timestamp() <= now:
        local += timedelta(hours=1)
    return local.timestamp()


def workspace_checkpoint(workspace: str) -> str:
    from garuda.workspace.no_edits import manifest

    root = Path(workspace)
    info = root.stat()
    return digest({"identity": [info.st_dev, info.st_ino], "manifest": manifest(root)})


def evidence(store, session_id: str) -> str:
    from garuda.flows.engine import journal, receipts

    meta = store.load_meta(session_id)
    rows, events = receipts(store, session_id), journal(store, session_id)
    expected = {(r["step"], r["attempt"]) for r in rows}
    intents = [(e.get("step"), e.get("attempt")) for e in events if e.get("event") == "intent"]
    settled = [(e.get("step"), e.get("attempt")) for e in events if e.get("event") == "receipt"]
    if (len(expected) != len(rows) or set(intents) != expected
            or set(settled) != expected or len(intents) != len(rows)
            or len(settled) != len(rows) or len(events) != 2 * len(rows)
            or any(r.get("status") not in {"done", "stopped"} for r in rows)):
        raise ValueError("step closure/journal coverage is incomplete")
    return digest({"receipts": rows, "journal": events,
                   "continuation": meta.get("flow_continuation"),
                   "admission": meta.get("quota_recovery_admission"),
                   "stop": meta.get("flow_stop"), "task": meta.get("task"),
                   "workspace": meta.get("workspace"), "flow": meta.get("flow")})


class RecoveryStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else default_root()

    def settings(self) -> dict:
        return ss.read_document(self.root / "settings.json", versions=(1,)) or {"enabled": False}

    def _path(self, session_id):
        return self.root / f"flow-{validate_session_ref(session_id)}.json"

    def get(self, session_id):
        value = ss.read_document(self._path(session_id), versions=(1,))
        if value is not None and value.get("flow_session") != session_id:
            raise ValueError("recovery record identity disagrees")
        return value

    def records(self):
        if not self.root.exists():
            return []
        return [self.get(p.name[5:-5]) for p in sorted(self.root.glob("flow-*.json"))]

    def update(self, session_id, updates, *, token=None):
        with ss.exclusive_lock(self.root) as fd:
            prior = self.get(session_id)
            if prior and prior.get("state") in {"dispatch-reserved", "running"}:
                if token != prior.get("token"):
                    raise ValueError("continuation is already claimed")
            if prior and prior.get("state") == "cancelled":
                raise ValueError("continuation was cancelled")
            value = {**(prior or {}), **updates, "version": 1,
                     "flow_session": session_id, "updated_at": time.time()}
            ss.write_locked_document(fd, self._path(session_id), value)
            return value

    def claim(self, session_id, *, automatic=False):
        with ss.exclusive_lock(self.root) as fd:
            if automatic and self.settings().get("enabled") is not True:
                raise ValueError("automatic quota recovery is disabled")
            record = self.get(session_id)
            if not record or record.get("state") != "waiting-for-quota":
                raise ValueError("continuation is not waiting or is already claimed")
            record.update(state="dispatch-reserved", token=uuid.uuid4().hex, automatic=automatic,
                          owner=current_owner().to_dict(), updated_at=time.time())
            ss.write_locked_document(fd, self._path(session_id), record)
            return record

    def cancel(self, session_id):
        with ss.exclusive_lock(self.root) as fd:
            record = self.get(session_id)
            if not record:
                raise ValueError("no quota continuation for that flow")
            if record["state"] in {"dispatch-reserved", "running"}:
                raise ValueError("continuation already dispatched; stop the live flow instead")
            if record["state"] == "completed":
                raise ValueError("flow already completed")
            record.update(state="cancelled", reason="cancelled by user", updated_at=time.time())
            ss.write_locked_document(fd, self._path(session_id), record)
            return record


def project(record):
    if not record:
        return None
    fields = ("flow_session", "state", "reason", "next_check", "reset_at", "last_probe",
              "pending_step", "dispatches", "updated_at", "continuation_mode")
    return {k: record.get(k) for k in fields}


def publish(record):
    """Mirror durable scheduler evidence for read-only session/dashboard consumers."""
    SessionStore(root=record["sessions_root"]).update_meta(
        record["flow_session"], {"quota_recovery": project(record)})


class FlowRecovery:
    def __init__(self, store=None, *, root=None, record=None):
        self.sessions = store if store is not None else SessionStore()
        self.records = RecoveryStore(root)
        self.record = record
        self.admission = None
        self.continuation = None
        self.last_observation = None

    async def probe(self):
        from garuda.acp.client import child_environment
        from garuda.acp.limit_probe import refresh_codex
        from garuda.observability.limits import LimitStore

        settings = self.records.settings()
        executable = settings.get("codex_executable")
        if not isinstance(executable, str) or not Path(executable).is_absolute():
            return None
        return await asyncio.to_thread(refresh_codex, executable, LimitStore(),
                                       environment=child_environment())

    async def admit(self, runner):
        from garuda.config_sources import SourceCapture, verify_sources
        from garuda.flows.engine import _settled_sync

        if self.record is not None:
            meta = self.sessions.load_meta(runner.flow_session)
            self.admission = meta.get("quota_recovery_admission")
            if not isinstance(self.admission, dict):
                raise ValueError("flow has no recovery admission")
            if await _settled_sync(evidence, self.sessions, runner.flow_session) != self.record.get("evidence"):
                raise ValueError("flow receipts or continuation changed while waiting")
            if await _settled_sync(workspace_checkpoint, runner.workspace) != self.record.get("workspace_checkpoint"):
                raise ValueError("workspace changed while waiting; retained work requires review")
            if self.admission["configuration"] != digest(runner.resolved.config):
                raise ValueError("flow configuration changed while waiting")
            verify_sources(self.admission["sources"])
            self.continuation = meta.get("flow_continuation")
        else:
            with SourceCapture() as capture:
                plans = {role: runner._role_plan(role) for role in
                         sorted({s["role"] for s in runner.flow["steps"]})}
            runner.prepared_roles = plans
            if not any(plan and plan.runtime_id == "codex" for plan in plans.values()):
                await runner._publish(runner.flow_session, {"quota_recovery": {
                    "state": "unsupported", "reason": "quota recovery currently supports Codex roles"}})
                runner.quota_recovery = None
                return
            observation = await self.probe()
            sources = [asdict(source) for source in capture.facts()]
            sources += [asdict(source) for source in runner.configuration_sources]
            self.admission = {
                "version": 1, "configuration": digest(runner.resolved.config),
                "sources": sources,
                "roles": {role: plan.record() if plan else None for role, plan in plans.items()},
                "account_digest": observation.account_digest if observation else None,
                "harness_version": observation.harness_version if observation else None,
                "codex_executable": self.records.settings().get("codex_executable"),
            }
            await runner._publish(runner.flow_session, {"quota_recovery_admission": self.admission})
            await runner._publish(runner.flow_session, {"quota_recovery_root": str(self.records.root.absolute())})
        await self.validate(runner)
        if self.record is not None:
            from garuda.observability.limits import recovery_availability

            observation = await self.probe()
            available, reason = recovery_availability(observation,
                account_digest=self.record.get("account_digest"),
                harness_version=self.record.get("harness_version"), now=time.time())
            if not available:
                if observation is not None and (
                        observation.account_digest != self.record.get("account_digest")
                        or observation.harness_version != self.record.get("harness_version")):
                    raise ValueError(reason)
                raise RecoveryWaiting(reason)

    async def validate(self, runner):
        from garuda.config_sources import verify_sources

        verify_sources(self.admission["sources"])
        if self.admission["configuration"] != digest(runner.resolved.config):
            raise ValueError("admitted configuration changed")
        if self.admission.get("codex_executable") != self.records.settings().get("codex_executable"):
            raise ValueError("quota executable changed")
        for role, recorded in self.admission["roles"].items():
            plan = runner._role_plan(role)
            if (plan.record() if plan else None) != recorded:
                raise ValueError(f"admitted role {role} changed")

    async def before_launch(self, runner, plan):
        """Recheck after inputs/review preparation, directly before step intent."""
        from garuda.observability.limits import recovery_availability

        await self.validate(runner)
        if plan is None or plan.runtime_id != "codex":
            return
        observation = await self.probe()
        self.last_observation = observation
        available, reason = recovery_availability(observation,
            account_digest=self.admission.get("account_digest"),
            harness_version=self.admission.get("harness_version"), now=time.time())
        if not available:
            if observation is not None and (
                    observation.account_digest != self.admission.get("account_digest")
                    or observation.harness_version != self.admission.get("harness_version")):
                raise ValueError(reason)
            raise RecoveryWaiting(reason)

    async def stopped(self, runner, stop):
        from garuda.flows.engine import _settled_sync, journal, receipts
        from garuda.observability.limits import recovery_exhausted

        rows, entries = receipts(self.sessions, runner.flow_session), journal(self.sessions, runner.flow_session)
        last = entries[-1] if entries else {}
        failed = next((r for r in rows if r["step"] == last.get("step")
                       and r["attempt"] == last.get("attempt")), {})
        reason = str(stop)
        eligible = (stop.code == "flow.step_failed"
                    and (failed.get("role_plan") or {}).get("runtime_id") == "codex"
                    and failed.get("status") == "stopped"
                    and failed.get("closure", {}).get("status") == "closed"
                    and QUOTA_ERROR.search(reason) and not NON_QUOTA.search(reason))
        state, observation = "blocked", self.last_observation
        admission = self.admission or {}
        details = {}
        if stop.code == "flow.quota_wait" and admission.get("account_digest"):
            details = {"evidence": await _settled_sync(evidence, self.sessions, runner.flow_session),
                       "workspace_checkpoint": await _settled_sync(workspace_checkpoint, runner.workspace)}
            state = "waiting-for-quota"
        elif eligible:
            observation = await self.probe()
            valid, why = recovery_exhausted(observation, now=time.time())
            if valid and (observation.account_digest != admission.get("account_digest")
                          or observation.harness_version != admission.get("harness_version")):
                valid, why = False, "quota account or CLI version changed since admission"
            if valid:
                try:
                    await self.validate(runner)
                    details = {"evidence": await _settled_sync(evidence, self.sessions, runner.flow_session),
                               "workspace_checkpoint": await _settled_sync(workspace_checkpoint, runner.workspace)}
                    state, reason = "waiting-for-quota", "Codex quota exhausted; waiting for availability"
                except (OSError, ValueError) as exc:
                    reason = str(exc)
            else:
                reason = why
        prior = self.records.get(runner.flow_session) or {}
        record = self.records.update(runner.flow_session, {
            **details, "state": state, "reason": reason,
            "sessions_root": str(self.sessions.root.resolve()),
            "pending_step": stop.step or prior.get("pending_step"),
            "next_check": next_check(time.time()) if state == "waiting-for-quota" else None,
            "reset_at": observation.reset_at if observation else prior.get("reset_at"),
            "last_probe": observation.observed_at if observation else None,
            "account_digest": admission.get("account_digest"),
            "harness_version": admission.get("harness_version"),
            "dispatches": prior.get("dispatches", 0),
            "continuation_mode": "saved-flow-and-workspace-context",
        }, token=(self.record or {}).get("token"))
        await runner._publish(runner.flow_session, {"quota_recovery": project(record)})

    async def completed(self, runner):
        prior = self.records.get(runner.flow_session)
        if prior:
            row = self.records.update(runner.flow_session, {
                "state": "completed", "reason": "flow completed", "next_check": None},
                                      token=(self.record or {}).get("token"))
            await runner._publish(runner.flow_session, {"quota_recovery": project(row)})

    async def interrupted(self, runner, reason):
        row = self.records.update(runner.flow_session, {
            "state": "uncertain", "reason": reason, "next_check": None,
            "sessions_root": str(self.sessions.root.resolve()),
        }, token=(self.record or {}).get("token"))
        await runner._publish(runner.flow_session, {"quota_recovery": project(row)})

    async def tick(self):
        from garuda.flows.engine import FlowStopped
        from garuda.flows.service import FlowExecutionService
        from garuda.observability.limits import recovery_availability
        from garuda.runtime.capacity import CapacityError
        from garuda.workspace.lease import LeaseError

        reports = []
        if self.records.settings().get("enabled") is not True:
            return {"enabled": False, "flows": []}
        for row in self.records.records():
            sid, state = row["flow_session"], row.get("state")
            if state in {"dispatch-reserved", "running"}:
                if owner_liveness(row.get("owner") or {}) is not True:
                    row = self.records.update(sid, {"state": "uncertain", "reason":
                        "dispatch owner lost; automatic replay is refused"}, token=row["token"])
                    publish(row)
                reports.append(project(row))
                continue
            if state != "waiting-for-quota":
                continue
            claimed = None
            try:
                observation = await self.probe()
                available, reason = recovery_availability(observation,
                    account_digest=row.get("account_digest"), harness_version=row.get("harness_version"),
                    now=time.time())
                if not available:
                    row = self.records.update(sid, {"reason": reason, "next_check": next_check(time.time()),
                        "last_probe": observation.observed_at if observation else None})
                    publish(row)
                    reports.append(project(row))
                    continue
                claimed = self.records.claim(sid, automatic=True)
                sessions = SessionStore(root=row["sessions_root"])
                service = FlowExecutionService(sessions)
                await service.resume(sid, recovery_record=claimed, recovery_root=self.records.root)
                reports.append(project(self.records.get(sid)))
                break  # at most one dispatch per tick; later ticks can admit other flows
            except (OSError, ValueError, ss.StorageError, FlowStopped, CapacityError, LeaseError) as exc:
                current = self.records.get(sid)
                if (current and current.get("state") == "waiting-for-quota"
                        and self.records.settings().get("enabled") is not True):
                    return {"enabled": False, "flows": reports}
                if (claimed is not None and current
                        and current.get("token") == claimed["token"]
                        and current.get("state") == "dispatch-reserved"):
                    current = self.records.update(sid, {"state": "blocked", "reason": str(exc)},
                                                  token=claimed["token"])
                    publish(current)
                reports.append(project(current))
        return {"enabled": True, "flows": reports}
