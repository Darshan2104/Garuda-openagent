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
from pathlib import Path

from garuda.workspace.diff import (
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

#: Cap on the path lists persisted into session meta (the full delta is still
#: returned to the caller for the run result).
META_PATH_LIMIT = 200

DeltaLoader = Callable[[], SessionDelta]


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


def record_session_baseline(
    store, session_id: str, workspace: str | Path, workspace_kind: str
) -> Baseline | None:
    """Persist the one baseline a session is allowed to use later.

    Attribution is a security and verification claim, so an unreadable Git
    view or metadata write refuses startup. A non-repo workspace records an
    explicit `unsupported_nonrepo` baseline; a non-local workspace records
    `unsupported_nonlocal` and returns None.
    """
    if not host_backed(workspace_kind):
        try:
            store.update_meta(
                session_id,
                {"baseline_state": BASELINE_UNSUPPORTED_NONLOCAL, "baseline": {}},
            )
        except Exception as exc:
            raise BaselineError(f"could not record non-local baseline state: {exc}") from exc
        return None
    try:
        baseline = capture_baseline(workspace)
        store.record_baseline(session_id, baseline.to_dict())
        store.update_meta(session_id, {"baseline_state": baseline.state})
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
    store, session_id: str, workspace: str | Path, workspace_kind: str
) -> DeltaLoader:
    """Record the session baseline and return the loader the verifier uses.

    Call after the session is persisted and before an environment is
    resolved or a prompt is sent. Raises `BaselineError`; the caller must
    refuse the session (see `record_startup_refusal`).
    """
    record_session_baseline(store, session_id, workspace, workspace_kind)

    def workspace_delta_loader() -> SessionDelta:
        # Resolved at call time so the loader always reads the recorded record.
        return load_session_delta(store, session_id, workspace)

    return workspace_delta_loader


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
