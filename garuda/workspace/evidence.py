"""Session workspace-evidence boundary (P0.18, issue #28).

One shared service for every entry point that persists a session and runs an
agent: classify the workspace kind, record the session's single immutable
baseline before any prompt, hand the verifier a loader bound to that record,
and persist the final delta. The git mechanics live in `garuda.workspace.diff`.

Attribution is possible only when the host path *is* the tree the agent
mutates. `local`, `sandbox` (a guarded `LocalEnvironment` on the host root),
`tmux` (a host shell rooted at the workspace), and `docker` (the host
workspace bind-mounted at `/workspace`) all qualify. `remote` bind-mounts a
path on the remote daemon's host, so the local path says nothing about what
changed there; it is recorded as `unsupported_nonlocal`, never as an empty
delta. An unknown kind fails closed. `docker` is classified by kind: a
`DOCKER_HOST`/context pointing at a remote daemon is the `remote` kind's case
and must be configured as such, or the host path is not what the container
sees. Files a root container creates unreadable to the host user fail the
delta closed (`DiffError`) rather than being skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from garuda.workspace.diff import (
    BASELINE_CAPTURED,
    BASELINE_UNSUPPORTED_NONLOCAL,
    Baseline,
    BaselineError,
    DiffError,
    SessionDelta,
    capture_baseline,
    session_delta,
)

logger = logging.getLogger(__name__)

#: Kinds whose mutated tree is the host workspace path.
HOST_BACKED_KINDS = frozenset({"local", "sandbox", "tmux", "docker"})
#: Kinds whose mutated tree is elsewhere; attribution is recorded unsupported.
NONLOCAL_KINDS = frozenset({"remote"})

#: Session meta key holding the capture taken when a session finished: the
#: evidence a resume uses to prove nothing changed between sessions.
FINAL_STATE_KEY = "final_workspace_state"

#: Cap on the path lists persisted into session meta (the full delta is still
#: returned to the caller for the run result).
META_PATH_LIMIT = 200

DeltaLoader = Callable[[], SessionDelta]

# A child handoff is intentionally smaller than persisted session evidence.  The
# complete Git view remains available to the child trace; the parent gets enough
# authoritative path identity to act without an unbounded metadata payload.
CHILD_PATH_LIMIT = 30
CHILD_PATH_CHARS = 300


@dataclass(frozen=True)
class WorkspaceObservation:
    """A child-scoped start boundary against the actual execution workspace."""

    workspace: str | None
    workspace_kind: str
    baseline: Baseline | None = None
    attribution: str = BASELINE_CAPTURED
    error: str | None = None


def begin_workspace_observation(
    workspace: str | Path | None, workspace_kind: str
) -> WorkspaceObservation:
    """Capture a non-mutating child baseline, retaining unsupported/error states."""
    try:
        local = host_backed(workspace_kind)
    except BaselineError as exc:
        return WorkspaceObservation(
            None,
            workspace_kind,
            attribution="error",
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    if not local:
        return WorkspaceObservation(
            str(workspace) if workspace is not None else None,
            workspace_kind,
            attribution=BASELINE_UNSUPPORTED_NONLOCAL,
        )
    if workspace is None:
        return WorkspaceObservation(
            None, workspace_kind, attribution="unavailable_host_root"
        )
    resolved = _resolved_workspace(workspace)
    try:
        baseline = capture_baseline(workspace)
        return WorkspaceObservation(
            resolved, workspace_kind, baseline=baseline, attribution=baseline.state
        )
    except (BaselineError, DiffError, OSError) as exc:
        return WorkspaceObservation(
            resolved,
            workspace_kind,
            attribution="error",
            error=f"{type(exc).__name__}: {exc}"[:500],
        )


def finish_workspace_observation(observation: WorkspaceObservation) -> dict:
    """Return bounded authoritative child evidence; emptiness never masks failure."""
    evidence: dict = {
        "kind": observation.workspace_kind,
        "attribution": observation.attribution,
    }
    if observation.workspace is not None:
        if len(observation.workspace) <= CHILD_PATH_CHARS:
            evidence["root"] = observation.workspace
        else:
            evidence["root_omitted"] = True
    if observation.error:
        evidence["error"] = observation.error
        return evidence
    if observation.baseline is None:
        return evidence
    try:
        delta = session_delta(observation.baseline, observation.workspace)
    except (DiffError, OSError) as exc:
        return {
            **evidence,
            "attribution": "error",
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    evidence["attribution"] = delta.attribution
    if not delta.attributable:
        return evidence
    evidence["baseline_commit"] = delta.baseline_commit
    if delta.head_commit:
        evidence["head_commit"] = delta.head_commit
    changed = [file for file in delta.files if not file.preexisting]
    evidence["changed_count"] = len(changed)
    files = []
    omitted = 0
    for file in changed:
        if len(files) >= CHILD_PATH_LIMIT or len(file.path) > CHILD_PATH_CHARS:
            omitted += 1
            continue
        record = {
            "path": file.path,
            "kind": file.kind,
            **({"preexisting_at_start": True} if file.preexisting_at_start else {}),
        }
        if file.renamed_from:
            if len(file.renamed_from) <= CHILD_PATH_CHARS:
                record["renamed_from"] = file.renamed_from
            else:
                record["renamed_from_omitted"] = True
        files.append(record)
    evidence["files"] = files
    if omitted:
        evidence["files_omitted"] = omitted
    return evidence


def host_backed(workspace_kind: str) -> bool:
    """True when the host workspace path is the tree the agent mutates.

    The single classification every entry point uses. Unknown kinds raise:
    attribution cannot be guessed for an environment this module has not
    been told about.
    """
    if workspace_kind in HOST_BACKED_KINDS:
        return True
    if workspace_kind in NONLOCAL_KINDS:
        return False
    raise BaselineError(f"unknown workspace kind {workspace_kind!r}; attribution unclassified")


def _resolved_workspace(workspace: str | Path) -> str:
    return str(Path(workspace).resolve())


def _inheritable_baseline(
    store, prior_session_id: str, workspace: str | Path, current: Baseline
) -> tuple[Baseline | None, str]:
    """The resumed session's baseline when it provably still applies.

    Returns ``(baseline, "")`` to inherit, or ``(None, reason)``. The proof
    is the prior session's *end* state: its start baseline carries forward
    only when this workspace is exactly as that session left it — same
    resolved path, and the same HEAD, status, and dirty-file fingerprints as
    the capture its finish recorded. Anything else (a pull, a teammate's
    commit, a human edit between sessions, a session that crashed before
    finishing, or one recorded before end states were) falls back to a
    fresh capture, which is always the narrower attribution claim.
    """
    if current.state != BASELINE_CAPTURED:
        return None, "workspace is not a git work tree"
    try:
        prior = store.load_meta(prior_session_id)
    except Exception:
        return None, "resumed session meta is unreadable"
    if not isinstance(prior, dict) or prior.get("baseline_state") != BASELINE_CAPTURED:
        return None, "resumed session recorded no captured baseline"
    if prior.get("baseline_workspace") != _resolved_workspace(workspace):
        # A different checkout of the same history would otherwise look
        # identical to this one.
        return None, "resumed session baseline was recorded for another workspace"
    try:
        baseline = Baseline.from_dict(prior.get("baseline") or {})
    except DiffError:
        return None, "resumed session baseline is malformed"
    if baseline.state != BASELINE_CAPTURED:
        return None, "resumed session recorded no captured baseline"
    ended = prior.get(FINAL_STATE_KEY)
    if not isinstance(ended, dict) or not ended:
        return None, "resumed session recorded no end state"
    try:
        final = Baseline.from_dict(ended)
    except DiffError:
        return None, "resumed session end state is malformed"
    if final.to_dict() != current.to_dict():
        return None, "workspace changed since the resumed session ended"
    if any(digest.startswith("special:") for digest in final.fingerprints.values()):
        # A dirty submodule or nested repository fingerprints as its kind
        # only, so edits inside it between sessions would be invisible.
        return None, "resumed session left opaque dirty paths (submodule or nested repository)"
    return baseline, ""


def record_session_baseline(
    store,
    session_id: str,
    workspace: str | Path,
    workspace_kind: str,
    *,
    inherit_from: str | None = None,
) -> Baseline | None:
    """Persist the one baseline a session is allowed to use later.

    Attribution is a security and verification claim, so an unreadable Git
    view or metadata write refuses startup. A non-repo workspace records an
    explicit `unsupported_nonrepo` baseline; a non-local workspace records
    `unsupported_nonlocal` and returns None.

    ``inherit_from`` names the session a resume continues. When its recorded
    baseline provably still applies to this workspace (see
    `_inheritable_baseline`), that baseline is recorded instead of a fresh
    capture, so the prior session's work is attributed as session work, not
    as preexisting dirt. Otherwise a fresh baseline is captured and the
    refusal reason recorded as ``baseline_inherit_refused``.
    """
    if not host_backed(workspace_kind):
        nonlocal_meta: dict = {"baseline_state": BASELINE_UNSUPPORTED_NONLOCAL, "baseline": {}}
        if inherit_from:
            nonlocal_meta["baseline_inherit_refused"] = "workspace is not host-backed"
        try:
            store.update_meta(session_id, nonlocal_meta)
        except Exception as exc:
            raise BaselineError(f"could not record non-local baseline state: {exc}") from exc
        return None
    try:
        baseline = capture_baseline(workspace)
        provenance: dict = {}
        if baseline.state == BASELINE_CAPTURED:
            provenance["baseline_workspace"] = _resolved_workspace(workspace)
        if inherit_from:
            inherited, reason = _inheritable_baseline(
                store, inherit_from, workspace, baseline
            )
            if inherited is not None:
                baseline = inherited
                provenance["baseline_inherited_from"] = inherit_from
            else:
                provenance["baseline_inherit_refused"] = reason
        store.record_baseline(session_id, baseline.to_dict())
        store.update_meta(session_id, {"baseline_state": baseline.state, **provenance})
    except Exception as exc:
        raise BaselineError(f"could not capture and persist workspace baseline: {exc}") from exc
    return baseline


def load_session_delta(store, session_id: str, workspace: str | Path) -> SessionDelta:
    """Compute the delta from the baseline the session recorded at start.

    Handoff and verification consume the *recorded* baseline — never a fresh
    capture — so pre-existing dirt and agent work stay attributed exactly as
    the session saw them. Raises `DiffError` when no baseline was recorded or
    the workspace can no longer be read.
    """
    try:
        meta = store.load_meta(session_id)
    except Exception as exc:
        raise DiffError(f"no readable session meta for {session_id}: {exc}") from exc
    if not isinstance(meta, dict):
        raise DiffError(f"session {session_id} meta is not a mapping")
    if meta.get("baseline_state") == BASELINE_UNSUPPORTED_NONLOCAL:
        return SessionDelta(attribution=BASELINE_UNSUPPORTED_NONLOCAL)
    recorded = meta.get("baseline") or {}
    if not recorded:
        raise DiffError(f"session {session_id} recorded no baseline")
    return session_delta(Baseline.from_dict(recorded), workspace)


def begin_session_evidence(
    store,
    session_id: str,
    workspace: str | Path,
    workspace_kind: str,
    *,
    inherit_from: str | None = None,
) -> DeltaLoader:
    """Record the session baseline and return the loader the verifier uses.

    Call after the session is persisted and before an environment is
    resolved or a prompt is sent. Raises `BaselineError`; the caller must
    refuse the session (see `record_startup_refusal`). A resume passes the
    session it continues as ``inherit_from``.
    """
    record_session_baseline(
        store, session_id, workspace, workspace_kind, inherit_from=inherit_from
    )

    def workspace_delta_loader() -> SessionDelta:
        # Resolved at call time so the loader always reads the recorded record.
        return load_session_delta(store, session_id, workspace)

    return workspace_delta_loader


def _capture_end_state(workspace: str | Path) -> dict | None:
    """The end state a later resume must match to inherit the baseline.

    Best-effort: without it a resume simply captures fresh, so a failure
    here must never fail the session.
    """
    try:
        return capture_baseline(workspace).to_dict()
    except DiffError:
        logger.warning("Could not record the session end state", exc_info=True)
        return None


def record_end_state(store, session_id: str, workspace: str | Path) -> None:
    """Record the end state of a session that stopped without finishing.

    A cancelled or failed run skips `finish_session_evidence`, yet it is the
    session a user is most likely to resume. Recording what it left lets that
    resume inherit its baseline under the same exact-match proof. Only a
    session whose baseline was captured gets one; never raises.
    """
    try:
        meta = store.load_meta(session_id)
        if not isinstance(meta, dict) or meta.get("baseline_state") != BASELINE_CAPTURED:
            return
        end_state = _capture_end_state(workspace)
        if end_state is not None:
            store.update_meta(session_id, {FINAL_STATE_KEY: end_state})
    except Exception:
        logger.warning("Could not record the interrupted session end state", exc_info=True)


def finish_session_evidence(
    store, session_id: str, workspace: str | Path, *, extra_meta: dict | None = None
) -> dict:
    """Persist the final delta into session meta and return its evidence dict.

    Raises when the recorded baseline or the workspace cannot be read or the
    meta write fails; callers turn that into a failed session, never success.
    """
    delta = load_session_delta(store, session_id, workspace)
    updates: dict = {"delta_attribution": delta.attribution}
    if delta.attributable:
        updates.update(
            {
                "baseline_commit": delta.baseline_commit,
                "delta_changed": list(delta.changed[:META_PATH_LIMIT]),
                "delta_preexisting": list(delta.preexisting[:META_PATH_LIMIT]),
            }
        )
        end_state = _capture_end_state(workspace)
        if end_state is not None:
            updates[FINAL_STATE_KEY] = end_state
    if extra_meta:
        updates.update(extra_meta)
    store.update_meta(session_id, updates)
    return delta.to_evidence()


def record_startup_refusal(store, session_id: str) -> None:
    """Best-effort: mark a session that refused to start as failed.

    The original refusal stays authoritative when the store is not writable.
    """
    try:
        store.update_meta(session_id, {"status": "failed", "startup_refused": True})
    except Exception:
        logger.warning("Failed to record startup refusal for %s", session_id, exc_info=True)
