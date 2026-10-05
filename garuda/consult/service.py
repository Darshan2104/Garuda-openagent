"""The consult engine (plan task G.2, #170).

``consult(target, question)`` runs a bounded question-and-answer **child session** on
another role, against a read-only snapshot of the asker's work, and returns the answer as
labelled data. It is not a connection to a live session: it cannot steer, stop, approve for
or control anyone.

Order, each step before the next and all of it inside one wall-clock deadline that runs from
admission through cleanup:

1. **Authorize and bound** — the asker is an authenticated session of this root task (never
   itself a consult); the target is granted to the asker's role *in the user's file* (a
   project can only narrow); question and brief fit the limits.
2. **Reserve atomically** — the root's count and a durable request id binding the payload
   digest (``state.py``): a replay returns the same result, a changed payload refuses, a second
   concurrent consult is ``consult.busy``.
3. **Resolve** the exact target (including fallback) and take a slot from the shared capacity
   store *immediately* — a full harness is ``consult.capacity_unavailable``, never a wait on a
   slot the parent or its flow holds.
4. **Quiesce and snapshot** — no live process of the asker's, then a bounded, stable snapshot
   into an independent repository (own Git metadata); the asker then continues independently.
5. **Launch** the child: a native read-only profile (file and search tools only) or the
   Docker-confined external runtime — an external child never runs on the host.
6. **Enforce** the deadline; on timeout or cancellation the child is closed and reaped
   *before* anything is released (a child that cannot be reaped is quarantined and keeps its
   slot).
7. **Validate** that the snapshot did not change, **persist** the text-free receipt and
   result, and only then release capacity and remove the scratch snapshot.

A consult never sets task verification to passed: its answer is advice.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from garuda.consult import envelope
from garuda.consult.errors import ConsultRefused
from garuda.consult.limits import Limits, from_config
from garuda.consult.state import ConsultState

REAP_GRACE = 15.0


@dataclass
class ConsultRequest:
    asker_session: str
    root_session: str
    target: str
    question: str
    brief: str = ""
    request_id: str = ""
    source_turn: int | None = None
    workspace: str = "."


@dataclass
class ChildRequest:
    prompt: str
    workspace: str
    plan: object
    limits: Limits
    deadline_sec: float
    child_id: str
    asker_session: str
    root_session: str
    request_id: str
    store: object
    source_workspace: str = "."


@dataclass
class ChildOutcome:
    session_id: str
    success: bool
    answer: str = ""
    denied_operations: int = 0


@dataclass
class ConsultResult:
    outcome: str  # answered | refused
    text: str = ""
    code: str | None = None
    message: str = ""
    receipt: dict = field(default_factory=dict)
    replay: bool = False

    def tool_text(self) -> str:
        return self.text if self.outcome == "answered" else f"{self.code}: {self.message}"


def payload_digest(target: str, question: str, brief: str) -> str:
    return hashlib.sha256(json.dumps([target, question, brief]).encode()).hexdigest()


class ConsultService:
    """Consults for one resolved configuration and session store."""

    def __init__(self, store, resolved, *, runner=None, catalog=None, quiesce=None,
                 clock=time.time, state_factory=None):
        self.store = store
        self.resolved = resolved
        self._runner = runner
        self._catalog = catalog
        self._quiesce = quiesce
        self._clock = clock
        self._state = state_factory or (lambda root: ConsultState(store, root))
        self.limits = from_config(resolved.config if resolved else None)

    # --- authorization --------------------------------------------------------------

    def _asker_role(self, asker_session: str) -> tuple[str, dict]:
        try:
            meta = self.store.load_meta(asker_session)
        except (OSError, ValueError, FileNotFoundError) as exc:
            raise ConsultRefused("consult.invalid", "the asking session is unknown") from exc
        role = meta.get("role") if isinstance(meta.get("role"), dict) else {}
        return role.get("name") or "", meta

    def authorize(self, req: ConsultRequest) -> tuple[str, dict]:
        role, meta = self._asker_role(req.asker_session)
        if meta.get("origin") == "consult":
            raise ConsultRefused("consult.nested", "a consulted child cannot consult")
        owner_root = (meta.get("flow_step") or {}).get("flow_session") or req.asker_session
        if req.root_session not in (req.asker_session, owner_root):
            raise ConsultRefused("consult.invalid", "the asker does not belong to that task")
        roles = (self.resolved.config.get("roles", {}) if self.resolved else {})
        granted = (roles.get(role) or {}).get("consult", []) if role else []
        if req.target not in granted:
            raise ConsultRefused("consult.not_granted",
                                 f"role {role or '(none)'} may not consult {req.target!r}")
        if req.target not in roles or req.target == role:
            raise ConsultRefused("consult.target_unavailable", f"no consultable role {req.target!r}")
        question = (req.question or "").strip()
        if not question:
            raise ConsultRefused("consult.invalid", "the question is empty")
        if len(question) > self.limits.max_question_chars:
            raise ConsultRefused("consult.too_long", f"a question is at most "
                                 f"{self.limits.max_question_chars} characters")
        if len(req.brief or "") > self.limits.max_brief_chars:
            raise ConsultRefused("consult.too_long", f"a brief is at most "
                                 f"{self.limits.max_brief_tokens} tokens")
        return role, meta

    # --- the consult --------------------------------------------------------------------

    async def consult(self, req: ConsultRequest, *, quiesce=None) -> ConsultResult:
        started = self._clock()
        role, _meta = self.authorize(req)
        request_id = req.request_id or uuid.uuid4().hex
        child_id = str(uuid.uuid4())
        state = self._state(req.root_session)
        digest = payload_digest(req.target, req.question, req.brief or "")
        admission = state.admit(request_id, digest, child_id=child_id,
                                limit=self.limits.max_per_session, asker=req.asker_session,
                                target=req.target)
        if admission.kind == "replay":
            return self._replayed(admission.request)
        if admission.kind == "pending":
            raise ConsultRefused("consult.busy", "that request is already in progress")

        held = _Held()
        try:
            held.quiesce = quiesce or self._quiesce
            result = await self._run(req, role, request_id, child_id, state, started, held)
            self._release(held)  # the receipt and result are already persisted
            return result
        except ConsultRefused as exc:
            await self._settle_failure(req, role, request_id, child_id, state, started, held, exc)
            raise
        except asyncio.CancelledError:
            await self._settle_failure(req, role, request_id, child_id, state, started, held,
                                       ConsultRefused("consult.cancelled", "the asker was cancelled"))
            raise

    def _replayed(self, request: dict) -> ConsultResult:
        result = request.get("result") or {}
        if request["state"] == "interrupted":
            raise ConsultRefused("consult.interrupted", "that consult was interrupted and is "
                                 "not sent again")
        return ConsultResult(outcome=result.get("outcome", "refused"), text=result.get("text", ""),
                             code=result.get("code"), message=result.get("message", ""),
                             receipt=result.get("receipt", {}), replay=True)

    async def _run(self, req, role, request_id, child_id, state, started, held) -> ConsultResult:
        from garuda.runtime.capacity import CapacityStore, CapacityUnavailable, configured_ceiling

        deadline = started + self.limits.timeout_sec
        plan = self._resolve_target(req.target, req.workspace)
        held.plan = plan
        # 3. capacity, immediately and without waiting
        ceiling = configured_ceiling(plan.runtime_id)
        if ceiling is not None:
            capacity = CapacityStore()
            try:
                held.reservation = capacity.reserve(plan.runtime_id, child_id, ceiling)
                held.capacity = capacity
            except CapacityUnavailable as exc:
                raise ConsultRefused("consult.capacity_unavailable", str(exc)) from exc
        # 4. quiesce, snapshot
        if held.quiesce is not None:
            paused = held.quiesce()
            if inspect.isawaitable(paused):
                await paused
        scratch = Path(self.store.root) / ".consult" / req.root_session / "scratch" / child_id
        held.scratch = scratch
        snapshot = self._snapshot(req.workspace, scratch / "snapshot")
        self._check_isolation(plan, scratch / "snapshot")
        from garuda.workspace.no_edits import NoEditsGuard

        guard = NoEditsGuard(scratch / "snapshot")
        prompt = envelope.question_prompt(req.question, req.brief or "", asker=role or "asker",
                                          target=req.target)
        child = ChildRequest(
            prompt=prompt, workspace=str(scratch / "snapshot"), plan=plan, limits=self.limits,
            deadline_sec=max(1.0, deadline - self._clock()), child_id=child_id,
            asker_session=req.asker_session, root_session=req.root_session,
            request_id=request_id, store=self.store, source_workspace=req.workspace)
        # 5-6. dispatch is recorded first; from here a failure counts
        state.mark_dispatched(request_id)
        held.dispatched = True
        outcome = await self._launch(child, deadline, held)
        # 7. validate, persist, then release
        check = guard.check()
        elapsed_ms = round((self._clock() - started) * 1000)
        receipt = self._receipt(req, role, request_id, child_id, plan, snapshot, outcome, check,
                                elapsed_ms)
        if not check.unchanged:
            receipt["outcome"] = "withheld"
            receipt["diagnostic"] = "consult.unexpected_changes"
            result = {"outcome": "refused", "code": "consult.unexpected_changes",
                      "message": "the snapshot or its metadata changed or could not be compared; "
                                 "the answer is withheld", "receipt": receipt}
        elif not outcome.success or not outcome.answer.strip():
            receipt["outcome"] = "failed"
            receipt["diagnostic"] = "consult.failed"
            result = {"outcome": "refused", "code": "consult.failed",
                      "message": "the consulted role did not produce an answer", "receipt": receipt}
        else:
            text = envelope.advice(outcome.answer, target=req.target,
                                   limit=self.limits.max_answer_chars)
            receipt["answer_chars"] = min(len(outcome.answer), self.limits.max_answer_chars)
            receipt["outcome"] = "answered"
            result = {"outcome": "answered", "text": text, "receipt": receipt}
        self._write_receipt(req.root_session, request_id, receipt)
        state.finish(request_id, result)  # persisted before anything is released
        return ConsultResult(outcome=result["outcome"], text=result.get("text", ""),
                             code=result.get("code"), message=result.get("message", ""),
                             receipt=receipt)

    # --- pieces ---------------------------------------------------------------------------

    def _resolve_target(self, target: str, workspace: str = "."):
        from garuda.agents import role_agent
        from garuda.agents.fallbacks import choose
        from garuda.agents.setup import prepare_runtime_catalog
        from garuda.config.garuda_yaml import Resolved
        from garuda.runtime.roles import RoleRefused, plan_role

        resolved = Resolved(config=self.resolved.config, provenance=self.resolved.provenance,
                            role=target)
        try:
            catalog = self._catalog or prepare_runtime_catalog(".")
            plan = plan_role(resolved, catalog)
            if plan is None:
                raise ConsultRefused("consult.target_unavailable", "no such role")
            return role_agent.bind(choose(plan, resolved, catalog), workspace)
        except RoleRefused as exc:
            raise ConsultRefused("consult.target_unavailable", str(exc)) from exc

    def _check_isolation(self, plan, snapshot: Path) -> None:
        """An external child runs only in the proven Docker read-only confinement. Checked
        before anything is dispatched, so a refusal here costs nothing."""
        if getattr(plan, "kind", "native") != "acp":
            return
        from garuda.workspace.confined_acp import Confinement, ConfinementRefused, preflight

        try:
            preflight(snapshot, Confinement.from_config(plan.harness))
        except ConfinementRefused as exc:
            raise ConsultRefused("consult.isolation_unavailable", str(exc)) from exc

    def _snapshot(self, workspace: str, dest: Path):
        from garuda.workspace import snapshot_proto as snap

        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            return snap.detached_repository(workspace, dest)
        except snap.SnapshotRefused as exc:
            code = ("consult.snapshot_unstable" if exc.code == "snapshot.changed"
                    else "consult.snapshot_unsupported")
            raise ConsultRefused(code, str(exc)) from exc

    async def _launch(self, child: ChildRequest, deadline: float, held: _Held) -> ChildOutcome:
        runner = self._runner or default_runner
        task = asyncio.ensure_future(runner(child))
        held.task = task
        remaining = max(0.0, deadline - self._clock())
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.TimeoutError:
            await self._reap(task, held)
            raise ConsultRefused("consult.timeout", f"no answer within {self.limits.timeout_sec}s") \
                from None
        except asyncio.CancelledError:
            await self._reap(task, held)
            raise
        except ConsultRefused:
            raise
        except Exception as exc:  # the child crashed: it is no longer running, and it counted
            raise ConsultRefused("consult.failed",
                                 f"the consulted role failed ({type(exc).__name__})") from exc

    async def _reap(self, task: asyncio.Future, held: _Held) -> None:
        """Close the child and wait for it to be gone; if it will not go, quarantine."""
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=REAP_GRACE)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            held.quarantined = True
        except Exception:  # the child failed while closing: it is no longer running
            pass
        if not task.done():
            held.quarantined = True

    def _receipt(self, req, role, request_id, child_id, plan, snapshot, outcome, check,
                 elapsed_ms) -> dict:
        return {
            "version": 1, "request_id": request_id, "asker_session": req.asker_session,
            "root_session": req.root_session, "child_session": outcome.session_id or child_id,
            "source_turn": req.source_turn, "asker_role": role,
            "role": req.target,
            "identity": {"runtime": plan.runtime_id, "kind": plan.kind, "model_id": plan.model_id,
                         "effort": plan.effort, "fallback": plan.fallback},
            "snapshot": {"tree": snapshot.tree, "commit": snapshot.commit,
                         "manifest_digest": snapshot.manifest_digest,
                         "files": len(snapshot.entries), "deleted": len(snapshot.deleted)},
            "admission": "accepted", "policy": self.limits.to_dict(),
            "outcome": None, "diagnostic": None, "answer_chars": 0, "elapsed_ms": elapsed_ms,
            "denied_operations": outcome.denied_operations,
            "observed_changes": {"unchanged": check.unchanged, "changed": len(check.changed)},
            "usage_refs": [outcome.session_id or child_id],
        }

    def _write_receipt(self, root: str, request_id: str, receipt: dict) -> None:
        from garuda.runtime import strict_store as ss

        directory = Path(self.store.root) / ".consult" / root / "receipts"
        ss.ensure_private_dir(directory)
        ss.write_document(directory / f"{request_id}.json", receipt)

    async def _settle_failure(self, req, role, request_id, child_id, state, started, held,
                              exc: ConsultRefused) -> None:
        """Record a failure and release what was held - unless a child may still run."""
        if held.quarantined:
            state.quarantine(request_id, {"outcome": "refused", "code": "consult.quarantined",
                                          "message": "the child could not be confirmed stopped; "
                                                     "its slot is held until an operator clears it"})
            return  # capacity and scratch stay held
        if held.dispatched:
            receipt = {"version": 1, "request_id": request_id, "asker_session": req.asker_session,
                       "root_session": req.root_session, "child_session": child_id,
                       "role": req.target, "admission": "accepted", "outcome": "failed",
                       "diagnostic": exc.code, "policy": self.limits.to_dict(),
                       "elapsed_ms": round((self._clock() - started) * 1000),
                       "answer_chars": 0, "usage_refs": [child_id],
                       "identity": ({"runtime": held.plan.runtime_id, "model_id": held.plan.model_id}
                                    if held.plan is not None else None)}
            self._write_receipt(req.root_session, request_id, receipt)
            state.finish(request_id, {"outcome": "refused", "code": exc.code,
                                      "message": exc.message, "receipt": receipt})
        else:
            with contextlib.suppress(ConsultRefused):
                state.refund(request_id)
        self._release(held)

    @staticmethod
    def _release(held: _Held) -> None:
        if held.reservation is not None and held.capacity is not None:
            with contextlib.suppress(Exception):
                held.capacity.release(held.reservation)
        if held.scratch is not None:
            shutil.rmtree(held.scratch, ignore_errors=True)


@dataclass
class _Held:
    plan: object = None
    reservation: object = None
    capacity: object = None
    scratch: Path | None = None
    task: asyncio.Future | None = None
    dispatched: bool = False
    quiesce: object = None
    quarantined: bool = False


# --- the default child runners ---------------------------------------------------------------


async def default_runner(child: ChildRequest) -> ChildOutcome:
    if getattr(child.plan, "kind", "native") == "acp":
        return await acp_child(child)
    return await native_child(child)


async def native_child(child: ChildRequest) -> ChildOutcome:
    """The dedicated read-only question-and-answer profile in the snapshot."""
    from garuda.agents.setup import prepare_agent_run
    from garuda.core.events import EventStore
    from garuda.interfaces.runner import run_agent_task
    from garuda.plugins.hooks import HookRegistry

    plan = child.plan
    agent = "garuda/consult"
    if plan.profile:  # the role's agent: its model, instructions, memory and skills (H.10)
        from garuda.agents import role_agent

        agent = role_agent.consult_spec(
            role_agent.native_spec(plan, child.source_workspace), child.source_workspace)
    prepared = await prepare_agent_run(
        agent, workspace=child.workspace, model=plan.model_id,
        permission_mode="readonly", reasoning_effort=plan.effort, no_collection=True,
        load_project_tools=False)
    config = prepared.config
    config.max_turns = child.limits.max_turns
    config.max_tokens = child.limits.max_output_tokens
    config.deadline_sec = child.deadline_sec
    config.enable_verifier = False
    config.enable_acceptance_contract = False
    events = EventStore(child.child_id)
    result = await run_agent_task(
        task=child.prompt, model=prepared.reasoning, agent=prepared.agent, tools=prepared.tools,
        config=config, permissions=prepared.permissions, workspace=child.workspace, events=events,
        hooks=HookRegistry(), mcp_manager=prepared.mcp_manager, store=child.store,
        session_record={"origin": "consult", "consult": {
            "asker_session": child.asker_session, "root_session": child.root_session,
            "request_id": child.request_id},
            "role": plan.record()})
    # Screened refusals have no tool result; file-access refusals happen during
    # execution. Count their structured evidence, never words in an error body.
    denied = sum(1 for e in events.get_all()
                 if (e["type"] == "permission_ask"
                     and (e.get("payload") or {}).get("approved") is False)
                 or (e["type"] == "tool_result"
                     and (e.get("payload") or {}).get("is_error")
                     and (e.get("payload") or {}).get("permission_denied") is True))
    return ChildOutcome(events.session_id, bool(result.success), result.final_message or "", denied)


async def acp_child(child: ChildRequest) -> ChildOutcome:
    """An external child runs only in the proven Docker read-only confinement; never on the host."""
    from dataclasses import replace

    from garuda.interfaces.runtime_cli import run_acp_task
    from garuda.workspace.no_edits import deny_all

    plan = replace(child.plan, permissions="readonly")
    try:
        summary = await run_acp_task(
            child.prompt, runtime_id=plan.runtime_id, workspace=child.workspace,
            approval=deny_all, emit=lambda *_a: None, role_plan=plan, store=child.store)
    except Exception as exc:
        if getattr(exc, "code", "") == "workspace.readonly_unenforced":
            raise ConsultRefused("consult.isolation_unavailable", str(exc)) from exc
        raise
    meta = child.store.load_meta(summary["session_id"])
    child.store.update_meta(summary["session_id"], {"origin": "consult"})
    return ChildOutcome(summary["session_id"], summary.get("status") == "completed",
                        str(meta.get("final_message") or ""))
