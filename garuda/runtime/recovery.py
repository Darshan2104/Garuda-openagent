"""Cancellation boundaries and crash recovery (P0.19, issue #29).

Cancellation lands at turn, switch, or process boundaries — never mid-turn —
and every interrupted session restarts from a classified state, never a guess:

- a crash mid-run resumes from its checkpoints;
- a failed or cancelled switch resumes the retained source;
- a prepared-but-unacknowledged switch rolls back before resuming;
- orphan agent child processes whose persisted identity still matches are
  reaped and verified dead;
- ambiguous terminal state refuses recovery instead of inventing an outcome.

Source state is retained until the target acknowledges (the handoff machine
enforces that); recovery only ever marks the failed attempt, never the source.
A bare process exit is evidence of nothing — only an accepted completion
verdict marks success, so exit codes alone never flip a session to successful.

Process safety is a guardrail, not a sandbox. Recovery signals a process only
when a persisted record says Garuda launched it for this session, the owning
Garuda process is gone, no live workspace lease names the session, and the
PID's current start time and command still equal what was recorded at launch.
Signals go to the child's own process group; descendants that left that group
(`setsid`, daemonised helpers) are outside what recovery can find or kill.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from garuda.runtime.protocol import AgentRuntimeError

#: Upper bound on waiting for a SIGKILLed group leader to disappear. SIGKILL
#: delivery is asynchronous: the leader is routinely still visible (or a
#: zombie awaiting its new parent) for a moment after `killpg` returns.
REAP_TIMEOUT_SEC = 3.0
REAP_POLL_SEC = 0.02
#: Cancellation audit entries kept per session (append-only, oldest dropped).
MAX_CANCELLATIONS = 50
_PS_CANDIDATES = ("/bin/ps", "/usr/bin/ps")


class RestartState(str, Enum):
    RESUMABLE = "resumable"
    ROLLED_BACK = "rolled_back"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RecoveryReport:
    session_id: str
    state: RestartState
    resume_session_id: str
    reaped_pids: tuple[int, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def success_claim(self) -> bool:
        """Recovery never claims success. Only an accepted completion does."""
        return False


class RecoveryError(Exception):
    """Recovery refused: ambiguous terminal state or an unreapable child."""


class ProcessIdentityUnavailable(RecoveryError):
    """A process identity probe failed; the answer is unknown, not 'gone'."""


class CancellationAuditError(RecoveryError, AgentRuntimeError):
    """The cancellation happened, but its audit record could not be persisted.

    Raised only after the runtime has already cancelled or closed, so an audit
    failure never keeps work running; callers still learn the trail is short.
    """


def _validate_pid(pid: object) -> int:
    """Accept only a safe, concrete child/process-group leader id.

    PID 0 addresses the caller's process group and negative values have
    similarly broad signal semantics. Recovery may only signal an explicitly
    recorded positive child that is not Garuda itself or its current group.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise RecoveryError(f"invalid persisted child pid {pid!r}")
    if pid in {os.getpid(), os.getpgrp()}:
        raise RecoveryError(f"refusing to signal Garuda's own pid/process group {pid}")
    return pid


# -- process identity ---------------------------------------------------------


