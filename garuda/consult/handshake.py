"""Giving an ACP asker the ``consult`` tool, only where it is proved (plan task G.3, #170).

``AcpConsultHost`` is what an ACP runtime asks, after ``initialize`` and before
``session/new``, for the ``mcpServers`` entry to send. It answers with an empty list unless the
exact adapter identity that connected passes :class:`~garuda.consult.transports.TransportPolicy`
(all three gates supported and implemented), the role has consult targets, and the broker
could start. Every other case leaves the adapter exactly as it was and records why in
``unavailable``; nothing here is a fallback to a weaker mechanism.

Permission requests are auto-allowed only when a per-adapter reader of the *structured*
request names the ``garuda-consult`` server and its ``consult`` tool for this live session.
A tool's display title is never consulted, and any adapter without such a reader gets the
ordinary approval flow.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from garuda.consult.broker import ConsultBroker
from garuda.consult.transports import DEFAULT, TransportPolicy

logger = logging.getLogger(__name__)

SERVER_NAME = "garuda-consult"
TOOL_NAME = "consult"


def trusted_command() -> tuple[str, list[str]]:
    """An absolute executable that runs the MCP server: the ``garuda`` script installed beside
    this interpreter if there is one, else this interpreter running the module."""
    interpreter = Path(sys.executable)
    script = interpreter.parent / "garuda"
    if script.is_file() and os.access(script, os.X_OK):
        return str(script.absolute()), ["_consult-mcp"]
    return str(interpreter.absolute()), ["-m", "garuda.interfaces.consult_mcp"]


def server_entry(endpoint) -> dict:
    """The ``session/new`` ``mcpServers`` item. Holds the token: build it only after the token
    is registered with the redactor and never log it (use :func:`describe`)."""
    command, args = trusted_command()
    return {"name": SERVER_NAME, "command": command, "args": args,
            "env": [{"name": "GARUDA_CONSULT_ENDPOINT", "value": endpoint.path},
                    {"name": "GARUDA_CONSULT_TOKEN", "value": endpoint.token}]}


def describe(entry: dict) -> dict:
    """The same entry with every environment value masked, for logs and exports."""
    shown = dict(entry)
    shown["env"] = [{"name": item.get("name"), "value": "[masked]"}
                    for item in entry.get("env", [])]
    return shown


class AcpConsultHost:
    def __init__(self, service, *, targets, workspace: str, root_session: str | None = None,
                 policy: TransportPolicy | None = None):
        self._service = service
        self._targets = list(targets or [])
        self._workspace = workspace
        self._root = root_session
        self._policy = policy or DEFAULT
        self._broker: ConsultBroker | None = None
        self.unavailable = ""

    @property
    def exposed(self) -> bool:
        return self._broker is not None

    async def mcp_servers(self, agent_info, *, session_id: str, pid: int | None) -> list[dict]:
        info = agent_info if isinstance(agent_info, dict) else {}
        package, version = str(info.get("name") or ""), str(info.get("version") or "")
        if not self._targets:
            return self._refuse("this role has no consult targets")
        decision = self._policy.exposure(package, version)
        if not decision.exposed:
            return self._refuse(decision.reason)
        if pid is None:
            return self._refuse("the adapter process is not known")
        quiesce = self._policy.quiesce[(package, version)]
        try:
            if self._broker is None:
                self._broker = ConsultBroker(
                    self._service, asker_session=session_id, root_session=self._root or session_id,
                    workspace=self._workspace, pid=pid, quiesce=quiesce)
                endpoint = await self._broker.start()
            else:  # a resumed session: the earlier endpoint stops working
                endpoint = self._broker.rotate()
        except Exception:
            logger.warning("consult endpoint could not start; the tool is not exposed",
                           exc_info=False)
            await self.close()
            return self._refuse("the consult endpoint could not start")
        self.unavailable = ""
        return [server_entry(endpoint)]

    def allows_permission(self, params, agent_info, *, agent_session_id: str | None) -> bool:
        """Auto-allow only this tool, identified structurally, for the live session."""
        if self._broker is None or not isinstance(params, dict) or not agent_session_id:
            return False
        if params.get("sessionId") != agent_session_id:
            return False
        info = agent_info if isinstance(agent_info, dict) else {}
        key = (str(info.get("name") or ""), str(info.get("version") or ""))
        reader = self._policy.identity.get(key)
        if reader is None:
            return False
        try:
            return tuple(reader(params) or ()) == (SERVER_NAME, TOOL_NAME)
        except Exception:
            return False

    def _refuse(self, reason: str) -> list[dict]:
        self.unavailable = reason
        return []

    async def close(self) -> None:
        broker, self._broker = self._broker, None
        if broker is not None:
            await broker.close()
