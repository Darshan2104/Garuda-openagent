"""The dashboard's route table.

``dispatch`` is a pure function of a :class:`~garuda.interfaces.web.wire.Request` and
a context object, so every route is testable by calling it — no socket, no thread, no
server lifecycle. ``http.py`` is the only thing that knows about sockets.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import garuda
from garuda.agents.loader import list_profiles, load_profile
from garuda.core.modes import GATE_FIELDS, MODE_CHOICES, MODE_PRESETS
from garuda.core.sessions import SessionStore
from garuda.interfaces.web import live as live_module
from garuda.interfaces.web import reads
from garuda.interfaces.web.grounding import SourceError
from garuda.interfaces.web.security import check_request
from garuda.interfaces.web.wire import (
    CODE_READ_ONLY,
    Request,
    Response,
    error,
    invalid,
    not_found,
    ok,
)
from garuda.model.protocol import DEFAULT_MODEL

logger = logging.getLogger(__name__)


@dataclass
class DashboardContext:
    """Everything a route needs, resolved once at startup."""

    port: int
    token: str
    store: SessionStore
    allow_run: bool = False
    agents_dir: Path | None = None
    #: Set by http.py once the static directory is known, so routes need no path logic.
    static_dir: Path | None = None
    #: Warm trajectory readers, so polling an open run tails bytes instead of re-reading
    #: the whole log. Shared across HTTP threads; the cache owns the locking.
    readers: reads.ReaderCache = field(default_factory=reads.ReaderCache)
    #: The agent loop, and the write-mode surface that runs on it. Both None in read-only
    #: mode, which is why every write route checks for them rather than assuming.
    loop: Any = None
    live: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def capabilities(self) -> dict[str, bool]:
        """What the UI is allowed to offer. It hides controls off this rather than
        showing buttons that 503.

        ``chat`` needs both the flag *and* an attached loop: a read-write dashboard with no
        agent loop would advertise a conversation that could never take a turn.
        """
        return {"chat": self.allow_run and self.live is not None}


Handler = Callable[[Request, DashboardContext, "re.Match[str]"], Response]

_ROUTES: list[tuple[str, re.Pattern[str], Handler]] = []


def route(method: str, pattern: str):
    """Register a handler. Patterns are anchored so a prefix cannot match by accident."""

    def register(func: Handler) -> Handler:
        _ROUTES.append((method, re.compile(f"^{pattern}$"), func))
        return func

    return register


def dispatch(request: Request, ctx: DashboardContext) -> Response:
    refusal = check_request(request, port=ctx.port, token=ctx.token)
    if refusal is not None:
        return refusal

    matched_path = False
    for method, pattern, handler in _ROUTES:
        match = pattern.match(request.path)
        if not match:
            continue
        matched_path = True
        if method != request.method:
            continue
        try:
            return handler(request, ctx, match)
        except ValueError as exc:
            # The validators (`validate_session_ref`, the `?file=` enum) signal a bad
            # request this way, and a bad request is a 400, not a 500.
            return invalid(str(exc))
        except Exception:
            logger.exception("Dashboard route %s %s failed", request.method, request.path)
            return error("unavailable", "The dashboard failed to handle this request.", status=500)
    if matched_path:
        return error(
            "method_not_allowed", f"{request.method} is not supported for this path.", status=405
        )
    return not_found(f"No route for {request.path}.")


def requires_write(ctx: DashboardContext) -> Response | None:
    """Write mode is opt-in; without it these routes do not exist in effect."""
    if ctx.allow_run:
        return None
    return error(
        CODE_READ_ONLY,
        "This dashboard was started with `--read-only`, so talking to an agent is disabled. "
        "Relaunch `garuda web` without it.",
        status=503,
    )


# --- health and configuration ------------------------------------------------


@route("GET", r"/api/health")
def _health(request: Request, ctx: DashboardContext, _match) -> Response:
    return ok(
        {
            "status": "ok",
            "version": garuda.__version__,
            "mode": "read-write" if ctx.allow_run else "read-only",
            "capabilities": ctx.capabilities,
            "sessions_root": str(ctx.store.root),
            "port": ctx.port,
        }
    )


@route("GET", r"/api/config")
def _config(request: Request, ctx: DashboardContext, _match) -> Response:
    """What the chat composer needs to offer exactly what will be accepted.

    Modes and gate fields are served from ``core/modes.py`` itself rather than a copied
    list, so the UI cannot describe a posture the harness does not have.
    """
    models = sorted(
        {meta.get("model") for meta in ctx.store.list_sessions(limit=reads.MAX_LIMIT)}
        - {None, ""}
    )
    return ok(
        {
            "default_model": DEFAULT_MODEL,
            # There is no model catalog anywhere in the repo — models are free-text
            # litellm strings — so the UI offers what this machine has actually run.
            "recent_models": models,
            "modes": list(MODE_CHOICES),
            "gate_fields": list(GATE_FIELDS),
            "presets": {name: dict(preset) for name, preset in MODE_PRESETS.items()},
            "agents": list_profiles(extra_dir=ctx.agents_dir),
            "default_agent": ctx.live.default_agent if ctx.live else None,
            # Write mode's two rules, published so the composer offers exactly what will be
            # accepted instead of letting the user discover the ceiling via a 400.
            "workspaces": [
                {"index": index, "path": str(path)}
                for index, path in enumerate(ctx.live.workspaces if ctx.live else ())
            ],
            "max_permission": ctx.live.max_permission if ctx.live else None,
            "permission_modes": [
                name
                for name, rank in sorted(live_module.PERMISSION_RANK.items(), key=lambda x: x[1])
                if ctx.live is None
                or rank <= live_module.PERMISSION_RANK.get(ctx.live.max_permission, 1)
            ],
        }
    )


@route("GET", r"/api/agents")
def _agents(request: Request, ctx: DashboardContext, _match) -> Response:
    return ok({"agents": list_profiles(extra_dir=ctx.agents_dir)})


@route("GET", r"/api/agents/(?P<name>[A-Za-z0-9._-]+)")
def _agent_detail(request: Request, ctx: DashboardContext, match) -> Response:
    try:
        profile = load_profile(match["name"], extra_dir=ctx.agents_dir)
    except (FileNotFoundError, ValueError) as exc:
        return not_found(str(exc))
    return ok(
        {
            "name": profile.name,
            "description": getattr(profile, "description", None),
            "mode": getattr(profile, "mode", None),
            "permission_mode": getattr(profile, "permission_mode", None),
            "tools": getattr(profile, "tools", None),
            "declared_fields": sorted(getattr(profile, "declared_fields", set()) or set()),
        }
    )


# --- runs --------------------------------------------------------------------


@route("GET", r"/api/runs")
def _runs(request: Request, ctx: DashboardContext, _match) -> Response:
    limit = request.int_param("limit", reads.DEFAULT_LIMIT)
    if limit is None:
        return invalid("`limit` must be a non-negative integer.")
    return ok(
        reads.list_runs(
            ctx.store,
            limit=limit,
            status=request.first("status"),
            agent=request.first("agent"),
            model=request.first("model"),
            query=request.first("q"),
        )
    )


@route("GET", r"/api/runs/(?P<sid>[^/]+)")
def _run_detail(request: Request, ctx: DashboardContext, match) -> Response:
    payload = reads.read_run(ctx.store, match["sid"], readers=ctx.readers)
    if payload is None:
        return not_found(f"No session {match['sid']!r}.")
    return ok(payload)


@route("GET", r"/api/runs/(?P<sid>[^/]+)/events")
def _run_events(request: Request, ctx: DashboardContext, match) -> Response:
    """The raw-event escape hatch: a window of the log by index."""
    start = request.int_param("start", 0)
    limit = request.int_param("limit", reads.MAX_EVENT_WINDOW)
    if start is None or limit is None:
        return invalid("`start` and `limit` must be non-negative integers.")
    payload = reads.read_events(
        ctx.store, match["sid"], start=start, limit=limit, readers=ctx.readers
    )
    if payload is None:
        return not_found(f"No session {match['sid']!r}.")
    return ok(payload)


@route("GET", r"/api/runs/(?P<sid>[^/]+)/raw")
def _run_raw(request: Request, ctx: DashboardContext, match) -> Response:
    name = request.first("file")
    document = reads.read_raw(ctx.store, match["sid"], name)
    if document is None:
        return not_found(f"No readable {name} for session {match['sid']!r}.")
    return ok({"file": name, "content": document})


@route("GET", r"/api/runs/(?P<sid>[^/]+)/subagents/(?P<sub>[^/]+)")
def _run_subagent(request: Request, ctx: DashboardContext, match) -> Response:
    """One subagent's own trajectory, nested under the run that invoked it.

    A subagent runs on a **separate** ``EventStore`` — its turns are not interleaved into the
    parent's log, which is what keeps the parent's turn segmentation honest. So it is a
    separate document, addressed by the sub-session id the parent recorded, and read by the
    same reader as everything else.
    """
    payload = reads.read_subagent(ctx.store, match["sid"], match["sub"], readers=ctx.readers)
    if payload is None:
        return not_found(f"No subagent log {match['sub']!r} under session {match['sid']!r}.")
    return ok(payload)


@route("GET", r"/api/runs/(?P<sid>[^/]+)/tail")
def _run_tail(request: Request, ctx: DashboardContext, match) -> Response:
    """One poll of a live run, addressed by **byte** offset.

    Deliberately a different route from ``/events``, which is index-addressed. A cursor
    needs bytes — that is what ``seek`` takes, and re-deriving it from an index would mean
    re-reading the file every poll. A cross-reference needs indices, because that is what
    ``Turn.events`` holds. Serving both from one parameter would make one of the two wrong.
    """
    offset = request.int_param("offset", 0)
    if offset is None:
        return invalid("`offset` must be a non-negative integer.")
    payload = reads.tail_run(ctx.store, match["sid"], offset=offset)
    if payload is None:
        return not_found(f"No session {match['sid']!r}.")
    return ok(payload)


@route("GET", r"/api/runs/(?P<sid>[^/]+)/buffers")
def _run_buffers(request: Request, ctx: DashboardContext, match) -> Response:
    return ok({"buffers": reads.list_buffers(ctx.store, match["sid"])})


@route("GET", r"/api/runs/(?P<sid>[^/]+)/buffers/(?P<bid>[A-Za-z0-9._-]+)")
def _run_buffer(request: Request, ctx: DashboardContext, match) -> Response:
    payload = reads.read_buffer(ctx.store, match["sid"], match["bid"])
    if payload is None:
        return not_found(f"No buffer {match['bid']!r}.")
    return ok(payload)


# --- talking to an agent ------------------------------------------------------
#
# Every route below 503s under `--read-only`. The transport gate (Host, Origin on
# state-changing methods, no CORS, token) is unchanged from the read-only surface — but
# from here it is the only thing between a page the developer visits and code running as
# them, which is why the module-level rules W1 (workspace allowlist) and W2 (permission
# ceiling) exist on top of it.


def _live(ctx: DashboardContext) -> Response | None:
    """The write-mode preconditions, in one place."""
    refusal = requires_write(ctx)
    if refusal is not None:
        return refusal
    if ctx.live is None or ctx.loop is None:
        return error(
            "unavailable",
            "Talking to an agent is enabled but no agent loop is attached to this dashboard.",
            status=503,
        )
    return None


@route("GET", r"/api/jobs/(?P<job_id>[A-Za-z0-9]+)")
def _job(request: Request, ctx: DashboardContext, match) -> Response:
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    payload = live_module.call_on_loop(ctx.loop, ctx.live.get_job(match["job_id"]))
    if payload is None:
        return _job_expired(match["job_id"])
    return ok(payload)


@route("POST", r"/api/jobs/(?P<job_id>[A-Za-z0-9]+)/cancel")
def _cancel_job(request: Request, ctx: DashboardContext, match) -> Response:
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    payload = live_module.call_on_loop(ctx.loop, ctx.live.cancel_job(match["job_id"]))
    if payload is None:
        return _job_expired(match["job_id"])
    return ok(payload)


# --- write mode: chat and approvals ------------------------------------------


@route("POST", r"/api/chat")
def _start_chat(request: Request, ctx: DashboardContext, _match) -> Response:
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    try:
        spec = live_module.ChatSpec.parse(request.json_body())
        payload = live_module.call_on_loop(ctx.loop, ctx.live.start_chat(spec))
    except live_module.SpecError as exc:
        return invalid(str(exc))
    except TimeoutError:
        return error("unavailable", "The agent loop did not open the chat in time.", status=504)
    return ok(payload, status=201)


@route("GET", r"/api/chat")
def _chats(request: Request, ctx: DashboardContext, _match) -> Response:
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    return ok(live_module.call_on_loop(ctx.loop, ctx.live.list_chats()))


@route("GET", r"/api/chat/(?P<chat_id>[A-Za-z0-9]+)")
def _chat(request: Request, ctx: DashboardContext, match) -> Response:
    """Also the heartbeat. Any request naming a chat marks it seen, which is what stops the
    reaper denying its pending approvals while someone is still watching."""
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    payload = live_module.call_on_loop(ctx.loop, ctx.live.touch_chat(match["chat_id"]))
    if payload is None:
        return not_found(f"No chat {match['chat_id']!r}.")
    return ok(payload)


@route("POST", r"/api/chat/(?P<chat_id>[A-Za-z0-9]+)/turn")
def _chat_turn(request: Request, ctx: DashboardContext, match) -> Response:
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    body = request.json_body()
    if not isinstance(body, dict):
        return invalid("A JSON object body is required.")
    try:
        payload = live_module.call_on_loop(
            ctx.loop, ctx.live.chat_turn(match["chat_id"], body.get("task") or "")
        )
    except live_module.BusyError as exc:
        # 409, not 400: the request is well-formed and will succeed when the current turn
        # finishes, which is a different thing for a client to do about it.
        return error("turn_in_flight", str(exc), status=409)
    except live_module.SpecError as exc:
        return invalid(str(exc))
    except TimeoutError:
        return error("unavailable", "The agent loop did not accept the turn in time.", status=504)
    if payload is None:
        return not_found(f"No chat {match['chat_id']!r}.")
    return ok(payload, status=202)


@route("POST", r"/api/chat/(?P<chat_id>[A-Za-z0-9]+)/stop")
def _chat_stop(request: Request, ctx: DashboardContext, match) -> Response:
    """Interrupt the turn in flight without ending the conversation.

    Separate from ``DELETE``: stopping a turn that has gone off the rails is the common
    case, and having to throw away the workspace and the whole conversation to do it would
    make the button useless. Cancellation propagates through ``JobManager`` to the awaiting
    agent loop, and an approval parked at that moment unparks on its own — the handler's
    ``finally`` does it, so there is nothing extra to clean up here.
    """
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    payload = live_module.call_on_loop(ctx.loop, ctx.live.stop_turn(match["chat_id"]))
    if payload is None:
        return not_found(f"No chat {match['chat_id']!r}.")
    return ok(payload)


@route("GET", r"/api/chat/(?P<chat_id>[A-Za-z0-9]+)/sources")
def _chat_sources(request: Request, ctx: DashboardContext, match) -> Response:
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    payload = live_module.call_on_loop(ctx.loop, ctx.live.list_sources(match["chat_id"]))
    if payload is None:
        return not_found(f"No chat {match['chat_id']!r}.")
    return ok(payload)


@route("POST", r"/api/chat/(?P<chat_id>[A-Za-z0-9]+)/sources")
def _chat_add_source(request: Request, ctx: DashboardContext, match) -> Response:
    """Ground the conversation in a document or a web page.

    Both kinds land as a **file in the chat's workspace**, and the next turn's task names
    them. Nothing here injects text into the model's context directly: the agent already has
    ``read_file``, ``read_pdf`` and ``read_spreadsheet``, and a source it reads with its own
    tools is one it can re-read, quote a line from and cite — where a blob pasted into the
    prompt is a thing it has to hold in context forever whether or not it is relevant.

    A URL is fetched **server-side through the same SSRF-vetted fetcher the ``web_fetch``
    tool uses**, so the dashboard cannot be turned into a proxy for reaching things the
    agent's own tool is not allowed to reach.
    """
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    body = request.json_body()
    if not isinstance(body, dict):
        return invalid("A JSON object body is required.")
    try:
        payload = live_module.call_on_loop(
            ctx.loop, ctx.live.add_source(match["chat_id"], body), timeout=90.0
        )
    except live_module.SpecError as exc:
        return invalid(str(exc))
    except SourceError as exc:
        # The request was well-formed and the fetch or the decode failed. 502, because the
        # failure is upstream of this dashboard, and the message says which URL.
        return error("source_unavailable", str(exc), status=502)
    except TimeoutError:
        return error("unavailable", "Fetching that source took too long.", status=504)
    if payload is None:
        return not_found(f"No chat {match['chat_id']!r}.")
    return ok(payload, status=201)


@route("DELETE", r"/api/chat/(?P<chat_id>[A-Za-z0-9]+)")
def _close_chat(request: Request, ctx: DashboardContext, match) -> Response:
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    payload = live_module.call_on_loop(ctx.loop, ctx.live.close_chat(match["chat_id"]))
    if payload is None:
        return not_found(f"No chat {match['chat_id']!r}.")
    return ok(payload)


@route("GET", r"/api/approvals")
def _approvals(request: Request, ctx: DashboardContext, _match) -> Response:
    """Pending asks, and the heartbeat that keeps them alive.

    Polling this *is* the heartbeat: an owner that stops polling has its pending asks denied
    within the grace window. That is what makes closing a tab mid-approval safe rather than
    leaving a run — and its container — wedged for the full timeout.
    """
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    owner = request.first("owner")
    if owner:
        ctx.live.approvals.touch(owner)
    # From the broker instance, not the module defaults: a dashboard constructed with
    # different timings would otherwise publish numbers it does not honour, and the client
    # sets its poll interval from these.
    return ok({
        "approvals": ctx.live.approvals.pending(owner),
        "timeout_seconds": ctx.live.approvals.timeout_seconds,
        "grace_seconds": ctx.live.approvals.grace_seconds,
    })


@route("POST", r"/api/approvals/(?P<ask_id>[A-Za-z0-9]+)")
def _resolve_approval(request: Request, ctx: DashboardContext, match) -> Response:
    refusal = _live(ctx)
    if refusal is not None:
        return refusal
    body = request.json_body()
    if not isinstance(body, dict) or not isinstance(body.get("approved"), bool):
        return invalid("`approved` must be true or false.")
    outcome = ctx.live.approvals.resolve_from_thread(
        match["ask_id"], body["approved"], loop=ctx.loop
    )
    if outcome == "unknown":
        return not_found(f"No pending approval {match['ask_id']!r}.")
    if outcome == "already_resolved":
        # Answered by the other tab, the timeout, or the reaper. A 409 rather than a 200,
        # because the caller's answer is not what the agent acted on.
        return error(
            "already_resolved",
            "That approval was already answered — by a timeout, the reaper, or another tab.",
            status=409,
        )
    return ok({"ask_id": match["ask_id"], "approved": body["approved"], "state": outcome})


def _job_expired(job_id: str) -> Response:
    """410, not 404.

    ``JobManager`` cannot distinguish an evicted job from one that never existed, but the
    browser still holds the ``session_id`` it got at submit time and the on-disk session
    lives forever. So the honest answer is "this handle is gone, the run is not" — and the
    UI's response is to switch to ``/api/runs/<session_id>`` rather than show an error.
    Eviction becomes invisible.
    """
    return error(
        "expired",
        f"Job {job_id!r} is no longer held in memory. The run itself is still readable at "
        f"/api/runs/<session_id>.",
        status=410,
    )
