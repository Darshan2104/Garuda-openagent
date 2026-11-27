"""The Flows page's reads: every available flow, its pipeline, readiness and start support.

Pure reads. Each flow resolves through the ``run-flow`` starter's own launch resolution
(``resolve_launch``), readiness and the pipeline projection, so what a card says is what
a preview and a start check. Nothing here compiles a task, admits an operation, probes a
runtime or writes: login readings are the recorded ones (``web.runtimes.login_checks``),
with their age.

One bad flow never blanks the library: a flow whose roles cannot be resolved is listed
with its typed error and a static pipeline, and a configuration file that cannot be
loaded leaves the packaged flows listed beside that error.

Payload (``GET /api/flows``)::

    {"workspace": PATH, "host": HOST, "config_error": {"code", "message"} | null,
     "flows": [FLOW, ...], "logins": {RUNTIME_ID: LOGIN_CHECK}}

    HOST = {"mode": "read-only" | "read-write", "start_available": bool,
            "platform": "linux" | <other>, "max_permission": str | null}
    FLOW = {"name", "description", "source": "packaged" | "user" | "project",
            "source_file", "trusted", "steps": int, "roles": [ROLE],
            "missing_roles": [ROLE], "error": {"code", "message", "fix"} | null,
            "readiness": READINESS | null,   # the run-flow readiness, owned when write mode
            "pipeline": PIPELINE,            # garuda.scenarios.pipeline
            "start": {"supported": bool, "reasons": [{"code", "message", "fix"}]}}

``GET /api/flows/<name>`` answers one ``FLOW`` with the same ``workspace``, ``host`` and
the logins of the runtimes it uses. ``start.supported`` is what the dashboard would allow
now; ``reasons`` name why not (read-only dashboard, no Linux supervisor, a flow shape the
owned engine refuses, a permission over the ceiling, readiness errors). Preview and Copy
work in every case.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

from garuda.config import garuda_yaml as gy
from garuda.interfaces.web.scenarios import _query, _query_workspace
from garuda.interfaces.web.wire import BadRequest
from garuda.scenarios.authority import flow_authority
from garuda.scenarios.catalog import load_catalog
from garuda.scenarios.compile import resolve_launch
from garuda.scenarios.service import RESOLUTION_ERRORS, readiness

logger = logging.getLogger(__name__)
STARTER = "run-flow"
_SOURCES = {gy.PACKAGE: "packaged", gy.USER: "user", gy.PROJECT: "project"}
#: The shapes the owned engine refuses, reported even when no gateway exists (read-only).
SHAPE_CODES = ("starter.parallel_ownership_unavailable", "starter.retries_unsupported",
               "starter.isolation_unsupported")
_NAME_FIX = "Review `garuda config show` and rerun `garuda init`."


def _host(ctx) -> dict:
    gateway = ctx.flow_starts
    return {"mode": "read-write" if ctx.allow_run else "read-only",
            "start_available": bool(ctx.capabilities.get("starter_start")),
            "platform": sys.platform,
            "max_permission": getattr(gateway, "max_permission", None)}


def _load(root):
    """The effective configuration, or the packaged flows alone beside the load error."""
    try:
        return gy.load_effective(root), None
    except gy.GarudaConfigError as exc:
        return None, {"code": getattr(exc, "code", "config.invalid"), "message": str(exc),
                      "fix": "Fix the named garuda.yaml value; `garuda config show` names it."}


def _available(resolved) -> dict:
    try:
        from garuda.flows import packaged

        return packaged.available(resolved)
    except Exception:  # noqa: BLE001 - the library lists what it can
        return {}


def _probe(name: str, root, context):
    """The fields of a plan the authority reads, without compiling a task."""
    return SimpleNamespace(kind="flow", target=name, workspace=str(root), starter_id=STARTER,
                           checks=[], provenance={"workspace_identity": None,
                                                  "flow_source": context.source})


def _start(ctx, name: str, root, context, state, failure=None) -> dict:
    """Whether the dashboard would start this flow now, and every reason it would not."""
    reasons: list[dict] = []

    def add(code, message, fix):
        if all(row["code"] != code for row in reasons):
            reasons.append({"code": code, "message": message, "fix": fix})

    gateway = ctx.flow_starts
    if not ctx.allow_run or gateway is None:
        add("dashboard.read_only", "This dashboard was started read-only.",
            "Start `garuda web` without --read-only to start flows here; copy the command meanwhile.")
    if context is not None:
        if gateway is not None:
            authority = flow_authority(
                _probe(name, root, context), context, max_permission=gateway.max_permission,
                server_epoch=gateway.server_epoch, backend_available=gateway.backend_available,
                approval_available=gateway.approvals is not None)
            diagnostics = authority["diagnostics"]
        else:
            shapes = flow_authority(_probe(name, root, context), context, max_permission="yolo",
                                    server_epoch="", backend_available=True,
                                    approval_available=True)
            diagnostics = [row for row in shapes["diagnostics"] if row["code"] in SHAPE_CODES]
        for row in diagnostics:
            add(row["code"], row["message"], row["fix"])
    if state is not None:
        for row in state.get("diagnostics", []):
            if row.get("level") == "error":
                add(row["code"], row["message"], row["fix"])
    elif context is None:
        failure = failure or {"code": "flow.not_resolved",
                              "message": "The flow's roles could not be resolved.", "fix": _NAME_FIX}
        add(failure["code"], failure["message"], failure["fix"])
    return {"supported": not reasons, "reasons": reasons}


def _row(ctx, entry, root, name: str, flow: dict, source: str, resolved) -> dict:
    from garuda.scenarios.pipeline import project, project_static

    roles, missing = _roles(flow, resolved)
    row = {"name": name, "description": flow.get("description"),
           "source": _SOURCES.get(source, source), "source_file": None, "trusted": None,
           "steps": len(flow.get("steps", [])), "roles": roles, "missing_roles": missing,
           "error": None, "readiness": None}
    context = state = None
    try:
        context = resolve_launch(entry, {"flow": name}, root)
        state = readiness(context, starter_id=STARTER, owned=ctx.flow_starts is not None)
        row["pipeline"] = project(context)
        row["readiness"] = state
    except Exception as exc:  # noqa: BLE001 - one bad flow never blanks the library
        if not isinstance(exc, RESOLUTION_ERRORS):
            logger.warning("Flow %s could not be resolved", name, exc_info=True)
        row["error"] = {"code": getattr(exc, "code", "flow.not_resolved"), "message": str(exc),
                        "fix": (f"Define {', '.join(missing)} in your garuda.yaml (`garuda init` proposes them)."
                                if missing else _NAME_FIX)}
        context = state = None
        row["pipeline"] = _static(project_static, name, flow, source, resolved, root)
    origin = (context.flow_origin if context is not None else None) or _origin(source, root)
    row["source_file"], row["trusted"] = origin.get("file"), origin.get("trusted")
    row["start"] = _start(ctx, name, root, context, state, row["error"])
    return row


def _origin(source: str, root) -> dict:
    """A flow's defining file and trust, read only, for a flow that did not resolve."""
    try:
        from garuda.scenarios.compile import _selection_facts

        return _selection_facts(None, ({}, source), root)["flow_origin"]
    except Exception:  # noqa: BLE001 - unknown stays unknown
        return {"file": None, "trusted": None}


