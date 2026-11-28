"""Crash continuation: resume a checked flow whose parent died hard mid-step.

A flow parent killed by SIGKILL, the OOM killer or a reboot leaves its running
step attempt with an intent and no receipt. Such an attempt is never replayed
blind. A **checked** flow (``--check``) ran that attempt in an owned worker
under a Linux subreaper, so ``garuda flow resume`` can settle it automatically,
but only when Garuda proves the attempt's whole process tree is gone:

1. the journal ends with that attempt's intent, and it is the only unsettled one;
2. the flow's recorded owner is dead (``owner_liveness`` by start identity);
3. the attempt's launch record (:mod:`garuda.flows.launches`) shows it ran under
   the ``linux-subreaper`` backend and names its supervisor and, once activation
   began, its worker command; both are proved dead by pid and start identity
   (a group each led is empty too);
4. the parent lease owner the attempt ran under is dead, and no live process
   carries its lineage token (``descendant_scan``), which covers any descendant
   that left the supervisor's tree;
5. what ``garuda runtime recover`` proves holds: every step worker is gone,
   every recorded confined container is absent, and the dead owner's leases and
   slots are reclaimed; afterwards no live lease and no capacity slot names the
   flow.

The crashed attempt is then recorded as a stopped ``flow.interrupted`` attempt
(its receipt, so the journal keeps two entries per attempt) and the flow
continues with the next attempt. If any fact cannot be proved nothing is
written: the attempt stays quarantined and the refusal says what is uncertain
and which command comes next. Unchecked CLI flows are never continued this way.

A **dashboard-started** (owned) flow, checked or not, runs every step in the
same owned worker under the same subreaper and writes the same launch record,
so ``owned=True`` proves the same facts for it. Its owned flow gateway's resume
(``POST /api/scenarios/resume``) is the only way it continues (never
``garuda flow resume``); there the flow's owner, the attempt's lease owner and
the operation's owner are the dashboard process that ran it.
"""

from __future__ import annotations

import datetime as _dt

from garuda.flows import engine, launches

#: Recorded ``flow_stop`` code of a flow whose owner died, settled by recovery.
OWNER_LOST = "flow.owner_lost"
UNSETTLED = "flow.interrupted_unsettled"


def _identity(value) -> bool:
    return (isinstance(value, dict) and type(value.get("pid")) is int and value["pid"] > 1
            and isinstance(value.get("identity"), str)
            and value["identity"] not in {"", "unknown"} and type(value.get("pgid")) is int)


def _recover(store, flow_session: str) -> None:
    """What ``garuda runtime recover --session FLOW`` does, raising its refusal."""
    from garuda.runtime.recovery import recover

    recover(store, flow_session, release_dead_owner=True)
    try:
        from garuda.acp.approval_channel import withdraw_dead_session_approvals

        withdraw_dead_session_approvals(store, flow_session)
    except Exception:  # noqa: BLE001 - parked approvals of a dead flow are advisory here
        pass


def applies(meta: dict, *, owned: bool = False) -> bool:
    """Whether ``meta`` is a checked CLI flow (``owned``: a dashboard-started flow,
    checked or not) that may have died hard mid-step."""
    policy = meta.get("publication_policy")
    stop = meta.get("flow_stop") if isinstance(meta.get("flow_stop"), dict) else {}
    kind = policy.get("kind") if isinstance(policy, dict) else None
    if owned:
        started = (kind in {"owned-flow", "flow-verification"}
                   and isinstance(meta.get("flow_operation"), dict)
                   and meta.get("launch_kind") != "run")
    else:
        started = kind == "flow-verification" and meta.get("flow_operation") is None
    return (started and "background_operation" not in meta
            and meta.get("flow_verification_phase") is None
            and meta.get("flow_verification_commit") is None
            and (meta.get("flow_state") == "running"
                 or (meta.get("flow_state") == "interrupted" and stop.get("code") == OWNER_LOST)))


def owned_resume_hint(flow_session: str) -> str:
    """The owned resume a dashboard-started flow continues through."""
    return ("the dashboard's owned resume (POST /api/scenarios/resume with "
            f'{{"operation_id": "<new UUIDv4>", "session_id": "{flow_session}"}})')


