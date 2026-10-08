"""Read-only starter HTTP boundary; compilation and results keep their owners."""

from __future__ import annotations

from dataclasses import asdict

from garuda.context.tags import TagError
from garuda.core.project_identity import ProjectIdentityError
from garuda.core.session_records import parse_json
from garuda.core.sessions import validate_session_ref
from garuda.interfaces.web.wire import error, ok
from garuda.scenarios.catalog import example_inputs, load_catalog
from garuda.scenarios.service import RESOLUTION_ERRORS, StarterService
from garuda.scenarios.types import StarterError


def _query(request, allowed):
    if set(request.query) - set(allowed) or any(len(v) != 1 for v in request.query.values()):
        raise ValueError("Use only supported, single-valued query parameters.")


def _workspace(ctx, index):
    workspaces = ctx.workspaces or (ctx.workspace,)
    if type(index) is not int or not 0 <= index < len(workspaces):
        raise ValueError("workspace must be an index into the configured workspaces.")
    root = workspaces[index]
    if root.resolve() != root:
        raise ValueError("The configured workspace changed; restart the dashboard.")
    return root


def _query_workspace(request, ctx):
    raw = request.first("workspace", "0")
    if not raw.isdecimal():
        raise ValueError("workspace must be an index into the configured workspaces.")
    return _workspace(ctx, int(raw))


def respond(operation, *args):
    """Keep typed source/trust refusals; never turn one into launch authority."""
    try:
        return ok(operation(*args))
    except TagError as exc:
        return error(exc.code, str(exc), status=403)
    except RESOLUTION_ERRORS + (ProjectIdentityError,) as exc:
        return error(getattr(exc, "code", "invalid_request"), str(exc), status=400)


def library(request, ctx):
    _query(request, {"workspace"})
    root = _query_workspace(request, ctx)
    return {"starters": StarterService(ctx.store).list(root), "workspace": str(root)}


def detail(request, ctx, starter_id):
    _query(request, {"workspace"})
    root = _query_workspace(request, ctx)
    entries = load_catalog()
    if starter_id not in entries:
        raise StarterError("starter.unknown", "select an installed starter")
    entry = entries[starter_id]
    row = next(row for row in StarterService(ctx.store).list(root) if row["id"] == starter_id)
    return {**asdict(entry), "readiness": row["readiness"], "example_inputs": example_inputs(entry)}


def preview(request, ctx):
    _query(request, set())
    body = parse_json(request.body or b"{}")
    allowed = {"starter_id", "inputs", "workspace", "remedy", "checks"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError("Preview needs starter_id, inputs and an optional configured workspace index.")
    if not isinstance(body.get("starter_id"), str) or not isinstance(body.get("inputs"), dict):
        raise ValueError("Preview needs an installed starter_id and an inputs object.")
    root = _workspace(ctx, body.get("workspace", 0))
    service = StarterService(ctx.store)
    if "remedy" in body or "checks" in body:
        if body.get("remedy") != "build-and-check" or body["starter_id"] != "build-review":
            raise ValueError("Select Build and check explicitly for the build-review starter.")
        return service.build_and_check(body["inputs"], root, checks=body.get("checks"))
    # Read-only preview cannot grant or persist cross-project authorization.
    return service.preview(body["starter_id"], body["inputs"], root)


def result(request, ctx, session_id):
    _query(request, {"workspace", "limit", "offset"})
    root = _query_workspace(request, ctx)
    limit, offset = request.int_param("limit", 50), request.int_param("offset", 0)
    if limit is None or offset is None or not 1 <= limit <= 200:
        raise ValueError("Result pages need limit 1..200 and a non-negative offset.")
    return StarterService(ctx.store).result(validate_session_ref(session_id), workspace=root,
                                             limit=limit, offset=offset)
