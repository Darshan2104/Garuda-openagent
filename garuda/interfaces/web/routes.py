"""The dashboard's route table.

``dispatch`` is a pure function of a :class:`~garuda.interfaces.web.wire.Request` and
a context object, so every route is testable by calling it — no socket, no thread, no
server lifecycle. ``http.py`` is the only thing that knows about sockets.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import garuda
from garuda.agents.loader import list_profiles, load_profile
from garuda.core import read_model
from garuda.core.modes import GATE_FIELDS, MODE_CHOICES, MODE_PRESETS
from garuda.core.sessions import SessionStore, validate_session_ref
from garuda.interfaces.web import live as live_module
from garuda.interfaces.web import reads
from garuda.interfaces.web.grounding import SourceError
from garuda.interfaces.web.security import check_request
from garuda.interfaces.web.tail import tail_records
from garuda.interfaces.web.wire import (
    CODE_EXPIRED,
    CODE_READ_ONLY,
    Request,
    Response,
    error,
    invalid,
    not_found,
    ok,
)
from garuda.model.protocol import DEFAULT_MODEL
from garuda.runtime import RegistryError

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
    workspace: Path = field(default_factory=lambda: Path.cwd())

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


def _queue_or_none():
    from garuda.runtime.queue import QueueStore

    try:
        return QueueStore()
    except Exception:
        return None


@route("GET", r"/api/sessions")
def _sessions(request: Request, ctx: DashboardContext, _match) -> Response:
    """The shared read model (D.3): the same rows ``garuda sessions --json`` prints."""
    limit = request.int_param("limit", reads.DEFAULT_LIMIT)
    if limit is None:
        return invalid("`limit` must be a non-negative integer.")
    return ok({"sessions": read_model.sessions(ctx.store, limit=min(limit, reads.MAX_LIMIT),
                                               queue=_queue_or_none())})


@route("GET", r"/api/sessions/(?P<sid>[^/]+)")
def _session(request: Request, ctx: DashboardContext, match) -> Response:
    try:
        return ok(read_model.session(ctx.store, validate_session_ref(match["sid"]),
                                     queue=_queue_or_none()))
    except (FileNotFoundError, ValueError):
        return not_found(f"No session {match['sid']!r}.")


@route("GET", r"/api/inbox")
def _inbox(request: Request, ctx: DashboardContext, _match) -> Response:
    """Pending approvals across active sessions, with the digest each answer must bind."""
    return ok({"approvals": read_model.inbox(ctx.store)})


_APPROVAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@route("POST", r"/api/sessions/(?P<sid>[^/]+)/approvals/(?P<aid>[^/]+)")
def _answer_approval(request: Request, ctx: DashboardContext, match) -> Response:
    """Answer one session's parked approval through its file channel (B.8).

    The inbox never answers a runtime: it writes the session-bound answer file and the
    broker, the one place a decision is made, validates it (session, request digest,
    nonce, expiry, permission ceiling) and records the single decision. So a 200 here
    means "the answer was recorded for the broker", not "the tool will run". The page must
    send the request digest it was shown; a request that has since changed refuses.
    """
    from garuda.acp.approval_channel import (
        AnswerRefused,
        RequestExpired,
        StaleRequest,
        write_answer,
    )

    refusal = requires_write(ctx)
    if refusal is not None:
        return refusal
    try:
        session_id = validate_session_ref(match["sid"])
    except ValueError:
        return not_found("No such session.")
    approval_id = match["aid"]
    body = request.json_body()
    if not _APPROVAL_ID.match(approval_id):
        return invalid("Not an approval id.")
    if (not isinstance(body, dict) or not isinstance(body.get("allow"), bool)
            or not isinstance(body.get("digest"), str) or not _DIGEST.match(body["digest"])):
        return invalid("`allow` (true or false) and the request `digest` you were shown are required.")
    directory = ctx.store.session_dir(session_id) / "approvals"
    try:
        write_answer(directory, session_id, approval_id, body["allow"],
                     expect_digest=body["digest"])
    except FileNotFoundError:
        return not_found("No such pending approval.")
    except FileExistsError:
        # One winner: a terminal answer, another tab or an earlier click got there first.
        return error("already_answered", "That approval already has an answer or a decision.",
                     status=409)
    except StaleRequest:
        return error("stale_request", "That request changed since the page loaded; reload it.",
                     status=409)
    except RequestExpired:
        return error(CODE_EXPIRED, "That approval expired and will be denied.", status=410)
    except (AnswerRefused, OSError, ValueError) as exc:
        return error("refused", f"The answer was not recorded: {exc}", status=409)
    return ok({"session_id": session_id, "approval_id": approval_id, "allow": body["allow"],
               "state": "answer_recorded"})


@route("GET", r"/api/sessions/(?P<sid>[^/]+)/conversation")
def _conversation(request: Request, ctx: DashboardContext, match) -> Response:
    """Models used, lanes and linked sessions for one conversation (F.1)."""
    from garuda.core import conversation
    from garuda.observability.ledger import Ledger

    try:
        session_id = validate_session_ref(match["sid"])
        return ok(conversation.conversation(ctx.store, session_id, ledger=Ledger(),
                                            queue=_queue_or_none()))
    except (FileNotFoundError, ValueError):
        return not_found(f"No session {match['sid']!r}.")


@route("POST", r"/api/sessions/(?P<sid>[^/]+)/cancel")
def _cancel_session(request: Request, ctx: DashboardContext, match) -> Response:
    """Stop a background session: leave the queue, or stop its worker (identity-checked)."""
    from garuda.interfaces.bg_sessions import BackgroundRefused, cancel

    refusal = requires_write(ctx)
    if refusal is not None:
        return refusal
    try:
        session_id = validate_session_ref(match["sid"])
        message = cancel(ctx.store, session_id, queue=_queue_or_none())
    except (FileNotFoundError, ValueError):
        return not_found(f"No session {match['sid']!r}.")
    except BackgroundRefused as exc:
        return error("not_background", str(exc), status=409)
    return ok({"session_id": session_id, "result": message})


STREAM_MAX_SECONDS = 60.0
STREAM_HEARTBEAT_SECONDS = 15.0
STREAM_POLL_SECONDS = 0.5


def sse_frames(store, session_id: str, offset: int, *, max_seconds: float = STREAM_MAX_SECONDS,
               poll: float = STREAM_POLL_SECONDS, heartbeat: float = STREAM_HEARTBEAT_SECONDS,
               sleep=None, clock=None):
    """Server-sent frames for one session's event log, resumable by byte offset.

    Each event frame's ``id`` is the offset just after its line, so ``Last-Event-ID``
    resumes with the next event: nothing repeats and nothing is skipped. The stream ends
    with an ``end`` frame once the session is finished and its log is drained, and after
    ``max_seconds`` regardless (the client reconnects with its last id)."""
    import time as _time

    from garuda.runtime.session_state import ACTIVE_WORK, effective_state

    sleep = sleep or _time.sleep
    clock = clock or _time.monotonic
    path = store.events_path(session_id)
    started = last_sent = clock()
    while True:
        records, offset = tail_records(path, offset)
        for end, event in records:
            yield f"id: {end}\nevent: event\ndata: {json.dumps(event, default=str)}\n\n".encode()
            last_sent = clock()
        if not records:
            try:
                finished = effective_state(store.load_meta(session_id)).get("work") \
                    not in ACTIVE_WORK
            except (OSError, ValueError):
                finished = True
            if finished:
                yield f"id: {offset}\nevent: end\ndata: {{}}\n\n".encode()
                return
        now = clock()
        if now - started >= max_seconds:
            return
        if now - last_sent >= heartbeat:
            yield b": keep-alive\n\n"
            last_sent = now
        if not records:
            sleep(poll)


@route("GET", r"/api/sessions/(?P<sid>[^/]+)/stream")
def _session_stream(request: Request, ctx: DashboardContext, match) -> Response:
    try:
        session_id = validate_session_ref(match["sid"])
    except ValueError:
        return not_found("No such session.")
    if not ctx.store.session_dir(session_id).is_dir():
        return not_found(f"No session {session_id!r}.")
    raw = request.headers.get("last-event-id") or request.first("last_event_id") or "0"
    try:
        offset = int(raw)
    except ValueError:
        return invalid("`Last-Event-ID` must be a byte offset from an earlier frame.")
    if offset < 0:
        return invalid("`Last-Event-ID` must be a byte offset from an earlier frame.")
    return Response(content_type="text/event-stream; charset=utf-8",
                    stream=sse_frames(ctx.store, session_id, offset))


@route("GET", r"/api/memory/proposals")
def _memory_proposals(request: Request, ctx: DashboardContext, _match) -> Response:
    """Pending note proposals, read-only. Accepting happens only at a terminal
    (`garuda memory review`); this route cannot change anything."""
    from garuda.context.notes import ProposalStore

    try:
        pending = ProposalStore(ctx.store, ctx.workspace).pending()
    except Exception as exc:
        return invalid(f"The proposal store is unavailable: {exc}")
    return ok({"proposals": [
        {"id": p.id, "scope": p.scope, "text": p.text, "session_id": p.session_id,
         "state": p.state, "created_at": p.created_at} for p in pending]})


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
    except live_module.WorkspaceBusy as exc:
        return error("workspace_busy", str(exc), status=409)
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


# --- runtimes, handoff, diff, recovery (P1.7) --------------------------------------


@route("GET", r"/api/runtimes")
def _runtimes(request: Request, ctx: DashboardContext, _match) -> Response:
    from garuda.interfaces.web.runtimes import list_runtimes

    return ok(list_runtimes(workspace=str(ctx.workspace), extra=ctx.extra))


def _usage_range(request: Request):
    name = request.first("range", "7d")
    if name not in ("24h", "7d", "30d"):
        return None
    return name


@route("GET", r"/api/usage")
def _usage(request: Request, ctx: DashboardContext, _match) -> Response:
    """Ledger statistics for a rolling UTC window; a report, never a routing input (F.3)."""
    import time as _time

    from garuda.core import usage_stats
    from garuda.observability.ledger import Ledger

    name = _usage_range(request)
    if name is None:
        return invalid("`range` must be 24h, 7d or 30d.")
    now = _time.time()
    from garuda.core.project_aliases import verified_aliases
    from garuda.core.project_identity import ProjectIdentityError

    try:
        aliases = verified_aliases(ctx.store.root)
    except ProjectIdentityError:
        return invalid("Project identity recovery cannot be verified; usage grouping is unavailable.")
    return ok(usage_stats.stats(list(Ledger().records(since=now - 31 * 86400)), name, now,
                               project_aliases=aliases))


@route("GET", r"/api/usage/export")
def _usage_export(request: Request, ctx: DashboardContext, _match) -> Response:
    import time as _time

    from garuda.core import usage_stats
    from garuda.observability.ledger import Ledger

    name = _usage_range(request)
    fmt = request.first("format", "json")
    if name is None or fmt not in ("csv", "json"):
        return invalid("`range` must be 24h, 7d or 30d and `format` csv or json.")
    now = _time.time()
    rows = usage_stats.export_rows(list(Ledger().records(since=now - 31 * 86400)), name, now)
    body = usage_stats.export_csv(rows) if fmt == "csv" else usage_stats.export_json(rows)
    return Response(
        body=body.encode("utf-8"),
        content_type="text/csv; charset=utf-8" if fmt == "csv"
        else "application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="garuda-usage-{name}.{fmt}"'})


@route("GET", r"/api/setup")
def _setup(request: Request, ctx: DashboardContext, _match) -> Response:
    """Diagnostics, effective-config provenance, roles and flows (F.4). Read-only."""
    from garuda.core import setup_view

    return ok(setup_view.setup(str(ctx.workspace)))


@route("GET", r"/api/providers")
def _providers(request: Request, ctx: DashboardContext, _match) -> Response:
    """One card per harness and per API provider: limits with their source and time,
    observed use "through Garuda only". Reads records; starts nothing."""
    from garuda.interfaces.web import providers
    from garuda.interfaces.web.runtimes import list_runtimes, registry_from_context

    workspace = str(ctx.workspace)
    entries = list_runtimes(workspace=workspace, extra=ctx.extra)
    manifests = {m.runtime_id: m for m in registry_from_context(ctx.extra, workspace).manifests}
    return ok(providers.cards(entries, manifests))


@route("POST", r"/api/providers/refresh")
def _providers_refresh(request: Request, ctx: DashboardContext, _match) -> Response:
    """Re-run the documented status reads (login check, proved limit read). No prompt."""
    from garuda.interfaces.web import providers

    refusal = requires_write(ctx)
    if refusal is not None:
        return refusal
    return ok(providers.refresh(str(ctx.workspace)))


@route("GET", r"/api/runtimes/(?P<rid>[A-Za-z0-9._-]+)")
def _runtime_detail(request: Request, ctx: DashboardContext, match) -> Response:
    from garuda.interfaces.web.runtimes import inspect_runtime

    payload = inspect_runtime(None, match["rid"], workspace=str(ctx.workspace), extra=ctx.extra)
    if payload is None:
        return not_found(f"No runtime {match['rid']!r}.")
    return ok(payload)


@route("GET", r"/api/runs/(?P<sid>[^/]+)/handoff")
def _handoff_preview(request: Request, ctx: DashboardContext, match) -> Response:
    from garuda.core.sessions import validate_session_ref
    from garuda.interfaces.web.runtimes import handoff_preview, registry_from_context

    session_id = validate_session_ref(match["sid"])
    target = request.first("to")
    if not target:
        return invalid("`to` query parameter is required.")
    try:
        return ok(
            handoff_preview(
                ctx.store,
                session_id,
                target,
                registry=registry_from_context(ctx.extra, str(ctx.workspace)),
            )
        )
    except (OSError, ValueError, RegistryError) as exc:
        return not_found(f"No readable session {session_id!r}: {exc}")


@route("POST", r"/api/runs/(?P<sid>[^/]+)/handoff")
def _handoff_prepare(request: Request, ctx: DashboardContext, match) -> Response:
    from garuda.core.sessions import validate_session_ref
    from garuda.interfaces.web.runtimes import handoff_prepare, registry_from_context

    refusal = requires_write(ctx)
    if refusal is not None:
        return refusal
    session_id = validate_session_ref(match["sid"])
    body = request.json_body()
    target = body.get("target") if isinstance(body, dict) else None
    if not target or not isinstance(target, str):
        return invalid("JSON body `target` is required.")
    try:
        return ok(
            handoff_prepare(
                ctx.store,
                session_id,
                target,
                registry=registry_from_context(ctx.extra, str(ctx.workspace)),
            )
        )
    except (OSError, ValueError, RegistryError) as exc:
        return not_found(f"Cannot prepare handoff for {session_id!r}: {exc}")


@route("GET", r"/api/runs/(?P<sid>[^/]+)/diff")
def _run_diff(request: Request, ctx: DashboardContext, match) -> Response:
    from garuda.core.sessions import validate_session_ref
    from garuda.interfaces.web.runtimes import diff_timeline
    from garuda.workspace.diff import DiffError

    session_id = validate_session_ref(match["sid"])
    try:
        return ok(diff_timeline(ctx.store, session_id))
    except (DiffError, OSError, ValueError) as exc:
        return not_found(f"No readable session {session_id!r}: {exc}")


@route("GET", r"/api/runs/(?P<sid>[^/]+)/recover")
def _recover_preview(request: Request, ctx: DashboardContext, match) -> Response:
    from garuda.core.sessions import validate_session_ref
    from garuda.interfaces.web.runtimes import recover_report

    session_id = validate_session_ref(match["sid"])
    return ok(recover_report(ctx.store, session_id))


@route("POST", r"/api/runs/(?P<sid>[^/]+)/recover")
def _recover_run(request: Request, ctx: DashboardContext, match) -> Response:
    from garuda.core.sessions import validate_session_ref
    from garuda.interfaces.web.runtimes import recover_report

    refusal = requires_write(ctx)
    if refusal is not None:
        return refusal
    session_id = validate_session_ref(match["sid"])
    return ok(recover_report(ctx.store, session_id, run=True))
