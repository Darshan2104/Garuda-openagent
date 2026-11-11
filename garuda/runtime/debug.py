"""Workspace-owned, launch-only Python debugging with stop-scoped evidence."""

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Protocol

from garuda.runtime.debug_adapter import DebugError
from garuda.workspace.protocol import WorkspaceAccessDenied
from garuda.workspace.settlement import settle_task


class DebugService(Protocol):
    async def launch(self, session_id: str, arguments: dict, limits: dict) -> dict: ...
    async def inspect(self, session_id: str, arguments: dict) -> dict: ...
    async def control(self, session_id: str, arguments: dict) -> dict: ...
    async def reap(self, session_id: str) -> list[str]: ...
    async def close(self) -> None: ...


UNSUPPORTED = "attach, evaluate, getters, setVariable, raw requests, and memory are unsupported"


def workspace_target(root: str, path: str) -> Path:
    base = Path(root).resolve()
    target = (base / path).resolve(strict=True)
    if not target.is_relative_to(base):
        raise WorkspaceAccessDenied("debug target is outside the workspace")
    return target


def _items(response, key):
    value = response.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise DebugError(f"adapter returned unknown {key}")
    return value


class PythonDebugSession:
    def __init__(self, adapter, session_id, limits):
        self.id = uuid.uuid4().hex
        self.session_id = session_id
        self.adapter = adapter
        self.limits = limits
        self.capabilities = {}
        self.handles = {}
        self.lock = asyncio.Lock()
        self.cleanup = None

    def evidence(self, data=None):
        return {"schema": "garuda.debug", "version": 1, "debug_session_id": self.id,
                "session_id": self.session_id, "state": self.adapter.state,
                "stop_generation": self.adapter.generation, "data": data or {},
                "adapter_error": self.adapter.failure,
                "cleanup": self.cleanup, "verification": "runtime observation only"}

    def handle(self, value, kind, depth=0):
        if type(value) is not int or value < (1 if kind == "variables" else 0):
            raise DebugError("adapter returned invalid debug reference")
        if len(self.handles) >= 1000:
            raise DebugError("stop handle limit reached; resume or disconnect")
        key = uuid.uuid4().hex
        self.handles[key] = (self.adapter.generation, kind, value, depth)
        return key

    def resolve(self, key, kind):
        entry = self.handles.get(key)
        if (entry is None or entry[0] != self.adapter.generation or entry[1] != kind
                or self.adapter.state != "stopped"):
            raise DebugError("stale or foreign debug handle; inspect the current stop again")
        return entry[2:]

    async def close(self):
        try:
            await self.adapter.close()
            self.cleanup = "verified: no live members in owned POSIX session"
        except Exception as exc:
            self.cleanup = str(exc) if str(exc).startswith("cleanup unproved") else f"cleanup unproved: {exc}"
            raise DebugError(self.cleanup) from exc


