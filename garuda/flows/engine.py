"""Sequential flows with step receipts (plan task C.6a, #158).

A flow runs its steps in order in **one** workspace. The parent flow session
takes the workspace lease once and holds it from the first step to the last;
each step borrows it through a capability the parent issues for that step
and revokes when the step ends (:class:`~garuda.interfaces.run_guard.LeaseCapability`),
so nothing outside the flow can take the workspace between steps and no
step can keep it.

For every step attempt:

1. its typed inputs are resolved from earlier receipts and checked
   (:mod:`garuda.flows.artifacts`) against the workspace as it is now;
2. the **intent** is journaled (fsynced) before anything launches;
3. the step runs as its own session (its own baseline and delta) under the
   role it names; a ``no-edits`` step is compared before and after (C.10);
4. its declared outputs are taken from the structured-output envelope only;
5. an immutable **receipt** records inputs, outputs, session, attempt,
   workspace versions and the result.

A step whose intent has no receipt — the worker died after launch — is
**quarantined**, never replayed: :func:`recover` records it and the flow can
only be resumed past steps that have receipts. A failed step, a missing
output or a no-edits change stops the flow.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from garuda.flows import artifacts as art
from garuda.flows import prompting
from garuda.workspace.confined_acp import Confinement, ConfinementRefused, require_image

JOURNAL = "journal.jsonl"
#: A checked flow stopped with one of these resumes from its pending step. A
#: ``flow.review_invalid`` reviewer receipt is settled (the step ran; only its
#: review block did not parse), so its resume runs the reviewer again on the
#: same frozen candidate (#761); an invalid review never counts as a round.
#: ``flow.interrupted`` is a checked step stopped by a signal (SIGTERM, Ctrl-C)
#: after its owned worker tree and confinement were proved closed (#760).
#: ``flow.artifact_malformed`` and ``flow.artifact_too_large`` are settled like
#: ``flow.output_missing``: the step ran, only its output could not be stored.
INTERRUPTED_STOP = "flow.interrupted"
# A plain step that itself failed or gave no usable output may run again, up
# to its ``retries`` (#810). Refusals, no-edits violations, interruptions,
# quota waits and input errors never do.
RETRYABLE_STOPS = frozenset({"flow.step_failed", "flow.output_missing", "flow.artifact_too_large",
                             "flow.artifact_malformed"})
RETRY_NOTE = ("[garuda] An earlier attempt of this step failed. Its partial edits may already be "
              "in the workspace; inspect existing work before acting.")
RESUMABLE_CHECKED_STOPS = {"workspace.readonly_unenforced", "flow.step_failed", "flow.output_missing",
                           "flow.artifact_malformed", "flow.artifact_too_large",
                           "flow.review_invalid", INTERRUPTED_STOP}


def _settled_refusal(receipt: dict) -> bool:
    """A stopped attempt whose owned worker reported a typed role refusal (#738).

    Like ``flow.step_failed`` it is resumable only as a settled attempt: its
    receipt names its session, and coverage still demands its bound closure and
    evidence boundary.
    """
    from garuda.runtime.role_control import refusal_code

    stop = receipt.get("stop")
    return (receipt.get("status") == "stopped" and receipt.get("success") is False
            and isinstance(receipt.get("session_id"), str) and isinstance(stop, dict)
            and stop.get("refused") is True and refusal_code(stop.get("code")))


class FlowStopped(Exception):
    def __init__(self, code: str, message: str, *, step: str | None = None,
                 cause: dict | None = None):
        # A refusal re-raised as a stop already says its code; say it once (#796).
        message = str(message).removeprefix(f"{code}:").strip() if code else message
        super().__init__(f"{code}: {message}")
        self.code = code
        #: Raised from a settled receipt of an owned worker's typed refusal (#738).
        self.settled_refusal = False
        self.step = step
        #: Why a failed step's session stopped (#749): ``{"code", "detail"}``, redacted
        #: and bounded, so the flow's stop says why without opening the step session.
        self.cause = cause


def _stop_record(stop: FlowStopped, **extra) -> dict:
    """A receipt's ``stop``: its code, message and, for a failed step, its cause."""
    return {"code": stop.code, "message": str(stop), **extra,
            **({"cause": stop.cause} if stop.cause else {})}


def _failed(what: str, outcome: StepResult, cause: dict | None, *,
            no_edits: bool = False) -> str:
    """``<what> did not complete`` and, when known, why (the launcher's own reason first).

    A read-only step whose turn ended after Garuda denied a file change says
    so: the agent tried to edit in a step that may not (#762).
    """
    reason = outcome.error or (cause or {}).get("detail")
    text = f"{what} did not complete" + (f": {reason}" if reason else "")
    if no_edits and reason and "after Garuda denied" in reason and any(
            f"kind={kind};" in reason for kind in ("edit", "delete", "move")):
        text += " (the agent attempted to change files in a read-only step)"
    return text


#: What a read-only step does, by the output it declares (#762).
READ_ONLY_JOBS = {
    "notes": "inspect the workspace and write notes on what the task involves",
    "plan": "inspect the workspace and write a plan for how the task should be done",
    "review": "review the work described in the flow inputs",
}


def read_only_instruction(outputs: list[str]) -> str:
    """The preamble a ``no-edits`` or ``readonly`` step's prompt carries (#762).

    Guidance only: the step's confinement and no-edits check still enforce it.
    """
    jobs = [READ_ONLY_JOBS[kind] for kind in outputs if kind in READ_ONLY_JOBS]
    job = "; ".join(jobs) or "inspect, plan or review as your role requires"
    produce = (", ".join(outputs) + " artifact" + ("s" if len(outputs) > 1 else "")
               if outputs else "answer")
    return (f"[garuda] This step is read-only. Your job in this step is to {job}, "
            "without editing files. You may read files and run read-only commands; "
            "do not edit, create, delete or move files, and do not apply patches or "
            "run commands that change the workspace. Another step makes the changes. "
            f"Produce only your {produce}.")


@dataclass
class StepLaunch:
    flow_session: str
    step_id: str
    role: str
    role_plan: Any
    prompt: str
    workspace: str
    capability: Any
    attempt: int
    no_edits: bool
    store: Any = None
    require_owned: bool = False
    runtime_catalog: Any = None
    approval_handler: Any = None
    workspace_identity: list[int] | None = None
    authority_workspace: str | None = None
    worktree_plan: dict | None = None
    authority_sources: tuple = ()
    external_approvals: bool = False
    quota_recovery: bool = False
    codex_executable: str | None = None
    quota_guard: Any = None


@dataclass
class StepResult:
    session_id: str
    success: bool
    output: str
    closure: dict | None = None
    #: Why the step's agent stopped, when it failed rather than finished.
    error: str = ""
    quota_wait: bool = False
    #: An owned role worker's typed refusal, ``{"code", "message"}`` (#738). The step
    #: still has its proved closure; the flow stops with the role's own code.
    refusal: dict | None = None


Launcher = Callable[[StepLaunch], Awaitable[StepResult]]


@dataclass
class FlowResult:
    flow_session: str
    receipts: list[dict] = field(default_factory=list)
    stopped: FlowStopped | None = None

    @property
    def completed(self) -> bool:
        return self.stopped is None


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def advanced(updates: dict) -> dict:
    """``updates`` stamped with a fresh ``updated_at`` (#763, #776).

    The store merge only defaults ``updated_at`` and ``begin`` already set it,
    so every flow-session state publication must carry its own stamp, or the
    session keeps reporting its creation time as its last activity.
    """
    return {"updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), **updates}


def _timing(started: tuple[str, float] | None) -> dict:
    """When a step attempt launched and settled (#726).

    ``started_at`` is when its intent was journaled, ``finished_at`` when its
    receipt was built, and ``duration_ms`` the monotonic time between them.
    An attempt refused before launch has no start and no duration.
    """
    if started is None:
        return {"started_at": None, "finished_at": _stamp(), "duration_ms": None}
    return {"started_at": started[0], "finished_at": _stamp(),
            "duration_ms": max(0, round((time.monotonic() - started[1]) * 1000))}


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append(path: Path, record: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_once(path: Path, record: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o400)
    try:
        os.write(fd, json.dumps(record, sort_keys=True, indent=1).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


async def _settled_sync(call, *args, complete_on_cancel: bool = False, **kwargs):
    """Keep bounded blocking work off the loop and await its actual settlement.

    A cancelled ``to_thread`` future does not stop its thread. Flow publication
    must therefore wait for the underlying lock/fsync operation before the
    parent lease can release. Only a completed terminal publication may absorb
    a cancellation that arrived while it was being committed.
    """
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    value = task.result()
    if cancelled and not complete_on_cancel:
        raise asyncio.CancelledError
    return value


def _settle_interrupted_step(store, session_id: str) -> None:
    """Mark a step session its killed worker left ``running`` as cancelled (#760).

    Called only after the worker's whole tree was proved closed; a session
    that already settled keeps its own outcome.
    """
    from garuda.runtime.session_state import interrupted

    def _settle(meta: dict) -> dict:
        if meta.get("status") != "running":
            return {}
        return {"status": "cancelled", "error_code": INTERRUPTED_STOP,
                "state": interrupted(cancelled=True)}

    try:
        store.mutate_meta(session_id, _settle)
    except Exception:
        pass  # best effort: the flow receipt is the authority for the attempt


def flow_dir(store, flow_session: str) -> Path:
    return Path(store.session_dir(flow_session)) / "flow"


def journal(store, flow_session: str) -> list[dict]:
    path = flow_dir(store, flow_session) / JOURNAL
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"event": "torn"})  # a crash mid-append
    return out


def receipts(store, flow_session: str) -> list[dict]:
    directory = flow_dir(store, flow_session) / "receipts"
    if not directory.is_dir():
        return []
    found = [json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")]
    return sorted(found, key=lambda r: (r.get("index", 0), r.get("attempt", 0)))


def _receipt_name(step: str, attempt: int) -> str:
    return f"{step}-{attempt}.json"


def unsettled_attempts(store, flow_session: str) -> list[tuple[str, int]]:
    """Launched step attempts (journal intents) with no receipt, read only (#760)."""
    directory = flow_dir(store, flow_session) / "receipts"
    return [(entry["step"], entry["attempt"]) for entry in journal(store, flow_session)
            if entry.get("event") == "intent" and isinstance(entry.get("step"), str)
            and type(entry.get("attempt")) is int
            and not (directory / _receipt_name(entry["step"], entry["attempt"])).exists()]


def recover(store, flow_session: str) -> list[str]:
    """Quarantine every launched step that has no receipt. Never replays."""
    directory = flow_dir(store, flow_session) / "receipts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    quarantined = []
    for entry in journal(store, flow_session):
        if entry.get("event") != "intent":
            continue
        name = _receipt_name(entry["step"], entry["attempt"])
        if (directory / name).exists():
            continue
        try:
            _write_once(directory / name, {
                "index": entry.get("index", 0), "step": entry["step"],
                "attempt": entry["attempt"], "status": "quarantined",
                "reason": "the worker stopped after this step was launched; it is not "
                          "replayed — inspect the workspace before continuing",
                "recorded_at": _now(),
            })
        except FileExistsError:
            continue
        quarantined.append(entry["step"])
    if quarantined:
        store.update_meta(flow_session, advanced({"flow_state": "quarantined"}))
    return quarantined


class FlowRunner:
    """Run one flow. ``launcher`` starts a step's session (see :class:`StepLaunch`)."""

    def __init__(self, store, workspace, name: str, flow: dict, resolved, *, task: str,
                 launcher: Launcher, flow_session: str | None = None,
                 lease_ttl: float | None = None, starter_record: dict | None = None,
                 verification_request=None, verification_permissions=None,
                 prepared_roles: dict | None = None, runtime_catalog=None,
                 authority_sources: tuple = (), operation_binding: dict | None = None,
                 on_admitted=None, approval_handler=None, launch_kind: str = "flow",
                 session_name: str | None = None, execution_workspace: dict | None = None,
                 worktree_plan: dict | None = None, prepared_fallbacks: dict | None = None,
                 background_admission=None, configuration_sources: tuple | None = None,
                 verification_cli_checks: tuple[str, ...] | None = None):
        if (verification_request is None) != (verification_permissions is None):
            raise ValueError("flow verification requires its frozen permission ceiling")
        if verification_cli_checks is not None and (verification_request is None
                or operation_binding is not None or background_admission is not None):
            raise ValueError("a saved check selection requires a standalone checked flow")
        if (operation_binding is None) != (on_admitted is None):
            raise ValueError("owned operation binding and admission callback are inseparable")
        if launch_kind not in {"flow", "run"}:
            raise ValueError("unsupported flow launch kind")
        if launch_kind == "run" and (
                operation_binding is None or len(flow.get("steps", [])) != 1
                or any(set(step) - {"id", "role", "write_policy"} for step in flow["steps"])):
            # A run parent hands the role its exact task: one owned step with
            # no flow inputs, outputs or review to describe in a preamble.
            raise ValueError("an owned run parent is exactly one plain role step")
        self.store = store
        self.workspace = str(Path(workspace).resolve())
        self.name = name
        from garuda.config.garuda_yaml import (
            effective_flow_steps,
            validate_flow_retries,
            validate_flow_reviews,
        )

        steps_path = f"flows.{name}.steps"
        self.steps = effective_flow_steps(flow, path=steps_path)
        validate_flow_reviews(self.steps, path=steps_path)
        validate_flow_retries(self.steps, path=steps_path)
        self.flow = flow
        self.resolved = resolved
        self.task = task
        self.launcher = launcher
        self.flow_session = flow_session or str(uuid.uuid4())
        self.dir = flow_dir(store, self.flow_session)
        self.lease_ttl = lease_ttl
        self.starter_record = starter_record
        self.verification_request = verification_request
        self.verification_permissions = verification_permissions
        self.verification_cli_checks = verification_cli_checks
        self.prepared_roles = dict(prepared_roles) if prepared_roles is not None else None
        self.prepared_fallbacks = dict(prepared_fallbacks or {})
        self._fallback_probes = {}
        self._review_approvals = {}
        self.runtime_catalog = runtime_catalog
        self.authority_sources = tuple(authority_sources)
        self.configuration_sources = (self.authority_sources if configuration_sources is None
                                      else tuple(configuration_sources))
        self.operation_binding = dict(operation_binding) if operation_binding is not None else None
        self.on_admitted = on_admitted
        self.approval_handler = approval_handler
        self.launch_kind = launch_kind
        self.session_name = session_name
        self.execution_workspace = execution_workspace
        self.worktree_plan = worktree_plan
        self.background_admission = background_admission
        self.require_owned = verification_request is not None or operation_binding is not None
        self._evidence = None
        self._evidence_count = 0
        self._prior_attempts = {}
        self._owned_snapshot = None
        self._verification_quarantined = False
        self._interrupted_acceptance_receipts = None
        self.quota_recovery = None
        self.quota_recovery_status = None

    def _step_cause(self, outcome: StepResult) -> dict | None:
        """Why a failed step stopped (#749): its session's recorded error code and detail.

        Read from the step session (in-process and owned-worker launches both record
        it there), else the launcher's own reason. Agent-supplied, so redacted and
        bounded again here: the cause lands in the flow's stop and its receipt.
        """
        from garuda.context.redact import redact_text

        meta: Any = {}
        if outcome.session_id and self.store is not None:
            try:
                meta = self.store.load_meta(outcome.session_id)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}

        def bounded(value, limit):
            if not isinstance(value, str) or not value.strip():
                return None
            return redact_text(" ".join(value.split()))[0][:limit] or None

        cause = {"code": bounded(meta.get("error_code"), 120),
                 "detail": bounded(meta.get("error_detail") or outcome.error, 500)}
        return cause if any(cause.values()) else None

    async def _publish(self, session_id: str, updates: dict, *, terminal: bool = False):
        # Every publication is a state change, so it advances `updated_at` (#763);
        # the store merge only defaults it, and `begin` already set it once.
        updates = {"updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), **updates}
        return await _settled_sync(self.store.update_meta, session_id, updates,
                                   complete_on_cancel=terminal)

    async def _touch(self) -> None:
        """Advance the flow session's `updated_at` after a step receipt (#763).

        Best effort: a receipt is already durable, and the next state
        publication surfaces a storage failure on its own.
        """
        try:
            await self._publish(self.flow_session, {})
        except Exception:
            pass

    # -- helpers -----------------------------------------------------------------------

    def _version(self) -> str | None:
        from garuda.core.acceptance import fingerprint

        return fingerprint(self.workspace)

    async def _version_for_flow(self) -> str | None:
        return await self._evidence.version() if self._evidence is not None else self._version()

    def _version_binds(self) -> bool:
        """Whether a workspace version can tell candidates apart at all.

        Outside a git work tree every state fingerprints alike, so a matching
        version proves nothing; an unreadable repository proves nothing either.
        """
        from garuda.workspace.diff import BASELINE_UNSUPPORTED_NONREPO, DiffError, capture_baseline

        try:
            return capture_baseline(self.workspace).state != BASELINE_UNSUPPORTED_NONREPO
        except DiffError:
            return False

    def _role_plan(self, role: str, *, step: str | None = None, acp_only: bool = False,
                   no_edits: bool = False):
        """Resolve the role, using its primary for ACP confinement checks.

        A refusal stops the flow at ``step``, the step id, never the role name (#796);
        a per-role check (quota recovery admission) names the first step of that role.
        """
        if step is None:
            step = next((s["id"] for s in self.steps
                         if role == s.get("role") or role in (s.get("parallel") or ())), None)
        if self.prepared_roles is not None:
            if role not in self.prepared_roles:
                raise FlowStopped("flow.role_unprepared",
                                  f"role {role} is absent from the admitted authority", step=step)
            return self.prepared_roles[role]
        from garuda.agents.fallbacks import choose
        from garuda.agents.setup import prepare_runtime_catalog
        from garuda.runtime.roles import RoleRefused, plan_role

        resolved = replace(self.resolved, role=role)
        catalog = prepare_runtime_catalog(self.workspace)
        # A step's fallback is decided before it launches and lands in its receipt.
        try:
            from garuda.agents import role_agent

            plan = plan_role(resolved, catalog)
            if plan is None or (acp_only and plan.kind != "acp"):
                return None
            if not acp_only:
                plan = choose(plan, resolved, catalog, no_edits=no_edits)
            return role_agent.bind(plan, self.workspace)
        except RoleRefused as exc:
            raise FlowStopped(exc.code, str(exc), step=step) from exc

    def _validate_operation_workspace(self) -> None:
        if self.operation_binding is None:
            return
        expected = self.operation_binding.get("workspace_identity")
        try:
            root = Path((self.execution_workspace or {}).get("source", self.workspace))
            resolved = root.resolve(strict=True)
            info = os.stat(resolved)
        except OSError as exc:
            raise FlowStopped(
                "flow.workspace_changed",
                "the admitted workspace is no longer available") from exc
        if (resolved != root or type(expected) is not list or len(expected) != 2
                or [info.st_dev, info.st_ino] != expected):
            raise FlowStopped(
                "flow.workspace_changed",
                "the admitted workspace identity changed before child launch")
        if self.execution_workspace is not None:
            target = Path(self.workspace)
            info = target.stat()
            if (str(target.resolve(strict=True)) != self.execution_workspace["path"]
                    or [info.st_dev, info.st_ino] != self.execution_workspace["identity"]):
                raise FlowStopped("flow.workspace_changed", "the selected worktree identity changed")

    def _no_edits(self, step: dict, role: str) -> bool:
        spec = self.resolved.config.get("roles", {}).get(role, {})
        return "no-edits" in (step.get("write_policy"), spec.get("write_policy"))

    def _check_confinement_image(self, step: dict, plan, *, no_edits: bool) -> None:
        from garuda.runtime.roles import acp_permission

        if plan is not None and plan.kind == "acp" and acp_permission(plan, no_edits=no_edits) == "readonly":
            try:
                require_image(Confinement.from_config(plan.harness))
            except ConfinementRefused as exc:
                raise FlowStopped(exc.code, exc.message, step=step["id"]) from exc

    def _check_confinement_images(self) -> None:
        for step in self.steps:
            if self.require_owned and "parallel" in step:
                continue  # owned execution cannot launch parallel groups
            for role in step.get("parallel") or (step["role"],):
                no_edits = self._no_edits(step, role)
                plan = self._role_plan(role, step=step["id"], acp_only=True)
                self._check_confinement_image(step, plan, no_edits=no_edits)

    def _review_plans(self, step: dict, terminal: dict):
        """Resolve the reviewed and reviewer plans, refusing a configured collision.

        The same resolution the review loop launches with; a required review
        whose configured reviewer shares an identity with the reviewed role
        can never finish, so it stops before anything runs.
        """
        from garuda.flows import review as rv

        reviewed_plan = self._role_plan(step["role"], step=step["id"], no_edits=self._no_edits(step, step["role"]))
        reviewer_plan = self._role_plan(terminal["role"], step=terminal["id"],
                                         no_edits=self._no_edits(terminal, terminal["role"]))
        if step["review"].get("independent", True):
            why = rv.check_independent(self.resolved.config, step["role"], reviewed_plan,
                                       terminal["role"], reviewer_plan)
            if why:
                raise FlowStopped("flow.review_not_independent", why, step=terminal["id"])
        return reviewed_plan, reviewer_plan

    def _check_review_independence(self, settled: set | frozenset = frozenset()) -> None:
        """Preflight every required review still to run before the first step (#753).

        ``settled`` names reviewed steps a resume will never run again.
        """
        for step in self.steps:
            if (step.get("review") and step["review"].get("independent", True)
                    and step["id"] not in settled):
                self._review_plans(step, self.steps[-1])

    def _closed_step(self, outcome: StepResult, step: dict, attempt: int, role_record=None,
                     *, interrupted: bool = False) -> dict:
        """Keep only bound lifetime facts, never raw process output tails.

        Only an ``interrupted`` attempt (#760) may have closed as ``cancelled``:
        its supervisor still published the ECHILD receipt for the whole tree.
        """
        from garuda.core.acceptance_request import is_digest

        closure = outcome.closure
        process = closure.get("process") if isinstance(closure, dict) else None
        if not isinstance(self._owned_snapshot, dict):
            raise FlowStopped("flow.cleanup_unproved",
                              "the parent ownership snapshot is unavailable", step=step["id"])
        expected_owner = {key: self._owned_snapshot["lease"][key]
                          for key in ("pid", "identity", "pgid", "epoch")}
        if (not isinstance(closure, dict) or not isinstance(process, dict)
                or closure.get("version") != 1 or not is_digest(closure.get("request_digest"))
                or closure.get("session_id") != outcome.session_id
                or closure.get("parent_id") != self.flow_session
                or closure.get("step_id") != step["id"] or closure.get("attempt") != attempt
                or closure.get("parent_owner") != expected_owner
                or (self.operation_binding is not None and closure.get("workspace_identity")
                    != (self.execution_workspace["identity"] if self.execution_workspace else
                        self.operation_binding["workspace_identity"]))
                or not isinstance(closure.get("workspace_identity"), list)
                or len(closure["workspace_identity"]) != 2
                or any(type(value) is not int or value < 0 for value in closure["workspace_identity"])
                or process.get("cleanup") != {"backend": "linux-subreaper", "echild": True}
                or process.get("outcome") not in ({"exited", "cancelled"} if interrupted else {"exited"})
                or not isinstance(process.get("supervisor"), dict)
                or not isinstance(process.get("command"), dict)):
            raise FlowStopped("flow.cleanup_unproved",
                              "step ended without bound descendant closure", step=step["id"])
        confinement = closure.get("confinement")
        if isinstance(role_record, dict) and role_record.get("kind") == "acp" and (
                self._no_edits(step, step["role"]) or role_record.get("permissions") == "readonly"):
            prefix = f"garuda-role-{outcome.session_id}"
            if confinement != {"backend": "docker", "absent": [f"{prefix}-probe", f"{prefix}-run"]}:
                raise FlowStopped("flow.cleanup_unproved", "ACP container closure is unproved", step=step["id"])
        return {key: closure[key] for key in ("version", "request_digest", "session_id",
                                             "parent_id", "step_id", "attempt",
                                             "workspace_identity", "parent_owner")} | {
            "process": {key: process[key] for key in ("supervisor", "command", "cleanup",
                                                      "outcome", "exit_code")},
            **({"confinement": confinement} if confinement is not None else {})}

    def _evidence_coverage(self, entries: list[dict], attempts: list[dict]) -> list[dict]:
        """Tie every settled step intent to its receipt and boundary digest."""
        from garuda.core.acceptance_request import is_digest

        by_attempt = {(row["step"], row["attempt"]): row for row in attempts}
        if (len(by_attempt) != len(attempts) or len(entries) != 2 * len(attempts)
                or len(attempts) > 512):
            raise FlowStopped("flow.evidence_incomplete", "step attempt coverage is incomplete")
        by_id = {step["id"]: step for step in self.steps}
        coverage = []
        for intent, recorded in zip(entries[::2], entries[1::2], strict=True):
            key = intent.get("step"), intent.get("attempt")
            receipt = by_attempt.pop(key, {})
            boundary = receipt.get("evidence_boundary")
            stopped = (receipt.get("status") == "stopped" and self.verification_cli_checks is not None
                       and ((receipt.get("stop") or {}).get("code") in RESUMABLE_CHECKED_STOPS
                            or _settled_refusal(receipt)))
            if (intent.get("event") != "intent" or recorded.get("event") != "receipt"
                    or key != (recorded.get("step"), recorded.get("attempt"))
                    or intent.get("index") != receipt.get("index")
                    or intent.get("index") != by_id.get(key[0], {}).get("index")
                    or key[0] not in by_id
                    or type(key[1]) is not int
                    or not (stopped or (receipt.get("status") == "done" and receipt.get("success") is True))):
                raise FlowStopped("flow.evidence_incomplete", "step receipt does not cover its intent")
            if (stopped and receipt["stop"]["code"] == "workspace.readonly_unenforced"
                    and receipt.get("session_id") is None and receipt.get("success") is False
                    and boundary is None):
                continue  # confinement refused before agent start
            if (not isinstance(boundary, dict)
                    or not is_digest(boundary.get("boundary_digest"))):
                raise FlowStopped("flow.evidence_incomplete",
                                  "step receipt or boundary does not cover its intent")
            if key in self._prior_attempts:
                if receipt != self._prior_attempts[key]:
                    raise FlowStopped("flow.evidence_incomplete", "a settled step receipt changed")
            else:
                self._closed_step(StepResult(receipt["session_id"], True, "",
                                             receipt.get("closure")), by_id[key[0]], key[1], receipt.get("role_plan"),
                                  interrupted=stopped and receipt["stop"]["code"] == INTERRUPTED_STOP)
            coverage.append({"step": key[0], "attempt": key[1],
                             "boundary_digest": boundary["boundary_digest"]})
        return coverage

    async def _finalize_verification(self, lease, reference, manifest, *, supersedes=None):
        """Keep the original parent until the check commit and state both publish.

        ``supersedes`` (#772) is the digest of an interrupted phase this one
        replaces; the owned prepare re-proves that phase has no possible writer.
        """
        from garuda.core.acceptance_request import CheckContext
        from garuda.core.acceptance_runner import AcceptanceRunner
        from garuda.flows.verification_records import OwnedFlowVerification
        from garuda.runtime.session_state import finished

        request, permissions = self.verification_request, self.verification_permissions
        request.validate_ceiling(permissions)
        if not request.checks:
            raise FlowStopped("flow.verification_not_requested", "no trusted check was selected")
        runner = None
        try:
            candidate = await self._evidence.version()
            identity = manifest["binding"]["workspace_identity"]
            context = CheckContext(self.flow_session, str(uuid.uuid4()), self.workspace,
                                   candidate, reference["manifest_digest"],
                                   tuple(manifest["changed"]), True, tuple(identity))
            publisher = OwnedFlowVerification(self.store, lease, request, context, permissions,
                                              baseline_digest=self._evidence.baseline_digest)
            phase = await publisher.prepare(supersedes=supersedes)
            # The intent precedes any acceptance launch. A crash after the
            # private intent but before this metadata update remains visible
            # and cannot be treated as a fresh flow run.
            await self._publish(self.flow_session, {
                "flow_verification_phase": {"phase_id": context.phase_id,
                                            "digest": phase["phase_digest"]}})
            runner = AcceptanceRunner(self.store)
            result = await lease.race(runner.run(request, context, lease, permissions))
            commitment = await publisher.commit(phase["phase_digest"], result)
            await self._publish(self.flow_session, {
                "flow_state": "completed", "status": "completed",
                "state": finished(success=True, verification=result.verification),
                "acceptance_receipts": result.receipts,
                "flow_verification_commit": commitment}, terminal=True)
        except asyncio.CancelledError:
            # Cancellation before launch authorization, or after a terminal
            # cancellation receipt, leaves no possible writer. Ambiguous
            # activation still retains quarantine.
            if runner is None or not runner.launch_ambiguous:
                if runner is not None:
                    self._interrupted_acceptance_receipts = list(runner.result.receipts)
                raise
            self._verification_quarantined = True
            await lease.stop_heartbeat()
            raise
        except BaseException:
            # An activation or publication may have happened. Retain the
            # original owner until the phase is reconciled; never claim that
            # a failed write rolled the check back.
            self._verification_quarantined = True
            await lease.stop_heartbeat()
            raise

    async def _inputs(self, step: dict, done: list[dict]) -> list[tuple[art.ArtifactRef, str]]:
        version = await self._version_for_flow()
        resolved = []
        for kind in step.get("inputs", []):
            # The latest receipt with this kind supplies it. A parallel group's
            # receipt holds one per member, and every member's is delivered (#809).
            receipt = next((r for r in reversed(done)
                            if any(o["type"] == kind for o in r.get("outputs", []))), None)
            if receipt is None:
                raise FlowStopped("flow.input_missing", f"no earlier step produced a {kind}",
                                  step=step["id"])
            refs = [art.ArtifactRef.from_dict(o) for o in receipt["outputs"] if o["type"] == kind]
            for ref in refs if "members" in receipt else refs[:1]:
                try:
                    resolved.append((ref, art.load(self.dir, ref, workspace_version=version)))
                except art.ArtifactError as exc:
                    raise FlowStopped(exc.code, str(exc), step=step["id"]) from exc
        return resolved

    def _read_only(self, step: dict, role: str) -> bool:
        """A ``no-edits`` step or a ``readonly`` role: one that must not change files."""
        spec = self.resolved.config.get("roles", {}).get(role, {})
        return self._no_edits(step, role) or spec.get("permissions") == "readonly"

    def _prompt(self, step: dict, role: str, inputs, *, read_only: bool | None = None,
                extra=None) -> str:
        """The step's prompt (:func:`garuda.flows.prompting.assemble`); ``extra`` ends it."""
        if read_only is None and self.launch_kind != "run":
            read_only = self._read_only(step, role)
        return prompting.assemble(task=self.task, flow=self.name, step=step, role=role,
                                  inputs=inputs, read_only=bool(read_only),
                                  run=self.launch_kind == "run", extra=extra)

    # -- running -----------------------------------------------------------------------

    def _resume_request(self):
        from garuda.core.acceptance_records import digest

        return {"version": 1, "checks": list(self.verification_cli_checks),
                "request": self.verification_request.identity, "flow": digest(self.flow)}

    def _verification_records(self):
        from garuda.flows.verification_records import FlowVerificationRecords

        return FlowVerificationRecords(self.store.session_dir(self.flow_session), self.flow_session)

    def _checked_history(self):
        from garuda.core.session_records import (
            BoundedSessionRecords,
            RecordError,
            list_json,
            parse_json,
        )

        reader = BoundedSessionRecords(self.store.root, byte_budget=16 * 1024 * 1024)
        directory = Path(self.flow_session) / "flow"
        with reader.selected_session(self.flow_session):
            names = list_json(reader, directory / "receipts", entry_limit=512)
            attempts = [parse_json(reader.read(directory / "receipts" / name)) for name in names]
            try:
                raw = reader.read(directory / JOURNAL)
            except RecordError as exc:
                if attempts or not isinstance(exc.__cause__, FileNotFoundError):
                    raise
                raw = b""
        return [parse_json(line) for line in raw.splitlines()], attempts

    def _resume_inputs(self, expected_checkpoint):
        from garuda.core.acceptance_records import digest
        from garuda.core.acceptance_request import is_digest
        from garuda.runtime.strict_store import CorruptRecord, StorageError

        try:
            prior = self.store.load_meta(self.flow_session)
            if self._verification_records().resume_request(prior) != self._resume_request():
                raise CorruptRecord("the original check authority or flow definition changed")
            checkpoint = prior["flow_resume_checkpoint"]
            baseline = prior["flow_evidence_baseline"]["baseline_digest"]
            entries, attempts = self._checked_history()
            if (checkpoint != expected_checkpoint or not is_digest(baseline)
                    or checkpoint["attempts"] != digest([entries, attempts])
                    or prior["workspace"] != self.workspace or prior["task"] != self.task):
                raise CorruptRecord("the stopped flow's history or workspace changed")
            # The stop was published only after verifying these exact receipts
            # against their original live owners. Keep their closure bindings.
            self._prior_attempts = {(row["step"], row["attempt"]): row for row in attempts}
            coverage = self._evidence_coverage(entries, attempts)
            existing = sorted(attempts, key=lambda row: (row["index"], row["attempt"]))
            return prior, baseline, checkpoint["candidate"], coverage, existing
        except (StorageError, OSError, ValueError, KeyError, TypeError) as exc:
            raise FlowStopped("flow.verification_reconciliation_required",
                              "original check authority or stopped flow evidence could not be proved") from exc

    async def _stop_checkpoint(self, lease):
        from garuda.core.acceptance_records import digest
        from garuda.core.acceptance_work import AcceptanceWorkError
        from garuda.runtime.process_supervision import CleanupUncertain
        from garuda.runtime.strict_store import StorageError

        try:
            entries, attempts = await _settled_sync(self._checked_history)
            self._evidence_coverage(entries, attempts)
            return {"attempts": digest([entries, attempts]),
                    "candidate": await self._evidence.version(), "id": uuid.uuid4().hex}
        except CleanupUncertain as exc:
            await lease.stop_heartbeat()
            raise FlowStopped("flow.cleanup_unproved",
                              "the stopped flow evidence worker did not settle") from exc
        except (AcceptanceWorkError, StorageError, OSError, ValueError, KeyError, TypeError, FlowStopped) as exc:
            raise FlowStopped("flow.verification_reconciliation_required",
                              "the stopped flow evidence could not be recorded") from exc

    def _begin(self) -> None:
        # A reserved ID is not permission to reuse an earlier or partially
        # published session. Reserve the directory only after the workspace
        # lease has been acquired.
        if self.background_admission is not None:
            self.background_admission.validate()
            meta = self.store.load_meta(self.flow_session)
            policy = meta.get("publication_policy")
            if (meta.get("background_operation") != self.background_admission.operation
                    or meta.get("flow_operation") is not None or self.dir.exists()
                    or not isinstance(policy, dict) or policy.get("kind") not in {"owned-flow", "flow-verification"}
                    or policy.get("session_id") != self.flow_session):
                raise FlowStopped("flow.background_invalid", "queued parent cannot be adopted")
            self.store.update_meta(self.flow_session, advanced({
                "workspace": self.workspace, "status": "running"}))
        else:
            try:
                self.store.session_dir(self.flow_session).mkdir(
                    mode=0o700, parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise FlowStopped("flow.session_exists", "the flow session already exists") from exc
            if self.require_owned:
                from garuda.core.session_publication import protect_session

                protect_session(
                    self.store.session_dir(self.flow_session) / "meta.json",
                    kind="flow-verification" if self.verification_request is not None else "owned-flow")
            self.store.begin(self.flow_session, task=self.task, model="flow",
                             agent=f"flow:{self.name}", workspace=self.workspace, name=self.session_name)
        # Path.mkdir(parents=True) creates missing ancestors with the default
        # mode, which makes the flow directory too open for owned records.
        self.dir.mkdir(mode=0o700)
        request_reference = (self._verification_records().save_resume_request(self._resume_request())
                             if self.verification_cli_checks is not None else None)
        self.store.update_meta(self.flow_session, {
            "kind": "flow", "flow": {"name": self.name, "steps": [s["id"] for s in
                                                                 self.steps]},
            "flow_state": "running",
            **({"flow_resume_request": request_reference} if request_reference is not None else {}),
            "review_policies": [{"step": s["id"], "reviewer": s["review"]["by"],
                                 "policy": "required" if s["review"].get("independent", True) else "waived"}
                                for s in self.steps if "review" in s],
            **({"starter": self.starter_record} if self.starter_record is not None else {}),
            **({"flow_operation": self.operation_binding}
               if self.operation_binding is not None else {}),
            **({"launch_kind": "run"} if self.launch_kind == "run" else {})})
        if self.quota_recovery_status is not None:
            self.store.update_meta(self.flow_session, {"quota_recovery": self.quota_recovery_status})
        elif self.require_owned:
            from garuda.flows.quota_recovery import RecoveryStore

            if RecoveryStore().settings().get("enabled") is True:
                self.store.update_meta(self.flow_session, {"quota_recovery": {
                    "state": "unsupported", "reason": "owned flow recovery is not supported"}})
        if self.execution_workspace is not None:
            from garuda.interfaces.session_service import plan_meta

            self.store.update_meta(self.flow_session, {
                **plan_meta(self.worktree_plan),
                "flow_execution_workspace": self.execution_workspace})
        (self.dir / "receipts").mkdir(mode=0o700)

    def _lease_mode(self) -> str:
        if self.require_owned:
            if (self.launch_kind == "run" and self.verification_request is None
                    and all(self._no_edits(s, s.get("role", "")) for s in self.steps)):
                return "read-only"
            return "mutating"
        if all(self._no_edits(s, s.get("role", "")) for s in self.steps if "role" in s):
            return "read-only"
        return "mutating"

    async def run(self, *, resume: bool = False, checked_checkpoint: dict | None = None) -> FlowResult:
        from garuda.interfaces.run_guard import WorkspaceLeaseGuard
        from garuda.runtime.process_supervision import CleanupUncertain
        from garuda.runtime.session_state import finished, interrupted

        checked_resume = resume and checked_checkpoint is not None and self.verification_cli_checks is not None
        if (self.require_owned or self.quota_recovery is not None) and any(
                step.get("retries") for step in self.steps):
            # An owned or checked flow accounts for every attempt in its evidence
            # and quota recovery continues from recorded stops; neither knows
            # about an automatic retry, so the combination is refused (#810).
            raise FlowStopped("flow.retries_unsupported",
                              "step retries are not supported in checked, dashboard-owned or "
                              "quota-recovered flows; remove retries or run the flow without them")
        if resume and not checked_resume:
            from garuda.runtime.strict_store import StorageError

            try:
                prior = await _settled_sync(self.store.load_meta, self.flow_session)
            except StorageError as exc:
                raise FlowStopped("flow.verification_reconciliation_required",
                                  "protected flow metadata is unavailable") from exc
            if prior.get("publication_policy") is not None or self.verification_request is not None:
                raise FlowStopped("flow.verification_reconciliation_required",
                                  "owned flow verification requires recovery from its durable phase; "
                                  "steps and checks are never replayed automatically")
            await _settled_sync(recover, self.store, self.flow_session)
            prior_receipts = await _settled_sync(receipts, self.store, self.flow_session)
            held = [r["step"] for r in prior_receipts
                    if r.get("status") == "quarantined"]
            if held:
                raise FlowStopped("flow.quarantined",
                                  f"{', '.join(held)} was interrupted after launch; it is never "
                                  "replayed", step=held[0])
        lease = WorkspaceLeaseGuard(self.workspace, self.flow_session, mode=self._lease_mode(),
                                    ttl_sec=self.lease_ttl,
                                    capacity_key=(self.background_admission.harness
                                                  if self.background_admission else None),
                                    queued_admission=self.background_admission)
        lease.acquire()
        lease.start_heartbeat()
        result = FlowResult(self.flow_session)
        begun = resume and not checked_resume
        prior = None
        confinement_stop = None
        try:
            if self.require_owned:
                self._owned_snapshot = (lease.snapshot_ownership() if lease._mode == "mutating"
                                        else lease.snapshot_lifetime())
                if self.operation_binding is not None:
                    self._validate_operation_workspace()
                    self.operation_binding.update({
                        "session_id": self.flow_session,
                        "lease_epoch": self._owned_snapshot["lease"]["epoch"],
                    })
            if not resume:
                await _settled_sync(self._begin)
                begun = True
                if self.on_admitted is not None:
                    await self.on_admitted(
                        self.flow_session, self.operation_binding["lease_epoch"])
                try:
                    await _settled_sync(self._check_confinement_images)
                except FlowStopped as exc:
                    if self.verification_cli_checks is None or exc.code != "workspace.readonly_unenforced":
                        raise
                    confinement_stop = exc
                # No step, slot, recovery admission or evidence baseline for a
                # flow whose review could never be independent (#753).
                await _settled_sync(self._check_review_independence)
            if checked_resume:
                prior, baseline_digest, candidate, coverage, existing = await _settled_sync(
                    self._resume_inputs, checked_checkpoint)
            if self.quota_recovery is not None:
                from garuda.flows.quota_recovery import RecoveryWaiting

                try:
                    await self.quota_recovery.admit(self)
                except RecoveryWaiting as exc:
                    raise FlowStopped("flow.quota_wait", str(exc)) from exc
                except (OSError, ValueError) as exc:
                    raise FlowStopped("flow.recovery_changed", str(exc)) from exc
            if resume and not checked_resume:
                from garuda.runtime.ownership import current_owner
                from garuda.runtime.session_state import started

                await self._publish(self.flow_session, {
                    "flow_state": "running", "status": "running", "flow_stop": None,
                    "state": started(current_owner().to_dict())})
            if self.authority_sources:
                from dataclasses import asdict

                from garuda.config_sources import SourceChanged, verify_sources

                try:
                    await lease.race(_settled_sync(
                        verify_sources,
                        [asdict(source) for source in self.authority_sources]))
                except SourceChanged as exc:
                    raise FlowStopped(
                        "flow.authority_changed",
                        "admitted starter sources changed before the first step") from exc
            self._validate_operation_workspace()
            if self.verification_request is not None:
                from garuda.core.acceptance_work import AcceptanceWorkError, owned_call
                from garuda.workspace.flow_evidence import OwnedFlowEvidence

                try:
                    await lease.race(owned_call("admitted-sources", {
                        "sources": [source.document() for source in
                                    self.verification_request.sources]}))
                except AcceptanceWorkError as exc:
                    raise FlowStopped("flow.check_authority_changed",
                                      "check sources changed before step admission") from exc
                self._evidence = OwnedFlowEvidence(
                    self.store.session_dir(self.flow_session), self.flow_session, self.workspace, lease,
                    baseline_digest=baseline_digest if checked_resume else None)
                if checked_resume:
                    try:
                        await self._evidence.resume(candidate, coverage)
                    except CleanupUncertain as exc:
                        await lease.stop_heartbeat()
                        raise FlowStopped("flow.cleanup_unproved",
                                          "the resume evidence worker did not settle") from exc
                    except AcceptanceWorkError as exc:
                        raise FlowStopped("flow.verification_reconciliation_required",
                                          "the stopped flow workspace or baseline evidence changed") from exc
                    self._evidence_count = len(coverage)
                    from garuda.runtime.ownership import current_owner
                    from garuda.runtime.session_state import started

                    await self._publish(self.flow_session, {
                        "flow_state": "running", "status": "running", "flow_stop": None,
                        "flow_resume_checkpoint": None, "state": started(current_owner().to_dict())})
                    begun = True
                else:
                    try:
                        baseline = await self._evidence.begin()
                    except AcceptanceWorkError as exc:
                        if confinement_stop is None:
                            raise
                        self._evidence = None
                        raise confinement_stop from exc
                    await self._publish(self.flow_session, {"flow_evidence_baseline": baseline})
                    if confinement_stop is not None:
                        raise confinement_stop
            if not checked_resume:
                existing = receipts(self.store, self.flow_session)
            done = [r for r in existing if r.get("status") == "done"]
            completed_steps = {r["step"] for r in done}
            tried = {}
            for r in existing:
                tried[r["step"]] = max(tried.get(r["step"], 0), r.get("attempt", 0))
            steps = self.steps
            prior_approvals = ((await _settled_sync(self.store.load_meta, self.flow_session))
                               .get("review_approvals") if resume else None)
            self._review_approvals = prior_approvals if isinstance(prior_approvals, dict) else {}
            continuation = (prior or {}).get("flow_continuation")
            if resume:
                # A resume refuses before its next step too; reviewed steps it
                # will never run again (completed, not continued) are skipped.
                resumed = {(continuation or {}).get("build"),
                           ((self.quota_recovery.continuation or {}).get("build")
                            if self.quota_recovery is not None else None)}
                await _settled_sync(self._check_review_independence,
                                    completed_steps - resumed)
            for step in steps:
                if step.get("review") and continuation and continuation.get("build") == step["id"]:
                    await self._review_loop(step, steps[-1], done, lease, tried, checkpoint=continuation)
                    completed_steps.add(steps[-1]["id"])
                    continue
                if step["id"] in completed_steps:
                    # A receipt says the step ran, not that its reviewer approved it.
                    if (step.get("review") and self.quota_recovery is not None
                            and self.quota_recovery.continuation
                            and self.quota_recovery.continuation.get("build") == step["id"]):
                        await self._review_loop(step, steps[-1], done, lease, tried)
                        completed_steps.add(steps[-1]["id"])
                        continue
                    if step.get("review") and not self._review_approved(step, steps[-1], done):
                        raise FlowStopped(
                            "flow.review_unresolved",
                            f"{step['id']} has no approved review; a resume never treats it "
                            "as reviewed, so start a new flow", step=steps[-1]["id"])
                    continue  # completed before a resume: never run again
                if "parallel" in step:
                    attempt = tried.get(step["id"], 0) + 1
                    done.append(await self._run_group(step, done, attempt=attempt))
                    continue
                if step.get("review"):
                    await self._review_loop(step, steps[-1], done, lease, tried)
                    completed_steps.add(steps[-1]["id"])
                    continue
                receipt = await self._run_with_retries(step, done, lease, tried)
                done.append(receipt)
            if self.verification_request is not None:
                try:
                    entries, attempts = await _settled_sync(self._checked_history)
                    known = {(row["step"], row["attempt"]): row for row in done} | self._prior_attempts
                    if known != {(row["step"], row["attempt"]): row for row in attempts}:
                        raise FlowStopped("flow.evidence_incomplete", "step attempt coverage is incomplete")
                    coverage = self._evidence_coverage(entries, attempts)
                    reference, manifest = await self._evidence.finalize(coverage)
                    await self._publish(self.flow_session, {
                        "flow_evidence_manifest": reference,
                        "flow_delta_changed": manifest["changed"][:200],
                        "flow_delta_changed_total": len(manifest["changed"]),
                        "flow_delta_changed_complete": len(manifest["changed"]) <= 200,
                        "flow_final_changed": manifest["final_changed"][:200],
                        "flow_final_changed_total": len(manifest["final_changed"]),
                        "flow_final_changed_complete": len(manifest["final_changed"]) <= 200})
                except FlowStopped:
                    await lease.stop_heartbeat()
                    raise
                except Exception as exc:
                    await lease.stop_heartbeat()
                    raise FlowStopped("flow.evidence_unavailable",
                                      "complete parent evidence could not be finalized") from exc
                await self._finalize_verification(lease, reference, manifest)
                result.receipts = receipts(self.store, self.flow_session)
                return result
            await self._publish(self.flow_session, {
                "flow_state": "completed", "status": "completed",
                "state": finished(success=True)})
            if self.quota_recovery is not None:
                await self.quota_recovery.completed(self)
        except FlowStopped as stop:
            if not begun:
                raise
            checkpoint = None
            refusal = None
            if (self.verification_cli_checks is not None and self._evidence is not None
                    and not self._verification_quarantined
                    and (stop.code in RESUMABLE_CHECKED_STOPS or stop.settled_refusal)):
                try:
                    checkpoint = await self._stop_checkpoint(lease)
                except FlowStopped as exc:
                    refusal = exc
            failure = refusal or stop
            result.stopped = failure
            await self._publish(self.flow_session, {
                "flow_state": "quarantined" if self._verification_quarantined or failure.code in {
                    "flow.cleanup_unproved", "flow.evidence_incomplete", "flow.evidence_unavailable"
                } else "stopped",
                "status": "failed", "state": finished(success=False),
                **({"flow_resume_checkpoint": checkpoint} if checkpoint is not None else {}),
                "flow_stop": {"code": failure.code, "step": failure.step, "message": str(failure),
                              **({"cause": failure.cause} if failure.cause else {})}})
            if refusal is not None:
                raise refusal from refusal.__cause__
            if self.quota_recovery is not None:
                await self.quota_recovery.stopped(self, stop)
        except BaseException as exc:
            # Cancelled or crashed mid-flow: the step that was running has an
            # intent and no receipt, so a resume quarantines it.
            uncertain = isinstance(exc, CleanupUncertain) or self._verification_quarantined
            if uncertain:
                await lease.stop_heartbeat()
            if not begun:
                raise
            updates = {
                "flow_state": "quarantined" if uncertain else "interrupted",
                "status": "failed",
                "state": interrupted(cancelled=isinstance(exc, asyncio.CancelledError))}
            if self._interrupted_acceptance_receipts is not None:
                updates["acceptance_receipts"] = self._interrupted_acceptance_receipts
            await self._publish(
                self.flow_session, updates,
                terminal=isinstance(exc, asyncio.CancelledError) and not uncertain)
            if self.quota_recovery is not None:
                await self.quota_recovery.interrupted(self, "flow interrupted; automatic replay refused")
            raise
        finally:
            await lease.release()
        result.receipts = receipts(self.store, self.flow_session)
        return result

    def _rerun_inputs(self):
        """Prove an interrupted check phase may be run again (#772), read only.

        Every step attempt must have settled, the original check request and
        flow definition must be unchanged, the published phase must be the
        durable one (or one a crashed rerun already archived), and it must be
        bound to the flow's committed evidence manifest.
        """
        from garuda.core.acceptance_request import is_digest
        from garuda.runtime.strict_store import CorruptRecord, StorageError

        try:
            prior = self.store.load_meta(self.flow_session)
            records = self._verification_records()
            if records.saved_request(prior) != self._resume_request():
                raise CorruptRecord("the original check authority or flow definition changed")
            if unsettled_attempts(self.store, self.flow_session):
                raise CorruptRecord("a launched step attempt never settled")
            phase, reference = records.current_phase()
            published = prior.get("flow_verification_phase")
            manifest = prior.get("flow_evidence_manifest")
            baseline = (prior.get("flow_evidence_baseline") or {}).get("baseline_digest")
            if (published != reference and not records.superseded(published)) \
                    or not isinstance(manifest, dict) \
                    or phase["evidence_digest"] != manifest.get("manifest_digest") \
                    or not is_digest(baseline) or prior.get("flow_verification_commit") is not None \
                    or prior["workspace"] != self.workspace or prior["task"] != self.task:
                raise CorruptRecord("the interrupted check phase is not bound to this flow")
            entries, attempts = self._checked_history()
            self._prior_attempts = {(row["step"], row["attempt"]): row for row in attempts}
            self._evidence_coverage(entries, attempts)
            return phase, reference, manifest, baseline
        except (StorageError, OSError, ValueError, KeyError, TypeError) as exc:
            raise FlowStopped("flow.verification_reconciliation_required",
                              "the interrupted check phase or its flow evidence could not be "
                              "proved; checks are never replayed past it") from exc

    async def rerun_checks(self) -> FlowResult:
        """Run the check phase of a flow interrupted during it again (#772).

        Steps never run again. The workspace must still be the exact candidate
        the interrupted phase checked; a fresh phase then supersedes it under a
        newly acquired lease, and only its own terminal receipts decide the
        verdict. Nothing from the interrupted phase is published as a result.
        """
        from garuda.interfaces.run_guard import WorkspaceLeaseGuard
        from garuda.runtime.ownership import current_owner
        from garuda.runtime.process_supervision import CleanupUncertain
        from garuda.runtime.session_state import finished, interrupted, started
        from garuda.workspace.flow_evidence import OwnedFlowEvidence

        phase, reference, manifest_reference, baseline = await _settled_sync(self._rerun_inputs)
        lease = WorkspaceLeaseGuard(self.workspace, self.flow_session, mode="mutating",
                                    ttl_sec=self.lease_ttl)
        lease.acquire()
        lease.start_heartbeat()
        result = FlowResult(self.flow_session)
        begun = False
        try:
            self._evidence = OwnedFlowEvidence(
                self.store.session_dir(self.flow_session), self.flow_session, self.workspace,
                lease, baseline_digest=baseline)
            try:
                manifest = await self._evidence.manifest(manifest_reference["manifest_digest"])
                candidate = await self._evidence.version()
            except CleanupUncertain as exc:
                await lease.stop_heartbeat()
                raise FlowStopped("flow.cleanup_unproved",
                                  "the rerun evidence worker did not settle") from exc
            except Exception as exc:
                raise FlowStopped("flow.verification_reconciliation_required",
                                  "the flow evidence manifest could not be read") from exc
            if candidate != phase["candidate"]:
                raise FlowStopped(
                    "flow.verification_reconciliation_required",
                    "the workspace changed after the interrupted check phase, so its checks "
                    "would not verify the reviewed candidate; start a new flow")
            await self._publish(self.flow_session, {
                "flow_state": "running", "status": "running", "flow_stop": None,
                "state": started(current_owner().to_dict())})
            begun = True
            await self._finalize_verification(lease, manifest_reference, manifest,
                                              supersedes=reference["digest"])
        except FlowStopped as stop:
            result.stopped = stop
            if begun:
                await self._publish(self.flow_session, {
                    "flow_state": "quarantined" if self._verification_quarantined
                    or stop.code == "flow.cleanup_unproved" else "interrupted",
                    "status": "failed", "state": finished(success=False),
                    "flow_stop": {"code": stop.code, "step": None, "message": str(stop)}})
            raise
        except BaseException as exc:
            uncertain = isinstance(exc, CleanupUncertain) or self._verification_quarantined
            if uncertain:
                await lease.stop_heartbeat()
            if begun:
                updates = {"flow_state": "quarantined" if uncertain else "interrupted",
                           "status": "failed",
                           "state": interrupted(cancelled=isinstance(exc, asyncio.CancelledError))}
                if self._interrupted_acceptance_receipts is not None:
                    updates["acceptance_receipts"] = self._interrupted_acceptance_receipts
                await self._publish(self.flow_session, updates,
                                    terminal=isinstance(exc, asyncio.CancelledError) and not uncertain)
            raise
        finally:
            await lease.release()
        result.receipts = receipts(self.store, self.flow_session)
        return result

    async def _run_group(self, step: dict, done: list[dict], *, attempt: int) -> dict:
        """A parallel review group on one immutable snapshot (C.8b).

        Every member reviews the same detached snapshot repository, never the
        workspace: native members run ``readonly`` and external ones must be
        ``readonly`` roles, which run only in proven Docker confinement
        (C.8a). Source and snapshot must both be unchanged afterwards.
        """
        if self.require_owned:
            raise FlowStopped("flow.parallel_verification_unavailable",
                              "parallel snapshot members have no owned closure evidence",
                              step=step["id"])
        import asyncio

        from garuda.workspace import snapshot_proto as snap
        from garuda.workspace.no_edits import NoEditsGuard

        members = step["parallel"]
        plans = {role: self._role_plan(role, step=step["id"]) for role in members}
        for role, plan in plans.items():
            if plan is not None and plan.kind == "acp" and plan.permissions != "readonly":
                raise FlowStopped("flow.parallel_reviewer_not_readonly",
                                  f"{role} is an external role without permissions: readonly; "
                                  "a parallel reviewer must be confined", step=step["id"])
        inputs = await self._inputs(step, done)
        version = await self._version_for_flow()
        target = self.dir / "snapshots" / f"{step['id']}-{attempt}"
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            snapshot = await asyncio.to_thread(snap.detached_repository, self.workspace, target)
        except snap.SnapshotRefused as exc:
            raise FlowStopped("flow.snapshot_unavailable", str(exc), step=step["id"]) from exc
        # The same refusal contract as a standalone or sequential reviewer:
        # every external member's confinement is proven before any launches.
        from garuda.workspace.confined_acp import preflight

        for role, plan in plans.items():
            if plan is not None and plan.kind == "acp":
                try:
                    await asyncio.to_thread(preflight, target,
                                            Confinement.from_config(plan.harness))
                except ConfinementRefused as exc:
                    raise FlowStopped(exc.code, f"{role}: {exc.message}", step=step["id"]) from exc
        _append(self.dir / JOURNAL, {"event": "intent", "index": step["index"],
                                     "step": step["id"], "attempt": attempt, "at": _now()})
        started = (_stamp(), time.monotonic())
        source_guard, snapshot_guard = NoEditsGuard(self.workspace), NoEditsGuard(target)
        prompt = self._prompt(step, "reviewer", inputs, read_only=True)

        async def one(role):
            return role, await self.launcher(StepLaunch(
                flow_session=self.flow_session, step_id=f"{step['id']}:{role}", role=role,
                role_plan=plans[role], prompt=prompt, workspace=str(target), capability=None,
                attempt=attempt, no_edits=True, store=self.store))

        results = await asyncio.gather(*(one(role) for role in members), return_exceptions=True)
        outcomes = []
        stop = None
        for role, item in zip(members, results, strict=True):
            if isinstance(item, BaseException):
                if isinstance(item, asyncio.CancelledError):
                    raise item
                if isinstance(item, ConfinementRefused) and stop is None:
                    stop = FlowStopped(item.code, f"{role}: {item.message}", step=step["id"])
                outcomes.append((role, StepResult("", False, f"{type(item).__name__}: {item}")))
            else:
                outcomes.append(item)
        receipt: dict[str, Any] = {
            "index": step["index"], "step": step["id"], "attempt": attempt,
            "snapshot": snapshot.commit, "inputs": [ref.to_dict() for ref, _ in inputs],
            "workspace_version_before": version, "recorded_at": _now(), "members": [],
            "outputs": [],
        }
        source, copy = source_guard.check(), snapshot_guard.check()
        receipt["source_check"], receipt["snapshot_check"] = source.record(), copy.record()
        if not (source.unchanged and copy.unchanged):
            stop = FlowStopped("flow.parallel_changed",
                               "a parallel reviewer changed the source or its snapshot",
                               step=step["id"])
        for role, outcome in outcomes:
            member = {"role": role, "session_id": outcome.session_id,
                      "success": outcome.success, "outputs": [],
                      "role_plan": plans[role].record() if plans[role] is not None else None}
            if stop is None and not outcome.success:
                cause = await _settled_sync(self._step_cause, outcome)
                stop = FlowStopped("flow.step_failed",
                                   _failed(f"reviewer {role}", outcome, cause, no_edits=True),
                                   step=step["id"], cause=cause)
            produced = {}
            if stop is None:
                try:
                    produced = art.extract(outcome.output)
                except art.ArtifactError as exc:
                    stop = FlowStopped(exc.code, f"reviewer {role}: {exc.message}",
                                       step=step["id"])
            for kind in step.get("outputs", []):
                if stop is not None:
                    break
                if kind not in produced:
                    stop = FlowStopped("flow.output_missing", f"reviewer {role} gave no {kind}",
                                       step=step["id"])
                    break
                try:
                    ref = art.store(self.dir, type=kind, content=produced[kind],
                                    step=f"{step['id']}-{role}", session_id=outcome.session_id,
                                    attempt=attempt, workspace_version=version)
                except art.ArtifactError as exc:
                    stop = FlowStopped(exc.code, f"reviewer {role}: {exc.message}",
                                       step=step["id"])
                    break
                member["outputs"].append(ref.to_dict())
            receipt["members"].append(member)
        if stop is None:
            receipt["outputs"] = [o for m in receipt["members"] for o in m["outputs"]]
        receipt["workspace_version_after"] = await self._version_for_flow()
        receipt["status"] = "done" if stop is None else "stopped"
        if stop is not None:
            receipt["stop"] = _stop_record(stop)
        receipt.update(_timing(started))
        _write_once(self.dir / "receipts" / _receipt_name(step["id"], attempt), receipt)
        _append(self.dir / JOURNAL, {"event": "receipt", "step": step["id"],
                                     "attempt": attempt, "at": _now()})
        await self._touch()
        if stop is not None:
            raise stop
        return receipt

    def _independence(self, step: dict, terminal: dict, done: list[dict], spec: dict,
                      reviewed_plan, reviewer_receipt: dict) -> dict:
        """Compare the selected or executed reviewer with recorded reviewed identities.

        Admission supplies the selected plan; approval supplies its actual receipt.
        Both retain the reviewed role's launched, consulted and configured identities.
        """
        from garuda.consult import view
        from garuda.flows import review as rv

        config = self.resolved.config
        reviewer = reviewer_receipt.get("role_plan") or {}
        mine = (reviewer.get("runtime_id"), reviewer.get("model_id"))
        attempts = [r for r in done if r.get("step") == step["id"]]
        launched = set()
        consults: list[dict] = []
        for attempt in attempts:
            plan = attempt.get("role_plan") or {}
            if plan.get("runtime_id"):
                launched.add((plan.get("runtime_id"), plan.get("model_id")))
            if attempt.get("session_id"):
                consults.extend(view.entries(self.store, attempt["session_id"]))
        consulted = view.identities(consults)
        identity_error = (None if mine[0] else
                          "cannot prove review independence: reviewer launch identity is missing")
        try:
            configured = rv.identities(config, step["role"], reviewed_plan)
        except rv.IdentityUnresolved as exc:
            configured, identity_error = set(), str(exc)
        shared = mine in configured or mine in launched or mine in consulted

        def ident(pair):
            return {"runtime": pair[0], "model_id": pair[1]}

        def ordered(pairs):
            return [ident(p) for p in sorted(pairs, key=lambda p: (str(p[0]), str(p[1])))]

        return {"policy": "required" if spec.get("independent", True) else "waived",
                "decision": "not_independent" if shared else "unknown" if identity_error else "independent",
                **({"identity_error": identity_error} if identity_error else {}),
                "reviewer": {"role": terminal["role"], **ident(mine)},
                "reviewed": {"role": step["role"], "configured": None if identity_error else ordered(configured),
                             "launched": ordered(launched), "consulted": ordered(consulted)},
                "consults": len(consults)}

    def _review_approved(self, step: dict, terminal: dict, done: list[dict]) -> bool:
        """A resume needs approval for this exact build attempt and candidate."""
        approval = self._review_approvals.get(step["id"])
        if not isinstance(approval, dict) or approval.get("policy") != step["review"]:
            return False
        build = next((r for r in reversed(done) if r.get("step") == step["id"]), None)
        if build is None:
            return False
        candidate = {"step": step["id"], "session_id": build.get("session_id"),
                     "attempt": build.get("attempt"),
                     "workspace_version": build.get("workspace_version_after"),
                     "workspace_version_before": build.get("workspace_version_before")}
        if approval.get("candidate") != candidate:
            return False
        reviewer = approval.get("reviewer")
        if not isinstance(reviewer, dict) or reviewer.get("step") != terminal["id"]:
            return False
        receipt = next((r for r in done if all(r.get(key) == reviewer.get(key)
                       for key in ("step", "attempt", "session_id"))), None)
        if receipt is None or receipt.get("workspace_version_after") != candidate["workspace_version"]:
            return False
        from garuda.core.acceptance_request import is_digest

        digest = approval.get("packet_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:") or not is_digest(digest[7:]):
            return False
        history = approval.get("history")
        return (approval.get("status") == "review_approved" and isinstance(history, list)
                and bool(history) and isinstance(history[-1], dict)
                and history[-1].get("approved") is True
                and history[-1].get("packet_digest") == approval.get("packet_digest"))

    async def _review_loop(self, step: dict, terminal: dict, done: list[dict], lease,
                           tried: dict, *, checkpoint=None) -> None:
        """Run ``step`` and its terminal reviewer until approval or the rounds run out."""
        from garuda.context.redact import redact_text
        from garuda.flows import review as rv

        spec = step["review"]
        rounds = spec.get("max_rounds", 1)
        reviewed_plan, reviewer_plan = self._review_plans(step, terminal)
        extra = ""
        history = []
        if self.quota_recovery is not None:
            checkpoint = self.quota_recovery.continuation
        start = 0
        pending = step["id"]
        if checkpoint:
            if (checkpoint.get("version") != 1 or checkpoint.get("build") != step["id"]
                    or checkpoint.get("reviewer") != terminal["id"]
                    or type(checkpoint.get("round")) is not int
                    or not 0 <= checkpoint["round"] <= rounds
                    or checkpoint.get("pending") not in {step["id"], terminal["id"]}
                    or len(checkpoint.get("history", [])) != checkpoint["round"]):
                raise FlowStopped("flow.recovery_checkpoint_invalid", "invalid review continuation")
            start, pending = checkpoint["round"], checkpoint["pending"]
            history, extra = checkpoint["history"], prompting.extra_from(checkpoint)

        async def admit_reviewer(plan):
            independence = self._independence(
                step, terminal, done, spec, reviewed_plan,
                {"role_plan": plan.record() if plan is not None else None})
            if independence["policy"] == "required" and independence["decision"] != "independent":
                await self._publish(self.flow_session, {"review": {
                    "status": "review_not_independent", "rounds": len(history), "history": history,
                    "independence": independence}})
                why = independence.get("identity_error") or (
                    "the selected reviewer shares an identity with what actually ran or "
                    "was consulted for the reviewed step")
                raise FlowStopped("flow.review_not_independent", why, step=terminal["id"])

        for pair in range(start, rounds + 1):
            for target in (step, terminal):
                if pair == start and pending == terminal["id"] and target is step:
                    continue
                if self.quota_recovery is not None or self.verification_cli_checks is not None:
                    await self._publish(self.flow_session, {"flow_continuation": {
                        "version": 1, "build": step["id"], "reviewer": terminal["id"],
                        "round": pair, "pending": target["id"], "history": history,
                        **prompting.extra_record(extra)}})
                packet = None
                if target is terminal:
                    packet = await self._review_packet(step, terminal, done, spec, reviewer_plan)
                tried[target["id"]] = tried.get(target["id"], 0) + 1
                receipt = await self._run_step(
                    target, done, lease, attempt=tried[target["id"]],
                    prompt_extra=extra if target is step else prompting.Packet(packet),
                    review_admission=admit_reviewer if target is terminal else None)
                done.append(receipt)
            ref = next(art.ArtifactRef.from_dict(o) for o in receipt["outputs"]
                       if o["type"] == "review")
            try:
                parsed = rv.parse(art.load(self.dir, ref,
                                           workspace_version=await self._version_for_flow()))
            except (rv.ReviewInvalid, art.ArtifactError) as exc:
                raise FlowStopped("flow.review_invalid", str(exc), step=terminal["id"]) from exc
            candidate = packet["candidate"]["workspace_version"]
            if candidate is not None and receipt.get("workspace_version_after") != candidate:
                # The reviewer saw a candidate that no longer exists: never an approval.
                raise FlowStopped("flow.review_stale_candidate",
                                  "the workspace changed while the review ran; the review does "
                                  "not describe the current candidate", step=terminal["id"])
            history.append({"round": pair + 1, "verdict": parsed.verdict,
                            "approved": parsed.approved, "findings": parsed.findings,
                            "packet_digest": packet["digest"],
                            **({"structured": parsed.structured}
                               if parsed.structured is not None else {})})
            independence = self._independence(step, terminal, done, spec, reviewed_plan,
                                              receipt)
            if independence["policy"] == "required" and independence["decision"] != "independent":
                # an identity only visible after the fact (a fallback or consult that ran)
                await self._publish(self.flow_session, {"review": {
                    "status": "review_not_independent", "rounds": pair + 1, "history": history,
                    "independence": independence}})
                why = independence.get("identity_error") or (
                    "the reviewer shares an identity with what actually ran or "
                    "was consulted for the reviewed step")
                raise FlowStopped("flow.review_not_independent", why, step=terminal["id"])
            if parsed.approved:
                review = {"status": "review_approved", "rounds": pair + 1, "history": history,
                          "independence": independence}
                self._review_approvals[step["id"]] = {
                    **review, "candidate": packet["candidate"], "packet_digest": packet["digest"],
                    "policy": dict(spec), "reviewer": {
                        "step": terminal["id"], "session_id": receipt["session_id"],
                        "attempt": receipt["attempt"]}}
                await self._publish(self.flow_session, {
                    "review": review, "review_approvals": dict(self._review_approvals)})
                if self.quota_recovery is not None:
                    self.quota_recovery.continuation = None
                if self.quota_recovery is not None or self.verification_cli_checks is not None:
                    await self._publish(self.flow_session, {"flow_continuation": None})
                return
            # The review artifact was redacted when stored; redact again so no
            # other path can carry a reviewer's secret to the reviewed step (#812).
            findings = redact_text(parsed.findings_text())[0]
            extra = prompting.Findings(findings)
        await self._publish(self.flow_session, {"review": {
            "status": "review_changes_requested", "rounds": rounds + 1, "history": history,
            "independence": independence}})
        raise FlowStopped("flow.review_changes_requested",
                          f"the reviewer still requests changes after {rounds + 1} rounds",
                          step=terminal["id"])

    def _first_attempt(self, step_id: str, done: list[dict]) -> dict | None:
        """The reviewed step's first attempt in this flow that could have edited.

        Read from the flow's own receipts, so stopped attempts and those loaded
        on resume count (#814); an attempt refused before launch never ran and
        is skipped. None when an earlier attempt has no readable receipt or no
        session record: its edits could not be attributed.
        """
        try:
            rows = {r.get("attempt"): r for r in receipts(self.store, self.flow_session)
                    if r.get("step") == step_id}
        except (OSError, ValueError):
            return None
        rows.update({r.get("attempt"): r for r in done if r.get("step") == step_id})
        attempt = 1
        while attempt in rows:
            row = rows[attempt]
            if not (row.get("status") == "stopped" and row.get("session_id") is None
                    and "started_at" in row and row["started_at"] is None):
                return row if self._first_session(row) else None
            attempt += 1
        return None

    @staticmethod
    def _first_session(receipt: dict | None) -> str | None:
        """The first harness session an attempt ran: its baseline is the attempt's start.

        A review packet compares the candidate with the baseline of the first
        session of the reviewed step's first attempt (#814). Today an attempt
        runs one session, its receipt's ``session_id``; an attempt that runs
        several sessions in turn would name its first one here.
        """
        if receipt is None:
            return None
        session = receipt.get("session_id")
        return session if isinstance(session, str) and session else None

    async def _review_packet(self, step: dict, terminal: dict, done: list[dict], spec: dict,
                             reviewer_plan) -> dict:
        """Freeze what the reviewer is shown, from the reviewed step's own records.

        Refuses when the workspace no longer is the candidate the reviewed step
        produced; unknown identities and manifests stay explicitly unknown. A
        candidate version that is unknown, or cannot tell workspaces apart,
        leaves the binding unenforced, which a review that must be independent
        refuses.
        """
        from garuda.flows import review as rv
        from garuda.flows.review_evidence import config_evidence
        from garuda.workspace.evidence import changed_since_baseline, review_diff_evidence

        reviewed = next((r for r in reversed(done) if r.get("step") == step["id"]), None)
        if reviewed is None:
            raise FlowStopped("flow.review_packet", "no reviewed step receipt to review",
                              step=terminal["id"])
        candidate = reviewed.get("workspace_version_after")
        current = await self._version_for_flow()
        if candidate is not None and current != candidate:
            raise FlowStopped("flow.review_stale_candidate",
                              "the workspace changed after the reviewed step; its candidate is "
                              "no longer what would be reviewed", step=terminal["id"])
        # The candidate is everything the reviewed step changed in this flow:
        # every attempt since the first one's baseline, not one round (#814).
        first = await _settled_sync(self._first_attempt, step["id"], done)
        base = self._first_session(first)
        if base is not None and first is reviewed:
            try:
                meta = self.store.load_meta(base)
            except (OSError, ValueError):
                meta = {}
            changed = meta.get("delta_changed")
            changed = changed if isinstance(changed, list) else None
            recorded_total = meta.get("delta_changed_total")
        else:
            changed = await _settled_sync(changed_since_baseline, self.store, base, self.workspace)
            recorded_total = None if changed is None else len(changed)
        changed_total = None if changed is None else rv.change_list_total(changed, recorded_total)
        changed_complete = rv.change_list_complete(changed, recorded_total)
        packet = rv.build_packet(
            task=self.task, step=step, reviewer=terminal, spec=spec,
            candidate={"step": step["id"], "session_id": reviewed.get("session_id"),
                       "attempt": reviewed.get("attempt"), "workspace_version": candidate,
                       "workspace_version_before": reviewed.get("workspace_version_before")},
            changed=changed,
            changed_total=recorded_total,
            attempts={"first": None if first is None else {
                          "attempt": first.get("attempt"), "session_id": first.get("session_id")},
                      "latest": {"attempt": reviewed.get("attempt"),
                                 "session_id": reviewed.get("session_id")},
                      "baseline_session": base},
            version_binds=candidate is not None and self._version_binds(),
            reviewer_plan=reviewer_plan.record() if reviewer_plan is not None else None,
            no_edits=self._no_edits(terminal, terminal["role"]),
            config=await _settled_sync(
                config_evidence, self.workspace, changed, changed_complete=changed_complete),
            diff=await _settled_sync(
                review_diff_evidence, self.store, base, self.workspace,
                changed, changed_total=changed_total, changed_complete=changed_complete))
        await self._publish(self.flow_session, {"review_packet": {
            "digest": packet["digest"], "candidate": packet["candidate"],
            "binding": packet["binding"], "changed_total": packet["changed"]["total"],
            "changed_complete": packet["changed"]["complete"]}})
        if not packet["binding"]["enforced"] and spec.get("independent", True):
            # Without a candidate version a review cannot be shown to describe
            # what it reviewed; an independent review must not rest on that.
            raise FlowStopped("flow.review_candidate_unknown",
                              "the reviewed step's workspace version is unknown, so the review "
                              "cannot be bound to its candidate", step=terminal["id"])
        return packet

    async def _run_with_retries(self, step: dict, done: list[dict], lease, tried: dict) -> dict:
        """Run a plain step, again after a retryable stop, up to ``retries`` (#810).

        Attempts are numbered from the recorded receipts, and the budget is the
        step's total attempts: a resume after the retries ran out runs the step
        once more and never retries it again.
        """
        attempt = tried.get(step["id"], 0) + 1
        extra = ""
        while True:
            try:
                return await self._run_step(step, done, lease, attempt=attempt, prompt_extra=extra)
            except FlowStopped as stop:
                # Only an attempt that launched and settled has a receipt; an
                # input error before launch is never retried.
                launched = (self.dir / "receipts" / _receipt_name(step["id"], attempt)).exists()
                if (stop.code not in RETRYABLE_STOPS or stop.settled_refusal or not launched
                        or attempt > step.get("retries", 0)):
                    raise
            tried[step["id"]] = attempt
            attempt += 1
            extra = RETRY_NOTE

    async def _record_step_receipt(self, receipt: dict) -> None:
        step, attempt = receipt["step"], receipt["attempt"]
        await _settled_sync(_write_once, self.dir / "receipts" / _receipt_name(step, attempt), receipt)
        await _settled_sync(_append, self.dir / JOURNAL,
                            {"event": "receipt", "step": step, "attempt": attempt, "at": _now()})
        await self._touch()

    async def _run_step(self, step: dict, done: list[dict], lease, *, attempt: int = 1,
                        prompt_extra: str | prompting.Findings | prompting.Packet = "",
                        review_admission=None) -> dict:
        from garuda.runtime.owned_role import StepInterrupted
        from garuda.runtime.roles import RoleRefused
        from garuda.workspace.no_edits import NoEditsGuard

        self._validate_operation_workspace()
        if self.quota_recovery is not None:
            try:
                await self.quota_recovery.validate(self)
            except (OSError, ValueError) as exc:
                raise FlowStopped("flow.recovery_changed", str(exc), step=step["id"]) from exc
        role = step["role"]
        no_edits = self._no_edits(step, role)
        plan = self._role_plan(role, step=step["id"], no_edits=no_edits)
        inputs = await self._inputs(step, done)
        version_before = await self._version_for_flow()
        prompt = self._prompt(step, role, inputs, extra=prompt_extra)
        if self.quota_recovery is not None and self.quota_recovery.record is not None:
            prompt += ("\n\n[garuda] Continue from the retained workflow and current workspace after "
                       "a quota pause. Preserve partial edits and inspect existing work before "
                       "acting; completed steps are not replayed. This is saved workflow context, "
                       "not restoration of hidden model state.")
        guard = await _settled_sync(NoEditsGuard, self.workspace) if no_edits else None
        capability = lease.delegate(f"{self.flow_session}:{step['id']}:{attempt}")
        started: tuple[str, float] | None = None
        interrupted: StepInterrupted | None = None
        try:
            if role in self.prepared_fallbacks:
                from garuda.agents.fallback_probe import select_owned

                async def publish(record):
                    self._fallback_probes[f"{step['id']}:{attempt}"] = record
                    await self._publish(self.flow_session, {"fallback_probes": dict(self._fallback_probes)})

                plan = await select_owned(self.prepared_fallbacks[role], self.runtime_catalog,
                                          self.workspace, lease, no_edits=no_edits, publish=publish)
                self.prepared_roles[role] = plan
            self._check_confinement_image(step, plan, no_edits=no_edits)
            if review_admission is not None:
                await review_admission(plan)
            if self.quota_recovery is not None:
                from garuda.flows.quota_recovery import RecoveryWaiting

                try:
                    await self.quota_recovery.before_launch(self, plan)
                except RecoveryWaiting as exc:
                    raise FlowStopped("flow.quota_wait", str(exc), step=step["id"]) from exc
                except (OSError, ValueError) as exc:
                    raise FlowStopped("flow.recovery_changed", str(exc), step=step["id"]) from exc
            await _settled_sync(_append, self.dir / JOURNAL,
                                {"event": "intent", "index": step["index"],
                                 "step": step["id"], "attempt": attempt, "at": _now()})
            started = (_stamp(), time.monotonic())
            outcome = await self.launcher(StepLaunch(
                flow_session=self.flow_session, step_id=step["id"], role=role, role_plan=plan,
                prompt=prompt, workspace=self.workspace, capability=capability,
                attempt=attempt, no_edits=no_edits, store=self.store,
                require_owned=self.require_owned, runtime_catalog=self.runtime_catalog,
                approval_handler=self.approval_handler,
                workspace_identity=(self.execution_workspace or self.operation_binding or {}).get(
                    "identity" if self.execution_workspace else "workspace_identity"),
                authority_workspace=(self.execution_workspace or {}).get("source"),
                worktree_plan=self.worktree_plan, authority_sources=self.configuration_sources,
                external_approvals=self.background_admission is not None,
                quota_recovery=self.quota_recovery is not None,
                codex_executable=(self.quota_recovery.admission.get("codex_executable")
                                  if self.quota_recovery and plan and plan.runtime_id == "codex"
                                  else None),
                quota_guard=((lambda: self.quota_recovery.before_launch(self, plan))
                             if self.quota_recovery and plan and plan.runtime_id == "codex"
                             else None)))
        except ConfinementRefused as exc:
            stop = FlowStopped(exc.code, exc.message, step=step["id"])
            # This refusal precedes agent start. Settle the intent so recovery
            # can distinguish it from an interrupted launch.
            await self._record_step_receipt({
                "index": step["index"], "step": step["id"], "role": role, "attempt": attempt,
                "status": "stopped", "success": False, "session_id": None,
                "inputs": [ref.to_dict() for ref, _ in inputs], "outputs": [],
                "workspace_version_before": version_before, "recorded_at": _now(),
                "role_plan": plan.record() if plan is not None else None,
                "stop": {"code": stop.code, "message": str(stop)},
                **_timing(started),
            })
            raise stop from exc
        except RoleRefused as exc:
            raise FlowStopped(exc.code, str(exc), step=step["id"]) from exc
        except StepInterrupted as exc:
            # The owned worker tree and its confinement closed under a signal
            # (#760). Only a checked CLI flow settles that attempt as a
            # resumable stop; any other caller keeps the cancellation.
            if not (self.require_owned and self.verification_cli_checks is not None
                    and self._evidence is not None):
                raise
            interrupted = exc
            outcome = exc.result
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()  # settled here; nothing else is cancelled
        finally:
            lease.revoke(capability)
        closure = None
        if self.quota_recovery is not None:
            closure = outcome.closure
        boundary = None
        if self.require_owned:
            from garuda.runtime.process_supervision import CleanupUncertain

            try:
                closure = self._closed_step(outcome, step, attempt, plan.record() if plan else None,
                                            interrupted=interrupted is not None)
            except FlowStopped:
                await lease.stop_heartbeat()
                raise
            if self._evidence is not None:
                try:
                    boundary = await self._evidence.boundary(self._evidence_count, step["id"], attempt)
                    self._evidence_count += 1
                except CleanupUncertain:
                    await lease.stop_heartbeat()
                    raise
                except Exception as exc:
                    # The cleanup is proved, but evidence publication is not. No
                    # later candidate may be verified from this attempt.
                    await lease.stop_heartbeat()
                    raise FlowStopped("flow.evidence_unavailable",
                                      "complete attempt evidence could not be published",
                                      step=step["id"]) from exc
        try:
            await self._publish(outcome.session_id, {"flow_step": {
                "flow_session": self.flow_session, "step": step["id"], "attempt": attempt}})
        except Exception:
            pass  # a launcher without a session record (tests) has nothing to mark
        if interrupted is not None:
            await _settled_sync(_settle_interrupted_step, self.store, outcome.session_id)
        receipt: dict[str, Any] = {
            "index": step["index"], "step": step["id"], "role": role, "attempt": attempt,
            "session_id": outcome.session_id, "success": outcome.success,
            "inputs": [ref.to_dict() for ref, _ in inputs],
            "workspace_version_before": version_before, "recorded_at": _now(), "outputs": [],
            "role_plan": plan.record() if plan is not None else None,
            **({"closure": closure} if closure is not None else {}),
            **({"evidence_boundary": boundary} if boundary is not None else {}),
        }
        stop: FlowStopped | None = None
        if interrupted is not None:
            stop = FlowStopped(INTERRUPTED_STOP,
                               f"step {step['id']} was interrupted (SIGTERM or Ctrl-C) after its "
                               "worker and every descendant were proved closed; `garuda flow resume` "
                               "runs it again under the original checks", step=step["id"])
        if guard is not None:
            check = await _settled_sync(guard.check)
            receipt["no_edits"] = check.record()
            if not check.unchanged:
                stop = FlowStopped("flow.no_edits_changed", check.summary(), step=step["id"])
        version_after = await self._version_for_flow()
        receipt["workspace_version_after"] = version_after
        if stop is None and outcome.refusal is not None and self.require_owned:
            # The owned worker refused the role (logged-out harness, model not
            # offered, ...): the attempt closed and is bounded like any failed
            # step, so it stops with the role's own code (#738).
            stop = FlowStopped(outcome.refusal["code"], outcome.refusal["message"], step=step["id"])
            stop.settled_refusal = True
        if stop is None and not outcome.success:
            # The step session's own cause is kept for checked (owned worker) and
            # unchecked launches alike; the stop code is unchanged (#749).
            cause = await _settled_sync(self._step_cause, outcome)
            stop = FlowStopped("flow.quota_wait" if outcome.quota_wait else "flow.step_failed",
                               _failed(f"step {step['id']}", outcome, cause,
                                       no_edits=self._read_only(step, role)),
                               step=step["id"], cause=cause)
        if stop is None:
            try:
                produced = art.extract(outcome.output)
            except art.ArtifactError as exc:
                stop = FlowStopped(exc.code, exc.message, step=step["id"])
        if stop is None:
            for kind in step.get("outputs", []):
                if kind not in produced:
                    stop = FlowStopped("flow.output_missing",
                                       f"step {step['id']} gave no {kind} block", step=step["id"])
                    break
                try:
                    ref = art.store(self.dir, type=kind, content=produced[kind], step=step["id"],
                                    session_id=outcome.session_id, attempt=attempt,
                                    workspace_version=version_after)
                except art.ArtifactError as exc:
                    stop = FlowStopped(exc.code, str(exc), step=step["id"])
                    break
                receipt["outputs"].append(ref.to_dict())
        receipt["status"] = "done" if stop is None else "stopped"
        if stop is not None:
            receipt["stop"] = _stop_record(stop, **({"refused": True} if stop.settled_refusal else {}))
            receipt["outputs"] = [] if receipt.get("no_edits", {}).get("result") == "changed" \
                else receipt["outputs"]
        receipt.update(_timing(started))
        await self._record_step_receipt(receipt)
        if stop is not None:
            raise stop
        return receipt
