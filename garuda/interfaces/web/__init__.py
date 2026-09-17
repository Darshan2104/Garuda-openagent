"""The Garuda web dashboard: read your runs, and talk to an agent.

Not to be confused with ``garuda.eval.dashboard``, which is the markdown cost/latency
table (``python -m garuda.eval.dashboard``). This package is the browser surface, and
it reuses that module's row builder rather than recomputing anything.

Its own HTTP server on its own port, deliberately not sharing anything with
``interfaces/server.py``: that surface refuses every request carrying an ``Origin``
header, which is right for a JSON-RPC endpoint only programmatic clients use and
impossible for a browser. Keeping them separate means the browser gate cannot be
weakened into the IDE gate by a later refactor.
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from garuda.core.sessions import SessionStore
from garuda.interfaces.web import http as http_module
from garuda.interfaces.web.live import DEFAULT_MAX_PERMISSION, LiveRuns
from garuda.interfaces.web.routes import DashboardContext
from garuda.interfaces.web.security import DEFAULT_PORT, generate_token
from garuda.model.protocol import DEFAULT_MODEL

logger = logging.getLogger(__name__)

__all__ = ["DashboardConfig", "serve_dashboard", "DEFAULT_PORT",
           "DEFAULT_MAX_PERMISSION", "build_context", "launch_url"]


@dataclass
class DashboardConfig:
    """What ``garuda web`` was asked for.

    Note the absence of a ``host`` field: the bind address is 127.0.0.1 and is not
    configurable — see ``http.BIND_HOST``. ``tests/test_web_bind.py`` asserts it stays
    absent, so it cannot be added back without the reasoning being revisited.
    """

    port: int = DEFAULT_PORT
    #: Empty means "generate one". Never printed anywhere but the launch URL.
    token: str = ""
    sessions_dir: Path | None = None
    agents_dir: Path | None = None
    #: Whether the dashboard may talk to an agent.
    #:
    #: **On by default, unlike every earlier revision of this file.** The reasoning changed
    #: because the dashboard changed: it used to be a viewer with an optional launch button,
    #: and a viewer that executes code by default would be indefensible. It is now a viewer
    #: *and* the place you talk to the agent, so shipping that half switched off meant the
    #: headline feature 503'd until you found a flag — which is exactly what happened.
    #:
    #: What actually bounds the risk is unchanged and is not this flag: the bind is loopback
    #: with no `--host`, every request needs the token, the Host allowlist stops DNS
    #: rebinding, and `max_permission` still defaults to `smart` so every destructive tool
    #: call waits for a click. `--read-only` turns it off for a dashboard left running.
    allow_run: bool = True
    #: W1: the only directories a browser-launched run may be given, by index. Empty means
    #: "the process's own working directory", filled in by `build_context`.
    workspaces: tuple[Path, ...] = ()
    #: W2: the loosest permission posture a request may ask for.
    max_permission: str = DEFAULT_MAX_PERMISSION
    model: str = ""
    agent: str = "build"
    workspace_kind: str = "local"
    open_browser: bool = True
    extra: dict = field(default_factory=dict)


def build_context(
    config: DashboardConfig, *, loop: asyncio.AbstractEventLoop | None = None
) -> DashboardContext:
    """Assemble the routes' context.

    ``loop`` is required for write mode and is deliberately a parameter rather than
    something fetched here: the loop that runs the agents has to be the one the HTTP
    threads marshal onto, and only the caller inside ``serve_dashboard`` knows which that
    is. Without it, ``ctx.live`` stays None and every write route 503s — the fail-closed
    outcome rather than a launch button that misbehaves.
    """
    store = SessionStore(root=config.sessions_dir)
    ctx = DashboardContext(
        port=config.port,
        token=config.token or generate_token(),
        store=store,
        allow_run=config.allow_run,
        agents_dir=Path(config.agents_dir) if config.agents_dir else None,
        loop=loop,
    )
    if config.allow_run and loop is not None:
        ctx.live = LiveRuns(
            loop=loop,
            store=store,
            workspaces=tuple(Path(p).expanduser().resolve() for p in config.workspaces)
            or (Path.cwd().resolve(),),
            max_permission=config.max_permission,
            default_model=config.model or DEFAULT_MODEL,
            default_agent=config.agent,
            agents_dir=ctx.agents_dir,
            workspace_kind=config.workspace_kind,
        )
        # A closed tab never sends DELETE and an abandoned approval wedges a run, so the
        # reaper is part of write mode rather than something the client can forget.
        ctx.live.start_reaper()
    return ctx


def launch_url(ctx: DashboardContext) -> str:
    """The URL to open.

    The token rides in the **fragment**: it is never sent to a server, never lands in
    an access log, and never leaks through ``Referer``. The page lifts it into
    ``sessionStorage`` and immediately replaces the hash.
    """
    return f"http://{http_module.BIND_HOST}:{ctx.port}/#t={ctx.token}"


async def serve_dashboard(config: DashboardConfig) -> None:
    """Bind, print the URL, and serve until cancelled.

    The HTTP server runs on a thread pool while this coroutine's own loop stays free —
    which is the whole point of the split. Reads are blocking file I/O, and doing them on
    the loop would stall the very agent the dashboard is watching; write mode needs the
    loop, because `JobManager.submit` creates a task on it.
    """
    ctx = build_context(config, loop=asyncio.get_running_loop())
    server, port = http_module.bind(ctx, config.port)
    url = launch_url(ctx)
    # flush=True is load-bearing, exactly as it is in `server.py`'s token print:
    # Python block-buffers stdout when it is not a tty, and this process then sits in
    # serve_forever without ever filling the buffer — stranding the only copy of the
    # URL that carries the token.
    print(
        f"Garuda dashboard ({'read-write' if ctx.allow_run else 'read-only'})\n"
        f"  {url}\n"
        f"  sessions: {ctx.store.root}",
        flush=True,
    )
    if config.open_browser:
        # Best-effort: a headless machine has no browser and that must not be fatal.
        try:
            webbrowser.open(url)
        except Exception:
            logger.debug("Could not open a browser automatically", exc_info=True)
    try:
        await asyncio.to_thread(server.serve_forever)
    finally:
        server.shutdown()
        server.server_close()
        if ctx.live is not None:
            # Cancel anything still running so `run_agent_task`'s own teardown fires: a
            # Ctrl-C must not leave a container or an MCP subprocess behind.
            await ctx.live.aclose()
