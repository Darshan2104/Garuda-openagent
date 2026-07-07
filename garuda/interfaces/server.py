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

logger = logging.getLogger(__name__)

UNAUTHORIZED_CODE = -32001
PARSE_ERROR_CODE = -32700
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Drop a client that hasn't sent a complete request within this many seconds
# (slow-loris protection).
REQUEST_READ_TIMEOUT = 30.0


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    model: str = "openai/gpt-4o-mini"
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
    print(
        "[garuda serve] No token configured; generated one for this session.\n"
        f"  Authorization: Bearer {config.token}"
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
        from importlib.metadata import version

        try:
            pkg_version = version("garuda-openagent")
        except Exception:
            pkg_version = "unknown"
        return {"status": "ok", "version": pkg_version}

    async def _execute(self, params: dict[str, Any], events: EventStore):
        """Build run dependencies from params and execute one agent task.

        Shared by the blocking ``run`` and the async job queue. The caller owns
        the ``events`` store so a submitted job can poll it incrementally.
        """
        task = params.get("task")
        if not task:
            raise ValueError("params.task is required")

        from garuda.config.agent_home import resolve_agents_dir

        model_name = params.get("model", self._config.model)
        agent_name = params.get("agent", self._config.agent)
        mode = params.get("mode")  # None -> honor the profile's own mode
        workspace_kind = params.get("workspace_kind", self._config.workspace_kind)
        workspace = params.get("workspace", self._config.workspace)
        agents_dir = params.get("agents_dir", self._config.agents_dir)
        mcp_config = params.get("mcp_config", self._config.mcp_config)
        # Default the profiles dir to the workspace's `.agent/agents` when unset,
        # so both the top-level run and any forked subagents resolve custom profiles.
        agents_path = resolve_agents_dir(workspace, agents_dir)

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
            self._job_manager = JobManager(max_jobs=self._config.max_jobs)
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
        print(f"Garuda JSON-RPC server listening on {addrs}")
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
            length = int(headers.get("content-length", 0) or 0)
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