class LocalDebugService:
    def __init__(self, env):
        self.env = env
        self.sessions = {}

    def get(self, session_id, debug_session_id):
        session = self.sessions.get(debug_session_id)
        if session is None or session.session_id != session_id:
            raise DebugError("unknown or foreign debug session")
        return session

    async def launch(self, session_id, arguments, limits):
        if set(arguments) - {"program", "args", "breakpoints"}:
            raise DebugError(UNSUPPORTED)
        program = workspace_target(self.env.workspace_root, arguments["program"])
        if not program.is_file() or program.suffix != ".py":
            raise DebugError("launch requires a workspace Python file")
        args = arguments.get("args", [])
        lines = arguments.get("breakpoints", [])
        if (not isinstance(args, list) or len(args) > 100
                or any(not isinstance(a, str) or len(a) > 4096 for a in args)):
            raise DebugError("args must be at most 100 bounded strings")
        if (not isinstance(lines, list) or len(lines) > 100
                or any(type(n) is not int or n <= 0 for n in lines)):
            raise DebugError("breakpoints must be at most 100 positive line numbers")
        if sum(s.session_id == session_id and not s.cleanup for s in self.sessions.values()) >= 4:
            raise DebugError("debug session limit reached")
        if self.env._debug_owners.get(session_id):
            raise DebugError("debug launch cleanup pending or unproved")
        if any(s.session_id == session_id and s.cleanup and s.cleanup.startswith("cleanup unproved")
               for s in self.sessions.values()):
            raise DebugError("cleanup unproved: new launches refused until session teardown")
        if len(self.sessions) >= 32:
            for key, old in list(self.sessions.items()):
                if old.cleanup and old.cleanup.startswith("verified:"):
                    self.sessions.pop(key)
                    break
            else:
                raise DebugError("workspace debug session limit reached")
        adapter = await self.env.launch_debug_adapter(session_id)
        session = PythonDebugSession(adapter, session_id, limits)
        self.sessions[session.id] = session
        self.env._debug_owners[session_id].remove(adapter.owner)
        # Unexpected EOF/crash owns cleanup even if the agent never calls again.
        asyncio.create_task(self._watch(session))
        try:
            session.capabilities = await adapter.request("initialize", {
                "adapterID": "python", "clientID": "garuda", "linesStartAt1": True,
                "columnsStartAt1": True, "pathFormat": "path",
                "supportsVariableType": True, "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": False,
            })
            launch = asyncio.create_task(adapter.request("launch", {
                "name": "Garuda Python", "type": "python", "request": "launch",
                "program": str(program), "cwd": self.env.workspace_root,
                "args": args, "console": "internalConsole", "redirectOutput": True,
                "python": [sys.executable, "-I"],
                "stopOnEntry": not bool(lines), "justMyCode": True, "subProcess": False,
                "showReturnValue": False,
                "variablePresentation": {"all": "hide"},
            }))
            try:
                await asyncio.wait_for(adapter.initialized.wait(), 15)
                if adapter.failure:
                    raise DebugError(adapter.failure)
                breakpoints = await adapter.request("setBreakpoints", {
                    "source": {"path": str(program)},
                    "breakpoints": [{"line": n} for n in lines],
                })
                await adapter.request("configurationDone")
                await launch
            finally:
                if not launch.done():
                    launch.cancel()
                await asyncio.gather(launch, return_exceptions=True)
        except BaseException as failed:
            cleanup = asyncio.create_task(session.close())
            try:
                await settle_task(cleanup)
            except Exception as exc:
                if isinstance(failed, asyncio.CancelledError):
                    raise failed from exc
                raise
            raise

        # A completed launch can keep running beyond the bounded stop wait.
        # Keep its identity available for status, later stops and disconnect.
        if adapter.state == "starting":
            adapter.state = "running"
        try:
            await adapter.wait_stop()
        except DebugError as exc:
            raise DebugError(f"{exc}; debug_session_id={session.id}") from exc
        return session.evidence({"capabilities": session.capabilities,
                                 "unsupported": UNSUPPORTED, **breakpoints})

    async def _watch(self, session):
        await session.adapter.reader
        try:
            await session.close()
        except Exception:
            pass  # Kept on the session; reap reports uncertainty to the lease owner.

    async def inspect(self, session_id, arguments):
        session = self.get(session_id, arguments["debug_session_id"])
        async with session.lock:
            operation = arguments["operation"]
            if set(arguments) - {"debug_session_id", "operation", "handle", "count", "depth"}:
                raise DebugError(UNSUPPORTED)
            adapter = session.adapter
            if operation == "status":
                return session.evidence({"capabilities": session.capabilities,
                                         "unsupported": UNSUPPORTED})
            if operation == "output":
                cap = session.limits["output_chars"]
                return session.evidence({"output": adapter.output[-cap:],
                                         "truncated": adapter.output_truncated or len(adapter.output) > cap})
            if operation not in {"stackTrace", "scopes", "variables"}:
                raise DebugError(UNSUPPORTED)
            if adapter.failure:
                raise DebugError(adapter.failure)
            if adapter.state != "stopped":
                raise DebugError("inspection requires a stopped debuggee")
            count = arguments.get("count", session.limits["max_variables"])
            depth = arguments.get("depth", 1)
            if type(count) is not int or count <= 0 or type(depth) is not int or depth <= 0:
                raise DebugError("count and depth must be positive integers")
            count = min(count, session.limits["max_variables"])
            depth = min(depth, session.limits["max_depth"])
            if operation == "stackTrace":
                response = await adapter.request("stackTrace", {"threadId": adapter.thread,
                                                                 "startFrame": 0, "levels": min(count, 32)})
                frames = [{"handle": session.handle(f["id"], "frame"),
                           "name": str(f.get("name", ""))[:256], "line": f.get("line"),
                           "path": str((f.get("source") or {}).get("path", ""))[:1024]}
                          for f in _items(response, "stackFrames")[:min(count, 32)]]
                total = response.get("totalFrames")
                data = {"stackFrames": frames, "totalFrames": total,
                        "truncated": len(frames) < total if type(total) is int else None}
            elif operation == "scopes":
                frame, _ = session.resolve(arguments.get("handle"), "frame")
                response = await adapter.request("scopes", {"frameId": frame})
                scopes = _items(response, "scopes")
                data = {"scopes": [{"name": str(s.get("name", ""))[:256],
                                    "handle": session.handle(s["variablesReference"], "variables")}
                                   for s in scopes[:16]],
                        "truncated": len(scopes) > 16}
            else:
                reference, base_depth = session.resolve(arguments.get("handle"), "variables")
                budget = [count]
                variables = await self._variables(session, reference, base_depth, depth, budget)
                data = {"variables": variables, "bounded": True, "omitted_variables": None}
            # Responses remain valid JSON when the character cap is reached.
            data.setdefault("truncated", None)
            for key in ("variables", "stackFrames", "scopes"):
                while data.get(key) and len(json.dumps(session.evidence(data))) > session.limits["max_chars"]:
                    data[key].pop()
                    data["truncated"] = True
            return session.evidence(data)

    async def _variables(self, session, reference, base_depth, depth, budget):
        if base_depth >= session.limits["max_depth"]:
            raise DebugError("variable depth limit reached")
        response = await session.adapter.request("variables", {
            "variablesReference": reference, "start": 0, "count": budget[0],
        })
        result = []
        for variable in _items(response, "variables"):
            if not budget[0]:
                break
            budget[0] -= 1
            if any(not isinstance(variable.get(key), str) for key in ("name", "value")):
                raise DebugError("adapter returned invalid variable evidence")
            item = {key: (variable[key][:256] if isinstance(variable.get(key), str) else None)
                    for key in ("name", "value", "type")}
            item["value_truncated"] = len(variable["value"]) > 256
            child = variable.get("variablesReference")
            if type(child) is not int or child < 0:
                raise DebugError("adapter returned unknown variable reference")
            # Arbitrary object expansion can invoke descriptors/getters. Expose
            # only adapter-rendered summaries and builtin container expansion.
            expandable = item["type"] in {"list", "tuple", "dict", "set", "frozenset"}
            if child and expandable and base_depth + 1 < session.limits["max_depth"]:
                item["handle"] = session.handle(child, "variables", base_depth + 1)
                if depth > 1 and budget[0]:
                    item["children"] = await self._variables(
                        session, child, base_depth + 1, depth - 1, budget)
            elif child:
                item["expansion"] = "unsupported or depth limited"
            result.append(item)
        return result

    async def control(self, session_id, arguments):
        session = self.get(session_id, arguments["debug_session_id"])
        if set(arguments) != {"debug_session_id", "operation"}:
            raise DebugError(UNSUPPORTED)
        operation = arguments["operation"]
        if operation not in {"next", "stepIn", "stepOut", "continue", "disconnect", "terminate"}:
            raise DebugError(UNSUPPORTED)
        async with session.lock:
            if operation in {"disconnect", "terminate"}:
                await session.close()
                session.adapter.state = "closed"
                return session.evidence()
            adapter = session.adapter
            if adapter.state != "stopped":
                raise DebugError("execution control requires a stopped debuggee")
            session.handles.clear()
            thread = adapter.thread
            adapter.state = "running"
            adapter.generation += 1
            try:
                await adapter.request(operation, {"threadId": thread, "singleThread": False})
                if operation != "continue":
                    await adapter.wait_stop()
            except BaseException:
                if not adapter.failure:
                    adapter.state = "unknown"
                raise
            return session.evidence()

    async def reap(self, session_id):
        survivors = []
        for key, session in list(self.sessions.items()):
            if session.session_id != session_id:
                continue
            try:
                await session.close()
            except Exception:
                survivors.append(f"debug:{key}: cleanup unproved")
            else:
                self.sessions.pop(key, None)
        survivors.extend(await self.env.reap_debug_owners(session_id))
        return survivors

    async def close(self):
        survivors = []
        for session_id in {s.session_id for s in self.sessions.values()} | set(self.env._debug_owners):
            survivors.extend(await self.reap(session_id))
        if survivors:
            raise DebugError("; ".join(survivors))
