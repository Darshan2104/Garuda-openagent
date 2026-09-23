"""JSON-RPC HTTP server for IDE and automation integrations."""

import asyncio
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garuda.agents.loader import list_profiles
from garuda.agents.setup import prepare_agent_run
from garuda.core.events import EventStore
from garuda.core.sessions import SessionStore
from garuda.interfaces.jobs import Job, JobManager
from garuda.interfaces.runner import run_agent_task
from garuda.model.litellm_model import LitellmModel
from garuda.model.protocol import DEFAULT_MODEL

logger = logging.getLogger(__name__)

UNAUTHORIZED_CODE = -32001
PARSE_ERROR_CODE = -32700
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _write_simple(writer: "asyncio.StreamWriter", status: bytes, message: str) -> None:
    """Write a minimal JSON error response for requests rejected before dispatch."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR_CODE, "message": message}}
    ).encode("utf-8")
    writer.write(
        status
        + b"\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        + body
    )

# Drop a client that hasn't sent a complete request within this many seconds
# (slow-loris protection).
REQUEST_READ_TIMEOUT = 30.0
# Ceiling on a request body. Generous for JSON-RPC (the largest realistic payload is
# a file write), and small enough that a declared Content-Length cannot be used to
# exhaust memory before the request is even authenticated.
MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    model: str = DEFAULT_MODEL
    agent: str = "build"
    workspace: str = "."
    workspace_kind: str = "local"
    docker_image: str = "ubuntu:22.04"
    docker_host: str | None = None
    agents_dir: str | None = None
    mcp_config: str | None = None
    token: str | None = None
    max_jobs: int = 4
    model_max_concurrency: int = 0
    # Retention for finished jobs. A completed job keeps its whole event history
    # in memory, so on a long-lived server these bound the process, not just the
    # response of `jobs.list`. 0 seconds disables the TTL (the count cap remains).
    max_retained_jobs: int = 200
    job_retain_seconds: float = 3600.0


def ensure_secure_config(config: ServerConfig) -> None:
    """Guarantee the server is authenticated before it accepts connections.

    A non-loopback bind still requires an explicit token (auto-generating one for
    an internet-facing port is a footgun). For loopback, a token is auto-generated
    and printed so the endpoint is not an unauthenticated local-RCE surface — any
    local process or a malicious web page the developer visits could otherwise
    drive it.
    """
    if config.token:
        return
    if config.host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"Refusing to serve on non-loopback host {config.host!r} without authentication. "
            "Set a bearer token via --token or the GARUDA_SERVE_TOKEN env var, "
            "or bind to 127.0.0.1."
        )
    config.token = secrets.token_urlsafe(32)
    # flush=True is load-bearing, not tidiness. Python block-buffers stdout when it
    # is not a tty, which is how a server is normally started (`garuda serve > log`,
    # systemd, docker). The process then blocks in the event loop forever, so the
    # buffer never fills and never flushes: the only copy of the generated token is
    # stranded in memory and every request 401s. The server was unusable in exactly
    # the deployment shape this token exists for.
    print(
        "[garuda serve] No token configured; generated one for this session.\n"
        f"  Authorization: Bearer {config.token}",
        flush=True,
    )


class JsonRpcServer:
    """Minimal JSON-RPC 2.0 server over HTTP POST."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._job_manager: JobManager | None = None

    def _authorized(self, headers: dict[str, str] | None) -> bool:
        if not self._config.token:
            return True
        provided = ""
        for key, value in (headers or {}).items():
            if key.lower() == "authorization":
                provided = value.strip()
                break
        # Constant-time compare so the token can't be recovered by timing.
        return hmac.compare_digest(provided, f"Bearer {self._config.token}")

    @staticmethod
    def _has_browser_origin(headers: dict[str, str] | None) -> bool:
        """True if the request carries an Origin header — i.e. it came from a
        browser. Programmatic clients (IDE, curl, SDK) don't set Origin; rejecting
        it blocks cross-site CSRF / DNS-rebinding attempts as defense-in-depth."""
        return any(k.lower() == "origin" for k in (headers or {}))

    async def handle(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        req_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        if self._has_browser_origin(headers):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": UNAUTHORIZED_CODE,
                    "message": "Unauthorized: cross-origin (browser) requests are not allowed",
                },
            }
        if not self._authorized(headers):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": UNAUTHORIZED_CODE,
                    "message": "Unauthorized: missing or invalid bearer token",
                },
            }

        try:
            if method == "health":
                result = await self._health()
            elif method == "run":
                result = await self._run(params)
            elif method == "submit":
                result = await self._submit(params)
            elif method == "status":
                result = self._status(params)
            elif method == "events":
                result = self._job_events(params)
            elif method == "result":
                result = self._result(params)
            elif method == "cancel":
                result = self._cancel(params)
            elif method == "jobs":
                result = self._list_jobs(params)
            elif method == "sessions":
                result = {
                    "sessions": SessionStore().list_sessions(limit=int(params.get("limit", 20)))
                }
            elif method == "list_agents":
                agents_dir = params.get("agents_dir", self._config.agents_dir)
                result = {
                    "agents": list_profiles(
                        extra_dir=Path(agents_dir) if agents_dir else None
                    )
                }
            elif method == "runtime_list":
                result = await self._runtime_list(params)
            elif method == "runtime_inspect":
                result = await self._runtime_inspect(params)
            elif method == "runtime_handoff":
                result = await self._runtime_handoff(params)
            elif method == "runtime_recover":
                result = await self._runtime_recover(params)
            elif method == "runtime_support":
                result = await self._runtime_support(params)
            else:
                raise ValueError(f"Unknown method: {method}")
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    async def _health(self) -> dict[str, Any]:
        # `garuda.__version__`, not importlib.metadata: under `pip install -e .` the
        # distribution metadata is a snapshot from install time, so health reported
        # 1.1.0 while the running code was 1.1.1. A version that lags whatever you
        # last installed is worse than no version — it is the field a client uses to
        # decide whether a fix is deployed. `tests/test_server_auth.py` pins this
        # against pyproject so the two cannot drift.
        from garuda import __version__

        return {"status": "ok", "version": __version__}

    def _runtime_registry(self, params: dict[str, Any]):
        """Build the trusted registry; request data may only select an ID."""
        if "runtimes" in params:
            raise ValueError("request-defined runtime manifests are not permitted")
        from garuda.config.agent_home import resolve_agent_home
        from garuda.interfaces.runtime_cli import configured_registry

        home = resolve_agent_home(self._config.workspace)
        return configured_registry(
            self._config.workspace,
            global_settings=getattr(home, "global_settings", None),
        )

    async def _runtime_list(self, params: dict[str, Any]) -> dict[str, Any]:
        from garuda.acp.catalog import discover, health_of
        from garuda.interfaces.runtime_cli import RUNTIME_API_VERSION
        registry = self._runtime_registry(params)
        return {
            "api": f"runtime/v{RUNTIME_API_VERSION}",
            "runtimes": [
                health_of(entry)
                for entry in discover(registry.manifests, disabled=registry.disabled_ids)
            ],
        }

    async def _runtime_inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        from garuda.acp.catalog import discover, health_of
        from garuda.interfaces.runtime_cli import RUNTIME_API_VERSION
        runtime_id = params.get("runtime")
        if not runtime_id:
            raise ValueError("params.runtime is required")
        registry = self._runtime_registry(params)
        found = {
            entry.runtime_id: entry
            for entry in discover(registry.manifests, disabled=registry.disabled_ids)
        }
        if runtime_id not in found:
            raise ValueError(f"Unknown runtime {runtime_id!r}")
        entry = found[runtime_id]
        record = health_of(entry)
        record["auth_guidance"] = entry.describe_auth()
        record["api"] = f"runtime/v{RUNTIME_API_VERSION}"
        return record

    async def _runtime_handoff(self, params: dict[str, Any]) -> dict[str, Any]:
        from garuda.core.sessions import SessionStore
        from garuda.interfaces.runtime_cli import (
            RUNTIME_API_VERSION,
            cmd_handoff_confirm,
            cmd_handoff_preview,
        )

        session_id = params.get("session")
        target = params.get("target")
        if not session_id or not target:
            raise ValueError("params.session and params.target are required")
        registry = self._runtime_registry(params)
        resolved = registry.get(target)
        if resolved.kind.value != "native":
            from garuda.acp.catalog import discover

            found = {
                entry.runtime_id: entry
                for entry in discover(registry.manifests, disabled=registry.disabled_ids)
            }
            if not found.get(target) or not found[target].available:
                raise ValueError(f"Runtime {target!r} is unavailable")
        store = SessionStore()
        if params.get("confirm"):
            from garuda.context.pack import ContextPackManager

            manager = ContextPackManager(store.session_dir(session_id))
            text = await cmd_handoff_confirm(
                store, session_id, target,
                workspace=self._config.workspace,
                disabled=registry.disabled_ids,
                pack_manager=manager,
            )
            return {"api": f"runtime/v{RUNTIME_API_VERSION}", "acknowledged": True, "detail": text}
        return {
            "api": f"runtime/v{RUNTIME_API_VERSION}",
            "prepared": False,
            "detail": cmd_handoff_preview(store, session_id, target),
        }

    async def _runtime_recover(self, params: dict[str, Any]) -> dict[str, Any]:
        from garuda.core.sessions import SessionStore
        from garuda.interfaces.runtime_cli import RUNTIME_API_VERSION

        session_id = params.get("session")
        if not session_id:
            raise ValueError("params.session is required")
        from garuda.interfaces.runtime_cli import recover_dict

        store = SessionStore()
        payload = recover_dict(store, session_id)
        payload["api"] = f"runtime/v{RUNTIME_API_VERSION}"
        return payload

    async def _runtime_support(self, params: dict[str, Any]) -> dict[str, Any]:
        from garuda.core.sessions import SessionStore
        from garuda.interfaces.runtime_cli import RUNTIME_API_VERSION, support_bundle_dict

        session_id = params.get("session")
        if not session_id:
            raise ValueError("params.session is required")
        store = SessionStore()
        payload = support_bundle_dict(store, session_id)
        payload["api"] = f"runtime/v{RUNTIME_API_VERSION}"
        return payload

    async def _execute(self, params: dict[str, Any], events: EventStore):
        """Build run dependencies from params and execute one agent task.

        Shared by the blocking ``run`` and the async job queue. The caller owns
        the ``events`` store so a submitted job can poll it incrementally.
        """
        task = params.get("task")
        if not task:
            raise ValueError("params.task is required")

        from garuda.config.agent_home import resolve_agents_dirs

        model_name = params.get("model", self._config.model)
        agent_name = params.get("agent", self._config.agent)
        mode = params.get("mode")  # None -> honor the profile's own mode
        workspace_kind = params.get("workspace_kind", self._config.workspace_kind)
        workspace = params.get("workspace", self._config.workspace)
        agents_dir = params.get("agents_dir", self._config.agents_dir)
        mcp_config = params.get("mcp_config", self._config.mcp_config)
        # Default the profiles dirs to the workspace's `.agent/agents` (then
        # `.garuda/agents`) when unset, so both the top-level run and any forked
        # subagents resolve custom profiles the same standard way.
        agents_path = resolve_agents_dirs(workspace, agents_dir)

        profile, config, permissions, tools, agent, mcp_manager = await prepare_agent_run(
            agent_name,
            workspace=workspace,
            agents_dir=agents_path,
            mcp_config_path=mcp_config,
            mode=mode,
        )
        config.workspace_kind = workspace_kind
        config.docker_image = params.get("docker_image", self._config.docker_image)

        model = LitellmModel(
            model_name=model_name,
            reasoning_effort=config.reasoning_effort,
            thinking_budget_tokens=config.thinking_budget_tokens,
        )
        return await run_agent_task(
            task=task,
            model=model,
            agent=agent,
            tools=tools,
            config=config,
            permissions=permissions,
            workspace=workspace,
            events=events,
            workspace_kind=workspace_kind,
            docker_image=config.docker_image,
            docker_host=params.get("docker_host", self._config.docker_host),
            mcp_manager=mcp_manager,
            agents_dir=agents_path,
            resume=params.get("resume"),
        )

    async def _run(self, params: dict[str, Any]) -> dict[str, Any]:
        """Blocking run: execute to completion and return the full event list."""
        events = EventStore()
        result = await self._execute(params, events)
        return {
            "success": result.success,
            "final_message": result.final_message,
            "turns": result.turns,
            "session_id": events.session_id,
            "events": events.get_all(),
        }

    # --- Job queue (submit → poll/stream → result/cancel) ---------------------

    def _jobs(self) -> "JobManager":
        if self._job_manager is None:
            self._job_manager = JobManager(
                max_jobs=self._config.max_jobs,
                max_retained=self._config.max_retained_jobs,
                retain_seconds=self._config.job_retain_seconds,
            )
        return self._job_manager

    def _require_job(self, params: dict[str, Any]) -> "Job":
        job_id = params.get("job_id")
        if not job_id:
            raise ValueError("params.job_id is required")
        job = self._jobs().get(job_id)
        if job is None:
            raise ValueError(f"Unknown job_id: {job_id}")
        return job

    async def _submit(self, params: dict[str, Any]) -> dict[str, Any]:
        task = params.get("task")
        if not task:
            raise ValueError("params.task is required")
        events = EventStore()
        job = self._jobs().submit(
            lambda j: self._execute(params, j.events),
            task=task,
            events=events,
        )
        return {"job_id": job.id, "state": job.state.value, "session_id": job.session_id}

    def _status(self, params: dict[str, Any]) -> dict[str, Any]:
        job = self._require_job(params)
        return {
            "job_id": job.id,
            "state": job.state.value,
            "done": job.done,
            "session_id": job.session_id,
            "turns": job.result.turns if job.result else 0,
            "error": job.error,
            "event_count": job.events.count(),
        }

    def _job_events(self, params: dict[str, Any]) -> dict[str, Any]:
        job = self._require_job(params)
        cursor = int(params.get("cursor", 0) or 0)
        new_events = job.events.get_since(cursor)
        return {
            "job_id": job.id,
            "state": job.state.value,
            "done": job.done,
            "events": new_events,
            "cursor": cursor + len(new_events),
        }

    def _result(self, params: dict[str, Any]) -> dict[str, Any]:
        job = self._require_job(params)
        if not job.done:
            return {"job_id": job.id, "state": job.state.value, "ready": False}
        return {
            "job_id": job.id,
            "state": job.state.value,
            "ready": True,
            "success": job.result.success if job.result else False,
            "final_message": job.result.final_message if job.result else "",
            "turns": job.result.turns if job.result else 0,
            "session_id": job.session_id,
            "error": job.error,
            "events": job.events.get_all(),
        }

    def _cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        job = self._require_job(params)
        cancelled = self._jobs().cancel(job.id)
        return {"job_id": job.id, "cancelling": cancelled, "state": job.state.value}

    def _list_jobs(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "jobs": [
                {"job_id": j.id, "state": j.state.value, "task": j.task[:120]}
                for j in self._jobs().list()
            ]
        }

    async def serve(self) -> None:
        ensure_secure_config(self._config)
        if self._config.model_max_concurrency:
            # Cap concurrent provider calls across all in-flight jobs (pairs with
            # the job-concurrency semaphore to bound total provider load).
            from garuda.model.governor import set_max_concurrency

            set_max_concurrency(self._config.model_max_concurrency)
        server = await asyncio.start_server(
            self._connection_handler,
            self._config.host,
            self._config.port,
        )
        addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        # Same reason as the token banner: this is the last thing printed before the
        # loop blocks, so unflushed it never reaches a redirected stdout.
        print(f"Garuda JSON-RPC server listening on {addrs}", flush=True)
        async with server:
            await server.serve_forever()

    async def _connection_handler(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), timeout=REQUEST_READ_TIMEOUT
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                # Slow-loris, oversized header line, or torn request: drop it.
                return
            header, _, body_bytes = raw.partition(b"\r\n\r\n")
            headers: dict[str, str] = {}
            for line in header.split(b"\r\n")[1:]:
                if b":" in line:
                    key, _, value = line.partition(b":")
                    headers[key.decode("utf-8", "replace").strip().lower()] = value.decode(
                        "utf-8", "replace"
                    ).strip()
            # A client-declared Content-Length is untrusted input. Without a ceiling,
            # `Content-Length: 5000000000` has the server allocating toward 5 GB
            # before any auth check runs; a non-numeric value used to raise straight
            # out of the handler.
            try:
                length = int(headers.get("content-length", 0) or 0)
            except ValueError:
                _write_simple(writer, b"HTTP/1.1 400 Bad Request", "Invalid Content-Length")
                await writer.drain()
                return
            if length < 0 or length > MAX_REQUEST_BODY_BYTES:
                _write_simple(
                    writer,
                    b"HTTP/1.1 413 Payload Too Large",
                    f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes",
                )
                await writer.drain()
                return
            if len(body_bytes) > MAX_REQUEST_BODY_BYTES:
                _write_simple(
                    writer,
                    b"HTTP/1.1 413 Payload Too Large",
                    f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes",
                )
                await writer.drain()
                return
            if length and len(body_bytes) < length:
                try:
                    body_bytes += await asyncio.wait_for(
                        reader.readexactly(length - len(body_bytes)), timeout=REQUEST_READ_TIMEOUT
                    )
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    return
            try:
                payload = json.loads(body_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                response = await self.handle(payload, headers=headers)
            else:
                # Malformed body or a JSON scalar/array: return a proper parse error
                # instead of letting payload.get(...) raise and silently drop the socket.
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": PARSE_ERROR_CODE, "message": "Parse error: body must be a JSON object"},
                }
            status = b"HTTP/1.1 200 OK"
            if response.get("error", {}).get("code") == UNAUTHORIZED_CODE:
                status = b"HTTP/1.1 401 Unauthorized"
            body = json.dumps(response, default=str).encode("utf-8")
            writer.write(
                status
                + b"\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def serve(config: ServerConfig) -> None:
    server = JsonRpcServer(config)
    await server.serve()