def _static(project_static, name, flow, source, resolved, root) -> dict:
    try:
        return project_static(name, flow, source, resolved, origin=_origin(source, root))
    except Exception as exc:  # noqa: BLE001 - one broken flow never blanks the library
        return {"version": 1, "flow": name, "source": _SOURCES.get(source, source),
                "steps": [], "edges": [],
                "unknown": [{"path": "steps", "reason": f"the pipeline could not be read: {exc}"}]}


def _roles(flow: dict, resolved) -> tuple[list[str], list[str]]:
    try:
        from garuda.flows import packaged

        return packaged.required_roles(flow), packaged.missing_roles(flow, resolved)
    except Exception:  # noqa: BLE001
        return [], []


def _logins(ctx, root, runtime_ids: set[str]) -> dict:
    from garuda.interfaces.web.runtimes import login_checks

    try:
        checks = login_checks(ctx.extra, str(root))
    except Exception:  # noqa: BLE001 - a login reading is advice, never a blocker here
        return {}
    return {rid: check for rid, check in checks.items() if rid in runtime_ids}


def _runtimes(rows: list[dict]) -> set[str]:
    out = set()
    for row in rows:
        for step in (row.get("pipeline") or {}).get("steps", []):
            for member in step.get("members", []):
                if member.get("runtime_id"):
                    out.add(member["runtime_id"])
                for candidate in member.get("fallbacks", []):
                    if candidate.get("runtime_id"):
                        out.add(candidate["runtime_id"])
    return out


def _rows(ctx, root, only: str | None = None):
    entry = load_catalog()[STARTER]
    resolved, config_error = _load(root)
    rows = []
    for name, (flow, source) in sorted(_available(resolved).items()):
        if only is not None and name != only:
            continue
        rows.append(_row(ctx, entry, root, name, flow, source, resolved))
    return rows, config_error


def library(request, ctx) -> dict:
    _query(request, {"workspace"})
    root = _query_workspace(request, ctx)
    rows, config_error = _rows(ctx, root)
    return {"workspace": str(root), "host": _host(ctx), "config_error": config_error,
            "flows": rows, "logins": _logins(ctx, root, _runtimes(rows))}


class FlowNotFound(LookupError):
    """No available flow has this name in the selected workspace (a 404)."""


def detail(request, ctx, name: str) -> dict:
    _query(request, {"workspace"})
    root = _query_workspace(request, ctx)
    rows, config_error = _rows(ctx, root, only=name)
    if not rows:
        raise FlowNotFound(name)
    if len(rows) != 1:  # pragma: no cover - names are unique keys
        raise BadRequest("Ambiguous flow name.")
    return {"workspace": str(root), "host": _host(ctx), "config_error": config_error,
            "flow": rows[0], "logins": _logins(ctx, root, _runtimes(rows))}