def prove(store, flow_session: str, meta: dict, *, recover=None, owned: bool = False,
          dry: bool = False) -> dict | None:
    """The proof that a crashed checked step attempt's tree is gone, or None.

    None: the flow is not a crashed checked flow (``owned``: a crashed
    dashboard-started flow) with an unsettled attempt (the caller's usual
    handling applies). Raises :class:`engine.FlowStopped` with the uncertainty
    and the next command when anything cannot be proved. May reclaim the dead
    owner's holdings (``recover``, as ``garuda runtime recover`` does); writes
    nothing else. ``dry`` writes nothing at all: it proves every process fact
    but neither reclaims nor checks the holdings (the owned gateway's admission
    check, before it holds the flow's resume claim), and returns only
    ``{"dry": True, ...}``: the full proof must still run before anything settles.
    """
    from garuda.runtime.ownership import descendant_scan, owner_liveness

    if not applies(meta, owned=owned):
        return None
    pending = engine.unsettled_attempts(store, flow_session)
    if not pending:
        return None
    step, attempt = pending[-1]
    name = (meta.get("flow") or {}).get("name") or "NAME"
    if owned:
        label = "dashboard flow"
        resume = owned_resume_hint(flow_session)
        new_flow = ("the attempt cannot be continued; inspect the workspace (`git status`) and "
                    "start a new flow from the dashboard")
    else:
        label = "checked flow"
        resume = f"`garuda flow resume {flow_session}`"
        new_flow = (f"the attempt cannot be continued; inspect the workspace (`git status`) and "
                    f"start a new flow: `garuda flow run {name} --check ...`")
    recover_next = (f"run `garuda runtime recover --session {flow_session}`, which says what is "
                    f"still held; once it succeeds, {resume} continues the flow")

    def refuse(why: str, next_step: str):
        raise engine.FlowStopped(
            UNSETTLED,
            f"{label} {flow_session} lost its process while step {step} (attempt {attempt}) "
            f"was running, and Garuda cannot prove that attempt's whole process tree is gone: "
            f"{why}. The attempt stays quarantined and is never replayed. Next: {next_step}",
            step=step)

    if len(pending) != 1:
        refuse(f"{len(pending)} launched attempts never settled", new_flow)
    entries = engine.journal(store, flow_session)
    last = entries[-1] if entries else {}
    if last.get("event") != "intent" or (last.get("step"), last.get("attempt")) != (step, attempt):
        refuse("the flow journal does not end with that attempt's intent", new_flow)
    if meta.get("flow_state") == "running":
        state = meta.get("state") if isinstance(meta.get("state"), dict) else {}
        owner = state.get("owner")
        alive = owner_liveness(owner) if isinstance(owner, dict) else None
        if alive is True:
            raise engine.FlowStopped(
                "flow.recovery_required",
                f"{label} {flow_session} is still running (pid {owner.get('pid')}); wait for "
                "it, or stop it with SIGTERM (its step settles as flow.interrupted), then "
                f"{resume}", step=step)
        if alive is None:
            raise engine.FlowStopped(
                "flow.recovery_required",
                f"{label} {flow_session} is still recorded running and its process cannot be "
                f"proved gone; {recover_next}", step=step)
    record = launches.read(engine.flow_dir(store, flow_session), step, attempt)
    if (record is None or record.get("backend") != "linux-subreaper"
            or not _identity(record.get("supervisor"))
            or not _identity(record.get("parent_owner"))
            or not isinstance(record.get("session_id"), str)):
        refuse("no launch record shows the attempt's owned worker ran under Garuda's Linux "
               "subreaper with recorded identities", new_flow)
    command = record.get("command")
    if record.get("phase") == "active" and not _identity(command):
        refuse("the worker's launch was acknowledged without a usable identity", new_flow)
    if record.get("phase") == "activating":
        refuse("the worker was being activated and its identity was never recorded", new_flow)
    if record.get("phase") not in {"prepared", "active"}:
        refuse("the attempt's launch record is incomplete", new_flow)
    for label, who in (("subreaper supervisor", record["supervisor"]), ("worker", command)):
        if who is None:
            continue
        alive = owner_liveness(who)
        if alive is not False:
            refuse(f"the step's {label} (pid {who['pid']}) "
                   + ("is still running" if alive else "cannot be proved gone"),
                   "wait for it to exit (it closes its tree when its parent dies), then "
                   f"{resume} again")
    parent = record["parent_owner"]
    if owner_liveness(parent) is not False:
        refuse(f"the lease owner the attempt ran under (pid {parent['pid']}) is not proved dead",
               recover_next)
    if dry:
        survivors, why = descendant_scan(parent)
        if survivors is None:
            refuse(f"its descendants cannot be inspected ({why})", recover_next)
        if survivors:
            refuse(f"process(es) {', '.join(map(str, survivors))} still carry its lineage",
                   f"stop them, then {resume} again")
        return {"dry": True, "step": step, "attempt": attempt}
    if meta.get("flow_state") == "running":
        try:
            (recover or _recover)(store, flow_session)
        except Exception as exc:  # noqa: BLE001 - every refusal keeps the quarantine
            refuse(f"recovery refused ({exc})", recover_next)
        current = store.load_meta(flow_session)
        stop = current.get("flow_stop") if isinstance(current.get("flow_stop"), dict) else {}
        if current.get("flow_state") != "interrupted" or stop.get("code") != OWNER_LOST:
            refuse("recovery did not record the flow interrupted", recover_next)
    survivors, why = descendant_scan(parent)
    if survivors is None:
        refuse(f"its descendants cannot be inspected ({why})", recover_next)
    if survivors:
        refuse(f"process(es) {', '.join(map(str, survivors))} still carry its lineage",
               f"stop them, then {resume} again")
    from garuda.runtime.capacity import CapacityStore
    from garuda.workspace.lease import LeaseStore

    try:
        held = LeaseStore().live_holders_for_session(flow_session)
        slots = CapacityStore().slots_for_session(flow_session)
    except Exception as exc:  # noqa: BLE001
        refuse(f"its leases and capacity slots cannot be read ({exc})", recover_next)
    if held:
        refuse(f"a live workspace lease still names the flow (pid {held[0].pid})", recover_next)
    if slots:
        refuse(f"capacity slot {slots[0]['holder']} still names the flow", recover_next)
    return {"version": 1, "step": step, "attempt": attempt, "session_id": record["session_id"],
            "backend": "linux-subreaper", "supervisor": dict(record["supervisor"]),
            "command": dict(command) if command else None,
            "parent_owner": dict(parent),
            **({"owned": True} if owned else {}),
            "proved": ["flow owner dead", "supervisor dead", "worker dead",
                       "lease owner dead", "no process carries its lineage",
                       "step workers and recorded containers gone", "no live lease",
                       "no capacity slot"],
            "proved_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
