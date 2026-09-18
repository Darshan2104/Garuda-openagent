"""Talking to an agent from the browser: conversations, grounding, cancellation.

**What changes from the read-only surface to here, stated plainly: reading runs discloses
information; this executes attacker-chosen code as the invoking user.** The Host allowlist,
the Origin check on state-changing methods, the absent CORS header and the token are the same
four defenses as before, but from this module on they are the only thing between a page the
developer happens to visit and arbitrary code running with their git and cloud credentials.
That is why the two rules below exist on top of the transport gate — and why they, not the
``--read-only`` switch, are what actually bounds the risk.

**W1 — the workspace comes from a server allowlist, referenced by index.** A free-form
``workspace`` string is arbitrary-filesystem-write by design: the agent's whole job is to
edit files in the directory it is given. The default allowlist is the single directory the
dashboard was started in.

**W2 — the server holds a permission ceiling, default ``smart``.** A request may ask for a
posture at least as strict as the ceiling and no looser, so ``yolo`` from a browser is
refused unless the operator started the dashboard with ``--max-permission yolo``. Same
reasoning as ``core/modes.py``'s ``FORCED_FIELDS``: a safety posture the caller can opt out
of is not a posture.

A turn is not reimplemented either: it is ``cli.py``'s chat loop with the 50 ms polling
deleted, because the browser polls the event log instead. ``JobManager`` owns concurrency,
cancellation and retention, which is what makes stopping a turn a two-line method.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from garuda.core.events import EventStore
from garuda.core.sessions import SessionStore
from garuda.interfaces.jobs import Job, JobManager
from garuda.interfaces.web import grounding
from garuda.interfaces.web.approvals import ApprovalBroker
from garuda.model.protocol import DEFAULT_MODEL

logger = logging.getLogger(__name__)

#: Strictest first. A request may name any mode with a rank at or below the ceiling.
PERMISSION_RANK = {"readonly": 0, "smart": 1, "auto": 2, "yolo": 3}

#: The default ceiling. `smart` still asks before anything destructive, which is the
#: posture that makes a browser-launched run recoverable.
DEFAULT_MAX_PERMISSION = "smart"

#: How long an HTTP thread waits for the agent loop to accept a request. Submitting a job
#: is fast (it creates a task and returns); a timeout here means the loop is wedged, and a
#: 504 is a better answer than a hung connection.
LOOP_CALL_TIMEOUT = 30.0

MAX_TASK_CHARS = 20_000


class SpecError(ValueError):
    """A bad request. Surfaces as a 400 with the message."""


class BusyError(RuntimeError):
    """A turn is already in flight for this chat. Surfaces as a 409."""


@dataclass
class ChatSpec:
    """One validated request to open a conversation.

    No ``task``: a chat is opened empty and fed turns afterwards. It used to carry one,
    because the same type validated the (now removed) one-off run launcher, and the chat
    route passed a placeholder string that reached nothing.
    """

    agent: str | None = None
    model: str | None = None
    mode: str | None = None
    permission_mode: str | None = None
    #: Index into the server's workspace allowlist. Never a path.
    workspace: int = 0
    workspace_kind: str | None = None

    @classmethod
    def parse(cls, payload: Any) -> "ChatSpec":
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise SpecError("A JSON object body is required.")

        workspace = payload.get("workspace", 0)
        if isinstance(workspace, bool) or not isinstance(workspace, (int, str)):
            raise SpecError("`workspace` must be an index into the configured workspaces.")
        try:
            workspace_index = int(workspace)
        except (TypeError, ValueError):
            # Names the actual failure: a caller sending a path is not making a typo, they
            # are expecting an interface this deliberately does not have.
            raise SpecError(
                "`workspace` must be an index into the configured workspaces, not a path. "
                "GET /api/config lists them."
            ) from None

        return cls(
            agent=_optional_str(payload.get("agent"), "agent"),
            model=_optional_str(payload.get("model"), "model"),
            mode=_optional_str(payload.get("mode"), "mode"),
            permission_mode=_optional_str(payload.get("permission_mode"), "permission_mode"),
            workspace=workspace_index,
            workspace_kind=_optional_str(payload.get("workspace_kind"), "workspace_kind"),
        )


def _optional_str(value: Any, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SpecError(f"`{name}` must be a string.")
    return value


@dataclass
class LiveRuns:
    """Owns the job queue and the two write-mode rules.

    Constructed on the asyncio loop that will run the agents, and every method that
    touches the loop is a coroutine — the HTTP thread reaches them through
    ``call_on_loop`` rather than touching a task or a future directly.
    """

    loop: asyncio.AbstractEventLoop
    store: SessionStore
    #: W1: the only directories a browser conversation may be given.
    workspaces: tuple[Path, ...] = ()
    #: W2: the loosest posture a request may ask for.
    max_permission: str = DEFAULT_MAX_PERMISSION
    default_model: str = DEFAULT_MODEL
    default_agent: str = "build"
    agents_dir: Path | None = None
    workspace_kind: str = "local"
    docker_image: str | None = None
    jobs: JobManager = field(default_factory=JobManager)
    approvals: ApprovalBroker = field(default_factory=ApprovalBroker)
    chats: dict[str, "LiveChat"] = field(default_factory=dict)
    #: A closed tab never sends DELETE, and an abandoned chat holds a container.
    chat_idle_seconds: float = 1800.0
    reap_interval_seconds: float = 10.0
    _reaper: Any = None

    # -- W1 and W2 ------------------------------------------------------------

    def resolve_workspace(self, index: int) -> Path:
        if not self.workspaces:
            raise SpecError("No workspaces are configured for this dashboard.")
        if index < 0 or index >= len(self.workspaces):
            raise SpecError(
                f"`workspace` must be 0–{len(self.workspaces) - 1}; got {index}."
            )
        return self.workspaces[index]

    def clamp_permission(self, requested: str | None) -> str:
        """The posture to run with, refusing anything looser than the ceiling.

        Refuses rather than silently clamping. A caller who asked for ``yolo`` and
        quietly got ``smart`` would see approval prompts they did not expect and blame
        the harness; being told the ceiling exists is the useful answer.
        """
        ceiling = PERMISSION_RANK.get(self.max_permission, PERMISSION_RANK[DEFAULT_MAX_PERMISSION])
        if requested is None:
            # Not the ceiling itself: a run should be as strict as the operator's
            # default allows, and the ceiling is a maximum, not a target.
            return self.max_permission
        rank = PERMISSION_RANK.get(requested)
        if rank is None:
            raise SpecError(
                f"`permission_mode` must be one of {', '.join(PERMISSION_RANK)}."
            )
        if rank > ceiling:
            raise SpecError(
                f"`permission_mode` {requested!r} is looser than this dashboard's ceiling "
                f"({self.max_permission!r}). Relaunch with "
                f"`garuda web --allow-run --max-permission {requested}` to allow it."
            )
        return requested

    # -- inspecting and cancelling -------------------------------------------

    def job_payload(self, job: Job) -> dict[str, Any]:
        return {
            "job_id": job.id,
            "session_id": job.session_id,
            "task": job.task,
            "state": job.state.value,
            "done": job.done,
            "error": job.error,
            "success": job.result.success if job.result else None,
            "turns": job.result.turns if job.result else None,
        }

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return self.job_payload(job) if job else None

    async def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        cancelling = self.jobs.cancel(job_id)
        return {**self.job_payload(job), "cancelling": cancelling}

    # -- chat -----------------------------------------------------------------

    async def start_chat(self, spec: ChatSpec) -> dict[str, Any]:
        """Open a conversation. One workspace and one environment for its whole life, as
        ``cli.py`` does — a chat that rebuilt its container per turn would lose every file
        it wrote."""
        from garuda.interfaces.runner import resolve_environment
        from garuda.interfaces.session import AgentSession

        workspace = self.resolve_workspace(spec.workspace)
        permission_mode = self.clamp_permission(spec.permission_mode)
        chat_id = uuid.uuid4().hex
        model = spec.model or self.default_model
        agent = spec.agent or self.default_agent

        # The handler needs the session's own event store to recover structured tool
        # arguments, but `AgentSession.create` takes the handler and creates the store. A
        # lazy provider closes that ordering gap without reaching into private state.
        holder: dict[str, EventStore] = {}
        session = await AgentSession.create(
            agent_name=agent,
            model=model,
            workspace=str(workspace),
            agents_dir=self.agents_dir,
            mode=spec.mode,
            permission_mode=permission_mode,
            approval_handler=self.approvals.make_handler(chat_id, lambda: holder.get("events")),
            workspace_kind=spec.workspace_kind or self.workspace_kind,
        )
        holder["events"] = session.events
        env, env_handle = await resolve_environment(
            session.config.workspace_kind, str(workspace), session.config.docker_image,
            docker_host=getattr(session.config, "docker_host", None),
        )
        events_path = self.store.begin(
            session_id=session.events.session_id,
            task="(dashboard chat)",
            model=model,
            agent=session.profile.name,
            workspace=str(workspace),
        )
        session.events.attach_persistence(events_path)

        chat = LiveChat(
            chat_id=chat_id, session=session, env=env, env_handle=env_handle,
            workspace=workspace, permission_mode=permission_mode, model=model,
            agent=session.profile.name, last_seen_at=time.monotonic(),
        )
        self.chats[chat_id] = chat
        logger.info("Dashboard opened chat %s in %s as %s", chat_id, workspace, permission_mode)
        return chat.as_dict()

    async def chat_turn(self, chat_id: str, task: str) -> dict[str, Any] | None:
        """Submit one turn. ``None`` for an unknown chat; raises ``BusyError`` for a 409."""
        chat = self.chats.get(chat_id)
        if chat is None:
            return None
        chat.last_seen_at = time.monotonic()
        if chat.busy:
            raise BusyError(
                "A turn is already in flight for this chat. One ContextManager spans the "
                "conversation and is not concurrency-safe."
            )
        task = (task or "").strip()
        if not task:
            raise SpecError("`task` is required and must be a non-empty string.")
        if len(task) > MAX_TASK_CHARS:
            raise SpecError(f"`task` must be at most {MAX_TASK_CHARS} characters.")

        # Sources added since the last turn are announced with this one, then cleared. On
        # *this* turn rather than at upload time, because an upload is not a turn: injecting
        # one would either take a model call to acknowledge a file the user is still choosing
        # files alongside, or need a second conversation state for "files pending".
        prompt = grounding.announce(chat.take_new_sources()) + task

        # Set before the await point, in the same tick as the submit: a gap here is the
        # window in which a second request passes the check.
        chat.busy = True
        chat.turn_index += 1
        if chat.turn_index == 1:
            # A chat opens before there is anything to call it, so `store.begin` writes a
            # placeholder. Naming it after the first message is what makes the run list
            # readable — a column of "(dashboard chat)" rows is a list you cannot use.
            # The raw message, not `prompt`: the grounding preamble is scaffolding.
            chat.session_task = task[:200]
            self.store.update_meta(chat.session_id, {"task": chat.session_task})
        context = chat.session.prepare_context(prompt)
        job = self.jobs.submit(
            lambda job: chat.session.agent.run(
                task=prompt,
                model=chat.session.model,
                env=chat.env,
                tools=chat.session.tools,
                config=chat.session.config,
                events=chat.session.events,
                permissions=chat.session.permissions,
                agents_dir=chat.session.agents_dir,
                context=context,
            ),
            task=prompt,
            events=chat.session.events,
        )
        chat.job_id = job.id
        if job._task is not None:
            job._task.add_done_callback(lambda _: setattr(chat, "busy", False))
        return {
            "chat_id": chat_id,
            "job_id": job.id,
            "session_id": chat.session_id,
            "turn_index": chat.turn_index,
            "state": job.state.value,
        }

    async def stop_turn(self, chat_id: str) -> dict[str, Any] | None:
        """Interrupt the turn in flight, keeping the conversation open.

        Cancellation lands on the ``await`` inside the agent loop and unwinds through the same
        path a Ctrl-C takes, so an approval parked at that moment is unparked by its own
        handler's ``finally`` and nothing here has to know about it. The workspace, the
        environment and the message history all survive — that is the difference from
        ``close_chat``, and the reason a stop button can exist at all.
        """
        chat = self.chats.get(chat_id)
        if chat is None:
            return None
        chat.last_seen_at = time.monotonic()
        if not chat.busy or chat.job_id is None:
            return {**chat.as_dict(), "stopped": False, "reason": "nothing in flight"}
        cancelling = self.jobs.cancel(chat.job_id)
        # `busy` is cleared by the job's done-callback, not here: clearing it now would let a
        # new turn start while the cancelled one is still unwinding its tool calls.
        return {**chat.as_dict(), "stopped": bool(cancelling)}

    # -- grounding ------------------------------------------------------------

    async def add_source(self, chat_id: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """Attach a document or a web page to a conversation."""
        chat = self.chats.get(chat_id)
        if chat is None:
            return None
        chat.last_seen_at = time.monotonic()
        kind = body.get("kind") or ("url" if body.get("url") else "file")
        if kind == "url":
            source = await grounding.store_url(chat.workspace, body.get("url"))
        elif kind == "file":
            source = await grounding.store_file(
                chat.workspace, body.get("name"), body.get("content_b64")
            )
        else:
            raise SpecError("`kind` must be `file` or `url`.")
        chat.sources.append(source)
        chat.new_sources.append(source)
        return {"source": source.as_dict(), "pending": len(chat.new_sources)}

    async def list_sources(self, chat_id: str) -> dict[str, Any] | None:
        chat = self.chats.get(chat_id)
        if chat is None:
            return None
        return {
            "sources": [source.as_dict() for source in chat.sources],
            # Which ones the agent has not been told about yet. The UI marks these, so
            # "uploaded" and "the agent knows" are visibly different states.
            "pending": [source.path for source in chat.new_sources],
            "directory": grounding.GROUNDING_DIR,
        }

    async def list_chats(self) -> dict[str, Any]:
        return {"chats": [chat.as_dict() for chat in self.chats.values()]}

    async def touch_chat(self, chat_id: str) -> dict[str, Any] | None:
        chat = self.chats.get(chat_id)
        if chat is None:
            return None
        chat.last_seen_at = time.monotonic()
        self.approvals.touch(chat_id)
        return chat.as_dict()

    async def close_chat(self, chat_id: str) -> dict[str, Any] | None:
        """Tear a chat down: deny its pending asks, close the session, drop the workspace."""
        chat = self.chats.pop(chat_id, None)
        if chat is None:
            return None
        denied = self.approvals.forget(chat_id)
        summary = chat.as_dict()
        try:
            await chat.session.close()
        except Exception:
            logger.warning("Closing chat %s session failed", chat_id, exc_info=True)
        try:
            from garuda.interfaces.runner import cleanup_workspace

            await cleanup_workspace(chat.env_handle)
        except Exception:
            logger.warning("Tearing down chat %s workspace failed", chat_id, exc_info=True)
        self.store.update_meta(chat.session_id, {"status": "finished"})
        return {**summary, "closed": True, "denied_approvals": denied}

    async def reap(self) -> dict[str, int]:
        """Deny abandoned approvals and close idle chats.

        Runs on the agent loop as a periodic task, because a closed tab never sends a
        DELETE and an abandoned chat holds a container, an MCP subprocess and a persistent
        shell. Both halves matter: the approval reaper unblocks a wedged run in seconds, the
        chat reaper reclaims its resources minutes later.
        """
        denied = self.approvals.reap()
        cutoff = time.monotonic() - self.chat_idle_seconds
        closed = 0
        for chat_id, chat in list(self.chats.items()):
            if chat.busy or chat.last_seen_at >= cutoff:
                continue
            logger.info("Closing chat %s after %.0fs idle", chat_id, self.chat_idle_seconds)
            await self.close_chat(chat_id)
            closed += 1
        return {"denied_approvals": denied, "closed_chats": closed}

    async def run_reaper(self) -> None:
        """The periodic task. Cancelled by ``aclose``."""
        while True:
            await asyncio.sleep(self.reap_interval_seconds)
            try:
                await self.reap()
            except Exception:  # pragma: no cover - a reaper that dies is worse than one
                logger.warning("Reaper pass failed", exc_info=True)

    async def aclose(self) -> None:
        """Cancel everything still running. Called from the server's shutdown path so a
        Ctrl-C does not leave a container or an MCP subprocess behind — ``run_agent_task``
        tears those down in its own ``finally``, but only if the task is actually
        cancelled."""
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for chat_id in list(self.chats):
            await self.close_chat(chat_id)
        for job in self.jobs.list():
            if not job.done:
                self.jobs.cancel(job.id)
        pending = [job._task for job in self.jobs.list() if job._task and not job._task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def start_reaper(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.ensure_future(self.run_reaper())


@dataclass
class LiveChat:
    """One multi-turn conversation: an ``AgentSession`` plus one workspace for its life.

    ``busy`` is not bookkeeping. A single ``ContextManager`` spans the whole chat and is not
    concurrency-safe: two turns in flight interleave appends and can split a
    ``tool_calls``/``tool_result`` pair, which providers reject outright with a 400. So a
    second turn while one is running is a 409, and ``busy`` is set in the same loop tick as
    the submit so there is no window between the check and the flag.
    """

    chat_id: str
    session: Any
    env: Any
    env_handle: Any
    workspace: Path
    permission_mode: str
    model: str
    agent: str
    busy: bool = False
    turn_index: int = 0
    last_seen_at: float = 0.0
    hooks: Any = None
    #: The job running the current turn, so it can be cancelled without ending the chat.
    job_id: str | None = None
    #: What the run list calls this conversation: the first message, set on the first turn.
    session_task: str | None = None
    #: Everything grounded into this conversation, and the subset not yet announced to the
    #: agent. Two lists rather than a flag per source: the announcement is per turn, and
    #: "which files does this turn need to mention" is exactly a queue.
    sources: list[Any] = field(default_factory=list)
    new_sources: list[Any] = field(default_factory=list)

    @property
    def session_id(self) -> str:
        return self.session.events.session_id

    def take_new_sources(self) -> list[Any]:
        """The sources to announce on this turn, clearing the queue."""
        pending = list(self.new_sources)
        self.new_sources.clear()
        return pending

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "agent": self.agent,
            "model": self.model,
            "permission_mode": self.permission_mode,
            "workspace": str(self.workspace),
            "busy": self.busy,
            "turns": self.turn_index,
            "task": self.session_task,
            "sources": len(self.sources),
            "pending_sources": len(self.new_sources),
            "idle_seconds": round(time.monotonic() - self.last_seen_at, 1),
        }


def call_on_loop(loop: asyncio.AbstractEventLoop, coro, *, timeout: float = LOOP_CALL_TIMEOUT):
    """Run a coroutine on the agent loop from an HTTP thread and wait for it.

    The HTTP server is threaded and the agent loop is a single asyncio loop, so every
    crossing goes through ``run_coroutine_threadsafe``. Anything else — creating a task
    from the wrong thread, touching a future directly — is undefined behaviour that
    usually looks like it works.

    The same-thread check is not defensive padding. Called from the loop's *own* thread
    this deadlocks for the full timeout: the loop cannot run the coroutine because the
    thread that would run it is blocked on ``result()``. A 30-second hang that resolves
    into a 504 is close to undiagnosable, so it is turned into an immediate, named error.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        coro.close()
        raise RuntimeError(
            "call_on_loop was called from the agent loop's own thread, which would "
            "deadlock. Dashboard routes run on HTTP threads; await the coroutine directly "
            "instead."
        )
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)
