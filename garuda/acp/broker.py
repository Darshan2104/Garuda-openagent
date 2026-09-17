"""Permission broker and approval lifecycle (P0.17, issue #27).

The one decision path for native and ACP approvals. A `PermissionEngine`
still draws the ceiling (allow/deny/ask per tool, path, and command); the
broker owns what happens at ASK and at every ACP request: park the approval
where UI, CLI, and SDK all read it, await an answer, and fail closed on
timeout or requester disconnect. Every outcome — allow, deny, timeout,
disconnect — is persisted, so an audit can replay each one.

Strict ceilings never silently downgrade: `describe_gaps` reports the
capabilities a strict policy needs but the adapter lacks, and `refuse_if_gaps`
turns that report into a refusal before anything runs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from garuda.acp.authority import (
    AgentCapabilities,
    AuthorityPolicy,
    NegotiationError,
    negotiate,
)
from garuda.core.permissions import PermissionDecision, PermissionEngine


class ApprovalOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"


@dataclass
class ApprovalRequest:
    approval_id: str
    action: str
    family: str
    runtime_id: str
    session_id: str
    requested_at: float = field(default_factory=time.time)


@dataclass
class ApprovalRecord:
    approval_id: str
    outcome: ApprovalOutcome
    action: str
    family: str
    runtime_id: str
    decided_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "outcome": self.outcome.value,
            "action": self.action,
            "family": self.family,
            "runtime_id": self.runtime_id,
            "decided_at": self.decided_at,
        }


class ApprovalBroker:
    """One broker per session. Park, answer, timeout, persist — same path always."""

    def __init__(
        self,
        engine: PermissionEngine,
        *,
        store=None,
        timeout_sec: float = 300.0,
        alive: Callable[[], bool] | None = None,
    ):
        self._engine = engine
        self._store = store
        self._timeout_sec = timeout_sec
        self._alive = alive or (lambda: True)
        self._parked: dict[str, ApprovalRequest] = {}
        self._answers: dict[str, asyncio.Future[bool]] = {}
        self._disconnected: set[str] = set()

    def pending(self) -> list[ApprovalRequest]:
        """Parked approvals, oldest first — the single surface UI cards read."""
        return sorted(self._parked.values(), key=lambda r: r.requested_at)

    def answer(self, approval_id: str, allow: bool) -> None:
        """Resolve a parked approval. Unknown ids fail closed, never silently."""
        request = self._parked.pop(approval_id, None)
        if request is None:
            raise KeyError(f"no pending approval {approval_id!r}")
        self._disconnected.discard(approval_id)
        future = self._answers.pop(approval_id, None)
        if future is not None and not future.done():
            future.set_result(bool(allow))

    def handler(self) -> Callable[[str], Any]:
        """An engine `approval_handler`: native ASK flows through the broker."""

        async def _handle(action: str) -> bool:
            allowed, _ = await self.decide_tool(
                "approval", {"action": action}, family="approval", runtime_id="native"
            )
            return allowed

        return _handle

    async def decide_tool(
        self,
        tool_name: str,
        arguments: dict,
        *,
        family: str,
        runtime_id: str,
        session_id: str = "",
        approval_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Apply the ceiling to one call. Returns (allowed, reason)."""
        allowed, reason = await self._engine.evaluate_tool_call(tool_name, dict(arguments))
        if allowed:
            self._record(session_id, approval_id or tool_name, ApprovalOutcome.ALLOW,
                         f"{tool_name}({arguments})", family, runtime_id)
            return True, None
        if reason is not None and "Approval required" in reason:
            return await self._park_and_wait(
                family, f"{tool_name}({arguments})", runtime_id, session_id, approval_id
            )
        self._record(session_id, approval_id or tool_name, ApprovalOutcome.DENY,
                     f"{tool_name}({arguments})", family, runtime_id)
        return False, reason

    async def decide_acp(
        self,
        family: str,
        action: str,
        *,
        runtime_id: str,
        session_id: str = "",
        approval_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Apply the ceiling to one ACP-side request (edit path, command, ...)."""
        decision = self._screen_family(family, action)
        if decision is PermissionDecision.ALLOW:
            self._record(session_id, approval_id or action, ApprovalOutcome.ALLOW,
                         action, family, runtime_id)
            return True, None
        if decision is PermissionDecision.DENY:
            self._record(session_id, approval_id or action, ApprovalOutcome.DENY,
                         action, family, runtime_id)
            return False, f"Ceiling denies {family}: {action}"
        return await self._park_and_wait(family, action, runtime_id, session_id, approval_id)

    def _screen_family(self, family: str, action: str) -> PermissionDecision:
        if family == "terminal":
            return self._engine.check_command(action)
        if family == "edit":
            return self._engine.check_path(action, "write")
        if family == "approval":
            return PermissionDecision.ASK
        return self._engine.check_tool(family)

    async def _park_and_wait(
        self, family: str, action: str, runtime_id: str, session_id: str, approval_id: str | None
    ) -> tuple[bool, str | None]:
        approval_id = approval_id or f"apr-{len(self._parked) + 1}-{int(time.time() * 1000)}"
        if approval_id in self._parked:
            raise KeyError(f"approval {approval_id!r} is already parked")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._parked[approval_id] = ApprovalRequest(
            approval_id=approval_id, action=action, family=family,
            runtime_id=runtime_id, session_id=session_id or runtime_id,
        )
        self._answers[approval_id] = future
        try:
            allowed = await asyncio.wait_for(_wait_answer(future, self._alive), self._timeout_sec)
        except TimeoutError:
            self._parked.pop(approval_id, None)
            self._answers.pop(approval_id, None)
            self._record(session_id, approval_id, ApprovalOutcome.TIMEOUT, action, family, runtime_id)
            return False, f"Approval timed out: {action}"
        except _Disconnected:
            self._parked.pop(approval_id, None)
            self._answers.pop(approval_id, None)
            self._disconnected.discard(approval_id)
            self._record(session_id, approval_id, ApprovalOutcome.DISCONNECTED, action, family, runtime_id)
            return False, f"Approval requester disconnected: {action}"
        self._parked.pop(approval_id, None)
        if approval_id in self._disconnected:
            self._disconnected.discard(approval_id)
            self._record(session_id, approval_id, ApprovalOutcome.DISCONNECTED, action, family, runtime_id)
            return False, f"Approval requester disconnected: {action}"
        outcome = ApprovalOutcome.ALLOW if allowed else ApprovalOutcome.DENY
        self._record(session_id, approval_id, outcome, action, family, runtime_id)
        return bool(allowed), None if allowed else f"Denied: {action}"

    def _record(
        self, session_id: str, approval_id: str, outcome: ApprovalOutcome,
        action: str, family: str, runtime_id: str,
    ) -> None:
        if self._store is None or not session_id:
            return
        record = ApprovalRecord(
            approval_id=approval_id, outcome=outcome, action=action,
            family=family, runtime_id=runtime_id,
        )
        try:
            self._store.update_meta(session_id, {f"approval:{approval_id}": record.to_dict()})
        except OSError:
            pass

    def disconnected(self, approval_id: str) -> None:
        """Deny a parked approval whose requester is gone. Audited as disconnect."""
        request = self._parked.get(approval_id)
        if request is None:
            raise KeyError(f"no pending approval {approval_id!r}")
        self._disconnected.add(approval_id)
        future = self._answers.get(approval_id)
        if future is not None and not future.done():
            future.set_result(False)


def describe_gaps(
    policy: dict[str, AuthorityPolicy], agent: AgentCapabilities
) -> list[str]:
    """Human-readable strict-policy gaps. Empty means negotiable as specified."""
    try:
        negotiate(policy, agent)
        return []
    except NegotiationError as exc:
        return [str(exc)]


async def _wait_answer(future: asyncio.Future[bool], alive: Callable[[], bool]) -> bool:
    while not future.done():
        if not alive():
            raise _Disconnected()
        await asyncio.sleep(0.02)
    return future.result()


class _Disconnected(Exception):
    pass