def _linux_stat(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat from field 3 (state) on; None when gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        raise ProcessIdentityUnavailable(f"cannot read /proc/{pid}/stat: {exc}") from exc
    open_paren, close_paren = text.find("("), text.rfind(")")
    if open_paren < 0 or close_paren < open_paren:
        raise ProcessIdentityUnavailable(f"unparseable /proc/{pid}/stat")
    return [text[open_paren + 1 : close_paren], *text[close_paren + 2 :].split()]


def _run_ps(pid: int, *columns: str) -> str:
    """Run the system `ps` for one pid with a fixed locale and time zone."""
    ps = next((path for path in _PS_CANDIDATES if os.path.exists(path)), None)
    if ps is None:
        raise ProcessIdentityUnavailable("no system ps to read process identity")
    argv = [ps]
    for column in columns:
        argv += ["-o", f"{column}="]
    argv += ["-p", str(pid)]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            env={"LC_ALL": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessIdentityUnavailable(f"ps failed for pid {pid}: {exc}") from exc
    return result.stdout.strip()


def _use_proc() -> bool:
    return os.path.isfile("/proc/self/stat")


def _process_identity(pid: int) -> str | None:
    """A PID-reuse-resistant identity: start time plus command.

    Returns None when no such process exists and raises
    `ProcessIdentityUnavailable` when the probe itself fails (indeterminate).
    Linux reads `/proc/<pid>/stat` (boot id, starttime in ticks since boot,
    comm); other POSIX hosts use `ps -o lstart= -o comm=` under C/UTC. A PID the
    OS recycled for a different program, or the same program started later,
    yields a different identity.
    """
    if _use_proc():
        fields = _linux_stat(pid)
        if fields is None:
            return None
        if len(fields) < 21:
            raise ProcessIdentityUnavailable(f"short /proc/{pid}/stat")
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            boot_id = ""
        # fields[0] is comm (stat field 2); starttime is stat field 22.
        return f"linux:{boot_id}:{fields[20]}:{fields[0]}"
    out = _run_ps(pid, "lstart", "comm")
    if not out:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except OSError as exc:
            raise ProcessIdentityUnavailable(f"pid {pid} is not inspectable: {exc}") from exc
        raise ProcessIdentityUnavailable(f"ps returned no identity for live pid {pid}")
    return "ps:" + " ".join(out.split())


def _is_zombie(pid: int) -> bool:
    """True for an exited process that only awaits reaping by its parent.

    A zombie can run nothing, and `kill(pid, 0)` still succeeds on it. Probe
    failures answer False, so the caller keeps treating the pid as live (and
    therefore still requires proof of death) rather than assuming it gone.
    """
    try:
        if _use_proc():
            fields = _linux_stat(pid)
            return bool(fields) and len(fields) > 1 and fields[1] == "Z"
        return _run_ps(pid, "stat").startswith("Z")
    except RecoveryError:
        return False


def _process_live(pid: int) -> bool | None:
    """Probe one pid. True = live, False = dead, None = indeterminate.

    `ProcessLookupError` means dead, and so does a zombie. `PermissionError`
    (or any other `OSError`) means the process may be alive under another
    owner — declaring it dead would be a guess, so it is indeterminate and the
    caller must refuse without operator action.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None
    return not _is_zombie(pid)


def _read_identity(identify: Callable[[int], str | None], pid: int) -> str:
    try:
        identity = identify(pid)
    except RecoveryError as exc:
        raise RecoveryError(f"cannot read identity of pid {pid}: {exc}") from exc
    if not isinstance(identity, str) or not identity:
        raise RecoveryError(f"cannot read identity of pid {pid}; refusing to record it")
    return identity


# -- persisted child records ---------------------------------------------------


def _children(meta: dict) -> list:
    children = meta.get("runtime_children", [])
    if not isinstance(children, list):
        raise RecoveryError("persisted runtime children must be a list")
    return list(children)


def record_child(
    store,
    session_id: str,
    *,
    runtime_id: str,
    pid: int,
    process_group: int | None = None,
    identify: Callable[[int], str | None] = _process_identity,
) -> None:
    """Record a Garuda-launched child bound to this persisted session.

    This is the only source `recover()` consults in production. The record
    binds the runtime identity to a known unified segment, and captures two
    process identities (start time + command): the child's, so a later
    restart never signals a recycled PID, and the owning Garuda process's, so
    recovery refuses while that owner is still alive. A child whose identity
    cannot be read is refused rather than launched unrecoverable.
    """
    pid = _validate_pid(pid)
    process_group = _validate_pid(process_group if process_group is not None else pid)
    if process_group != pid:
        raise RecoveryError("recorded child must lead its own isolated process group")
    unified = store.load_unified(session_id)
    if runtime_id not in {segment.runtime_id for segment in unified.segments}:
        raise RecoveryError(
            f"child runtime {runtime_id!r} is not bound to session {session_id}"
        )
    owner_pid = os.getpid()
    entry = {
        "session_id": session_id,
        "runtime_id": runtime_id,
        "pid": pid,
        "process_group": process_group,
        "identity": _read_identity(identify, pid),
        "owner": {"pid": owner_pid, "identity": _read_identity(identify, owner_pid)},
        "state": "live",
    }
    store.mutate_meta(
        session_id, lambda meta: {"runtime_children": [*_children(meta), entry]}
    )


def _retire(store, session_id: str, outcomes: dict[int, str]) -> None:
    """Flip `live` records for the given pids to their observed end state."""

    def _flip(meta: dict) -> dict:
        updated = []
        for child in _children(meta):
            if (
                isinstance(child, dict)
                and child.get("state", "live") == "live"
                and child.get("pid") in outcomes
            ):
                child = {**child, "state": outcomes[child["pid"]]}
            updated.append(child)
        return {"runtime_children": updated}

    store.mutate_meta(session_id, _flip)


def record_child_exit(store, session_id: str, *, pid: int) -> None:
    """Retire a recorded child after Garuda itself reaped it.

    Only `live` records are signal candidates. A record left `live` (Garuda
    crashed between reaping and this write) is safe only because recovery
    re-checks the persisted process identity before any signal.
    """
    _retire(store, session_id, {pid: "exited"})


def _live_children(store, session_id: str) -> list[dict]:
    """Load only well-formed, live identities recorded by Garuda itself."""
    meta = store.load_meta(session_id)
    children = _children(meta)
    unified = store.load_unified(session_id)
    runtime_ids = {segment.runtime_id for segment in unified.segments}
    live: list[dict] = []
    for child in children:
        if not isinstance(child, dict):
            raise RecoveryError("persisted runtime child must be a mapping")
        if child.get("state", "live") != "live":
            continue
        if child.get("session_id") != session_id:
            raise RecoveryError("persisted runtime child has a mismatched session identity")
        runtime_id = child.get("runtime_id")
        if not isinstance(runtime_id, str) or runtime_id not in runtime_ids:
            raise RecoveryError("persisted runtime child is not bound to a session runtime")
        pid = _validate_pid(child.get("pid"))
        process_group = _validate_pid(child.get("process_group"))
        if process_group != pid:
            raise RecoveryError("persisted child must lead its isolated process group")
        identity = child.get("identity")
        if not isinstance(identity, str) or not identity:
            raise RecoveryError(f"persisted child {pid} has no process identity; refusing")
        owner = child.get("owner")
        if (
            not isinstance(owner, dict)
            or isinstance(owner.get("pid"), bool)
            or not isinstance(owner.get("pid"), int)
            or owner["pid"] <= 0
            or not isinstance(owner.get("identity"), str)
            or not owner["identity"]
        ):
            raise RecoveryError(f"persisted child {pid} has no owner identity; refusing")
        live.append(child)
    return live


def _recorded_child_pids(store, session_id: str) -> list[int]:
    """PIDs of the well-formed live child records (signal candidates)."""
    return [child["pid"] for child in _live_children(store, session_id)]


def audit_terminal(events: list) -> bool:
    """True when terminal state is unambiguous: at most one terminal event,
    and when present it is the last event in the trail."""
    terminal = [i for i, e in enumerate(events) if e.is_terminal()]
    if not terminal:
        return True
    return len(terminal) == 1 and terminal[0] == len(events) - 1


def _reap_group(pid: int) -> None:
    """SIGKILL a recorded child's process group — only while it still leads it.

    The caller has already matched the pid's persisted start-time/command
    identity; leading its own group is a second, cheap consistency check
    (Garuda launches every ACP child with `start_new_session`).
    """
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise RecoveryError(
            f"cannot confirm process group of child {pid}: {exc}; refusing"
        ) from exc
    if group != pid:
        raise RecoveryError(
            f"pid {pid} no longer leads its recorded process group; refusing to signal"
        )
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise RecoveryError(f"could not signal child group {pid}: {exc}") from exc


def _await_death(
    pid: int,
    is_alive: Callable[[int], bool | None],
    timeout: float,
) -> None:
    """Poll until `pid` is observed dead; bounded, fail-closed otherwise."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        state = is_alive(pid)
        if state is None:
            raise RecoveryError(
                f"child process {pid} has indeterminate liveness after reaping; "
                "refusing without operator action"
            )
        if not state:
            return
        if time.monotonic() >= deadline:
            raise RecoveryError(f"child process {pid} survived reaping; refusing")
        time.sleep(REAP_POLL_SEC)


def reap_orphans(
    pids: list[int],
    *,
    is_alive: Callable[[int], bool | None],
    reap: Callable[[int], None],
    timeout: float = REAP_TIMEOUT_SEC,
) -> tuple[int, ...]:
    """Kill pids through `reap` and verify each one dead within `timeout`.

    A verification primitive with no identity check of its own, so `reap` has
    no default: production signalling goes through `recover()`, which gates
    every pid on its persisted identity and owner first. Indeterminate
    liveness refuses until an operator confirms the process is gone.
    """
    reaped: list[int] = []
    for pid in pids:
        pid = _validate_pid(pid)
        alive = is_alive(pid)
        if alive is None:
            raise RecoveryError(
                f"child process {pid} has indeterminate liveness; "
                "refusing without operator action"
            )
        if not alive:
            continue
        reap(pid)
        _await_death(pid, is_alive, timeout)
        reaped.append(pid)
    return tuple(reaped)


def audit_core_trail(path: str | Path) -> bool:
    """True when the persisted core trail is unambiguous: no malformed lines,
    at most one `session_end`, and nothing substantive after it.

    A missing trail is unambiguous (nothing to contradict). A torn or
    foreign line is malformed and blocks recovery. Post-terminal telemetry
    (`turn_metrics`, `budget`) is bookkeeping the loop emits after closing
    the session — allowed. Anything else after the terminal (a second end,
    more model/tool traffic, a new start) is ambiguous and refuses.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RecoveryError(f"unreadable event trail at {path}: {exc}") from exc
    kinds: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return False
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return False
        kinds.append(event["type"])
    ends = [i for i, kind in enumerate(kinds) if kind == "session_end"]
    if not ends:
        return True
    if len(ends) > 1:
        return False
    return all(kind in ("turn_metrics", "budget") for kind in kinds[ends[0] + 1 :])


def record_cancel(store, session_id: str, *, boundary: str, reason: str = "") -> None:
    """Append a cancellation at a named boundary (turn, switch, or process).

    Audit evidence only: `cancellations` is an append-only list (the newest
    `MAX_CANCELLATIONS` kept) that classification does not yet consume —
    restart state comes from handoff state, checkpoints, and the trail.
    """
    if boundary not in ("turn", "switch", "process"):
        raise RecoveryError(f"unknown cancellation boundary {boundary!r}")
    entry = {
        "boundary": boundary,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }

    def _append(meta: dict) -> dict:
        history = meta.get("cancellations", [])
        if not isinstance(history, list):
            raise RecoveryError("persisted cancellations must be a list")
        return {"cancellations": [*history, entry][-MAX_CANCELLATIONS:]}

    store.mutate_meta(session_id, _append)


def classify(store, session_id: str) -> RecoveryReport:
    """Classify a session's restart state from its persisted records only."""
    try:
        meta = store.load_meta(session_id)
    except (OSError, ValueError) as exc:
        raise RecoveryError(f"session {session_id} is unreadable: {exc}") from exc
    if not isinstance(meta, dict) or not meta.get("session_id"):
        raise RecoveryError(f"session {session_id} has no identity; refusing")
    try:
        unified = store.load_unified(session_id)
    except Exception as exc:
        raise RecoveryError(f"session {session_id} fails validation: {exc}") from exc
    try:
        store.load_messages(session_id)
    except Exception as exc:
        raise RecoveryError(
            f"session {session_id} has no readable message checkpoint: {exc}"
        ) from exc
    active = unified.active
    if active.kind == "native" and active.native_session_id != session_id:
        raise RecoveryError("native runtime identity does not match the persisted session")
    if active.kind == "acp":
        try:
            from garuda.runtime.session import validate_authority_snapshot

            validate_authority_snapshot(active.capabilities)
        except Exception as exc:
            raise RecoveryError(
                f"ACP authority snapshot is missing or inconsistent: {exc}"
            ) from exc
    handoff_state = unified.handoff.get("state", "none")
    if handoff_state == "prepared":
        return RecoveryReport(
            session_id=session_id,
            state=RestartState.ROLLED_BACK,
            resume_session_id=session_id,
            notes=("switch prepared but never acknowledged; source retained",),
        )
    if handoff_state in ("failed", "none", "acknowledged"):
        return RecoveryReport(
            session_id=session_id,
            state=RestartState.RESUMABLE,
            resume_session_id=session_id,
            notes=(f"handoff state {handoff_state}; checkpoints intact",),
        )
    raise RecoveryError(f"session {session_id} has unknown handoff state {handoff_state!r}")


def _refuse_live_lease(session_id: str, leases) -> None:
    """Refuse while any unexpired workspace lease still names this session."""
    from garuda.workspace.lease import LeaseError, LeaseStore

    lease_store = leases if leases is not None else LeaseStore()
    try:
        holders = lease_store.live_holders_for_session(session_id)
    except LeaseError as exc:
        raise RecoveryError(f"cannot audit workspace leases: {exc}; refusing") from exc
    if holders:
        holder = holders[0]
        raise RecoveryError(
            f"session {session_id} still holds a live {holder.mode} workspace lease "
            f"(pid {holder.pid}); another Garuda process may own it — refusing until "
            "it exits or the lease expires"
        )


def _refuse_live_owner(child: dict, identify: Callable[[int], str | None]) -> None:
    """Refuse while the Garuda process that launched `child` is still alive."""
    owner = child["owner"]
    try:
        current = identify(owner["pid"])
    except RecoveryError as exc:
        raise RecoveryError(
            f"cannot confirm owner process {owner['pid']} of child {child['pid']} "
            f"is gone: {exc}; refusing without operator action"
        ) from exc
    if current is not None and current == owner["identity"]:
        raise RecoveryError(
            f"child {child['pid']} belongs to live Garuda process {owner['pid']}; "
            "refusing to reap another instance's runtime"
        )


def recover(
    store,
    session_id: str,
    *,
    is_alive: Callable[[int], bool | None] = _process_live,
    reap: Callable[[int], None] = _reap_group,
    identify: Callable[[int], str | None] = _process_identity,
    leases=None,
    reap_timeout: float = REAP_TIMEOUT_SEC,
) -> RecoveryReport:
    """Audit, classify, reap identity-matched orphans, and mark rolled-back
    switches. Returns the session to resume; never invents success.

    Order: refuse while a live workspace lease or a live owning Garuda
    process names the session; audit the persisted trail and classify (both
    read-only); only then signal. A recorded child is signalled only when it
    is still alive and its start-time/command identity equals the recorded
    one; a gone child is retired `exited`, a recycled PID is retired
    `reused` without a signal, and a signalled child must be observed dead
    within `reap_timeout` before it is retired `reaped`.
    """
    # The session record is the only source of signal targets.  Callers may
    # customise the probes for deterministic tests, but may not smuggle an
    # arbitrary PID into a production recovery operation.
    _refuse_live_lease(session_id, leases)
    children = _live_children(store, session_id)
    for child in children:
        _refuse_live_owner(child, identify)
    events_path = getattr(store, "events_path", None)
    if callable(events_path):
        try:
            trail_ok = audit_core_trail(events_path(session_id))
        except RecoveryError:
            raise
        except Exception as exc:
            raise RecoveryError(
                f"session {session_id} trail audit failed: {exc}"
            ) from exc
        if not trail_ok:
            raise RecoveryError(
                f"session {session_id} has a malformed or ambiguous terminal "
                "trail; refusing without operator action"
            )
    report = classify(store, session_id)

    outcomes: dict[int, str] = {}
    reaped: list[int] = []
    notes: list[str] = []
    try:
        for child in children:
            pid = child["pid"]
            alive = is_alive(pid)
            if alive is None:
                raise RecoveryError(
                    f"child process {pid} has indeterminate liveness; "
                    "refusing without operator action"
                )
            if not alive:
                outcomes[pid] = "exited"
                continue
            try:
                current = identify(pid)
            except RecoveryError as exc:
                raise RecoveryError(
                    f"child process {pid} identity is indeterminate: {exc}; "
                    "refusing without operator action"
                ) from exc
            if current is None:
                outcomes[pid] = "exited"
                continue
            if current != child["identity"]:
                outcomes[pid] = "reused"
                notes.append(f"pid {pid} was reused by another process; not signalled")
                continue
            reap(pid)
            _await_death(pid, is_alive, reap_timeout)
            outcomes[pid] = "reaped"
            reaped.append(pid)
    except BaseException:
        if outcomes:
            try:
                _retire(store, session_id, outcomes)
            except Exception:
                pass
        raise
    if outcomes:
        try:
            _retire(store, session_id, outcomes)
        except Exception as exc:
            raise RecoveryError(
                f"session {session_id} children were handled but could not be "
                f"retired: {exc}"
            ) from exc
    if report.state is RestartState.ROLLED_BACK:
        store.record_handoff(session_id, state="failed", attempts=1)
    return RecoveryReport(
        session_id=report.session_id,
        state=report.state,
        resume_session_id=report.resume_session_id,
        reaped_pids=tuple(reaped),
        notes=(*report.notes, *notes, *(f"reaped child {pid}" for pid in reaped)),
    )


def report_to_dict(report: RecoveryReport) -> dict[str, Any]:
    return {
        "session_id": report.session_id,
        "state": report.state.value,
        "resume_session_id": report.resume_session_id,
        "reaped_pids": list(report.reaped_pids),
        "notes": list(report.notes),
        "success_claim": report.success_claim,
    }
