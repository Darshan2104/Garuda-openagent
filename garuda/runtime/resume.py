"""How a session is resumed (plan task B.7, #157).

``garuda run --resume S`` continues ``S`` in a new, linked session
(``resumed_from``). :func:`plan_resume` picks how, before anything starts:

- ``native`` — ``S`` ran on the native loop and continues there: its
  transcript and working state are restored (existing behaviour).
- ``acp`` — ``S`` ran on an ACP runtime whose cross-process resume was
  *exercised* for that exact adapter version (:data:`PROVEN_LOAD`), the
  recorded segment has the agent's session id, and the version has not
  drifted. The adapter reloads the agent's own session (``session/load``).
  If the agent no longer offers it at the handshake, the run falls back to a
  brief.
- ``brief`` — everything else: the runtime only *declares* resume (Claude
  Code and Codex today), the adapter version changed, or the user continues
  on another runtime with ``--as``. A new session starts with ``S``'s bounded
  brief attached as data (``resume_mode: brief``); nothing of ``S``'s
  transcript crosses.

A session whose owner is still live refuses (``session.live_owner``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Adapters whose cross-process ``session/load`` was exercised, by runtime id,
#: with the versions it was proved on. The A.3 captures (#152) show Claude
#: Code and Codex *declare* ``loadSession`` but never proved it, so they are
#: absent and resume through a brief.
PROVEN_LOAD: dict[str, frozenset[str]] = {}


class ResumeRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ResumePlan:
    source: str
    mode: str  # native | acp | brief
    runtime_id: str
    reason: str = ""
    native_session_id: str | None = None
    changes: tuple[str, ...] = field(default_factory=tuple)

    def record(self) -> dict:
        """The keys the new session records about where it came from."""
        out = {"resumed_from": self.source, "resume_mode": self.mode}
        if self.reason:
            out["resume_reason"] = self.reason
        if self.changes:
            out["link_reason"] = list(self.changes)
        return out


def _active_segment(meta: dict) -> dict:
    segments = meta.get("runtime_segments") or []
    if segments and isinstance(segments[-1], dict):
        return segments[-1]
    return {"runtime_id": "native", "kind": "native"}


def _manifest_version(catalog, runtime_id: str) -> str | None:
    try:
        return str(catalog.registry.get(runtime_id).version)
    except Exception:
        return None


def plan_resume(
    store,
    session_ref: str,
    *,
    workspace,
    all_projects: bool = False,
    runtime: str | None = None,
    as_runtime: str | None = None,
    model: str | None = None,
    agent: str | None = None,
    catalog=None,
) -> ResumePlan:
    """Decide how ``session_ref`` resumes. Refuses a live owner."""
    from garuda.runtime.session_state import effective_state

    try:
        source = store.resolve(session_ref, workspace=workspace, all_projects=all_projects)
    except (OSError, ValueError) as exc:
        raise ResumeRefused("session.not_found", str(exc)) from exc
    meta = store.load_meta(source)
    if effective_state(meta).get("process") == "live":
        raise ResumeRefused(
            "session.live_owner", f"session {source[:8]} is still running; stop it first"
        )
    active = _active_segment(meta)
    active_id = str(active.get("runtime_id") or "native")
    target = as_runtime or runtime or active_id
    changes = []
    if target != active_id:
        changes.append(f"runtime {active_id} -> {target}")
    if model and meta.get("model") and model != meta.get("model") and target == "native":
        changes.append(f"model {meta.get('model')} -> {model}")
    if agent and meta.get("agent") and agent != meta.get("agent") and target == "native":
        changes.append(f"agent {meta.get('agent')} -> {agent}")

    if target != active_id:
        return ResumePlan(source, "brief", target, reason="continued on another runtime",
                          changes=tuple(changes))
    if active.get("kind", "native") == "native":
        return ResumePlan(source, "native", "native", changes=tuple(changes))
    version = _manifest_version(catalog, active_id) if catalog is not None else None
    native_id = active.get("native_session_id")
    if version is None or version not in PROVEN_LOAD.get(active_id, frozenset()):
        reason = f"{active_id} resume is not proven for this adapter version"
    elif str(active.get("version")) != version:
        reason = f"{active_id} changed from {active.get('version')} to {version}"
    elif not native_id:
        reason = "the session recorded no agent session id"
    else:
        return ResumePlan(source, "acp", active_id, native_session_id=native_id)
    return ResumePlan(source, "brief", active_id, reason=reason)


def as_brief(plan: ResumePlan, reason: str) -> ResumePlan:
    """The same continuation through a brief (the adapter drifted at load)."""
    return ResumePlan(plan.source, "brief", plan.runtime_id, reason=reason, changes=plan.changes)
