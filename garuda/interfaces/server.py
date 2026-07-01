"""JSON-RPC HTTP server for IDE and automation integrations."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.interfaces.runner import run_agent_task
from garuda.model.litellm_model import LitellmModel


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


class JsonRpcServer:
    """Minimal JSON-RPC 2.0 server over HTTP POST."""

    def __init__(self, config: ServerConfig):
        self._config = config

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        req_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        try:
            if method == "health":
                result = await self._health()
            elif method == "run":
                result = await self._run(params)
            elif method == "list_agents":
                from garuda.agents.loader import list_profiles

                result = {"agents": list_profiles()}
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

    async def _run(self, params: dict[str, Any]) -> dict[str, Any]:
        task = params.get("task")
        if not task:
            raise ValueError("params.task is required")

        model_name = params.get("model", self._config.model)
        agent_name = params.get("agent", self._config.agent)
        mode = params.get("mode", "standard")
        workspace_kind = params.get("workspace_kind", self._config.workspace_kind)

        profile = load_profile(agent_name)
        config = profile.to_agent_config()
        config.mode = mode
        config.workspace_kind = workspace_kind
        config.docker_image = params.get("docker_image", self._config.docker_image)

        model = LitellmModel(model_name=model_name)
        permissions = PermissionEngine(mode=config.permission_mode, tool_rules=profile.tool_rules)
        agent = create_agent(profile.name, mode=mode)
        events = EventStore()

        from garuda.tools import build_toolkit

        tools, mcp_manager = await build_toolkit(profile.tools, config.mcp_config_path)
        result = await run_agent_task(
            task=task,
            model=model,
            agent=agent,
            tools=tools,
            config=config,
            permissions=permissions,
            workspace=params.get("workspace", self._config.workspace),
            events=events,
            workspace_kind=workspace_kind,
            docker_image=config.docker_image,
            docker_host=params.get("docker_host", self._config.docker_host),
            mcp_manager=mcp_manager,
        )

        return {
            "success": result.success,
            "final_message": result.final_message,
            "turns": result.turns,
            "session_id": events.session_id,
            "events": events.get_all(),
        }

    async def serve(self) -> None:
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
            raw = await reader.readuntil(b"\r\n\r\n")
            header, _, body_bytes = raw.partition(b"\r\n\r\n")
            if b"Content-Length:" in header:
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                        body_bytes += await reader.readexactly(length)
                        break
            payload = json.loads(body_bytes.decode("utf-8"))
            response = await self.handle(payload)
            body = json.dumps(response).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
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
