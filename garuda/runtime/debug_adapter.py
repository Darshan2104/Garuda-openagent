"""Bounded DAP transport for an environment-owned Python adapter."""

import asyncio
import json


class DebugError(RuntimeError):
    pass


MAX_FRAME = 1_048_576
MAX_OUTPUT = 20_000
REQUEST_TIMEOUT = 15
STOP_TIMEOUT = 15


class DebugAdapter:
    def __init__(self, process, owner):
        self.process = process
        self.owner = owner
        self.sequence = 0
        self.pending = {}
        self.initialized = asyncio.Event()
        self.changed = asyncio.Event()
        self.state = "starting"
        self.thread = None
        self.generation = 0
        self.output = ""
        self.output_truncated = False
        self.failure = None
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._stderr())

    def capture(self, value):
        value = str(value)
        self.output_truncated |= len(self.output) + len(value) > MAX_OUTPUT
        self.output = (self.output + value)[-MAX_OUTPUT:]

    async def _stderr(self):
        while data := await self.process.stderr.read(4096):
            self.capture(data.decode(errors="replace"))

    async def _read(self):
        try:
            while True:
                header = await self.process.stdout.readuntil(b"\r\n\r\n")
                if len(header) > 1024:
                    raise DebugError("DAP header too large")
                fields = header.decode("ascii").strip().split("\r\n")
                if len(fields) != 1 or not fields[0].startswith("Content-Length: "):
                    raise DebugError("invalid DAP header")
                length = int(fields[0].split(": ")[1])
                if not 0 < length <= MAX_FRAME:
                    raise DebugError("DAP frame exceeds limit")
                message = json.loads(await self.process.stdout.readexactly(length))
                if not isinstance(message, dict):
                    raise DebugError("DAP message must be an object")
                kind = message.get("type")
                if kind == "response":
                    future = self.pending.get(message.get("request_seq"))
                    if future is not None and not future.done():
                        if message.get("success") is True:
                            body = message.get("body", {})
                            if not isinstance(body, dict):
                                raise DebugError("DAP response body must be an object")
                            future.set_result(body)
                        else:
                            future.set_exception(DebugError(str(message.get("message"))[:1000]))
                elif kind == "event":
                    event, body = message.get("event"), message.get("body") or {}
                    if not isinstance(body, dict):
                        raise DebugError("DAP event body must be an object")
                    if event == "initialized":
                        self.initialized.set()
                    elif event in {"stopped", "continued", "terminated", "exited"}:
                        self.state = {"stopped": "stopped", "continued": "running",
                                      "terminated": "terminated", "exited": "terminated"}[event]
                        self.thread = body.get("threadId") if event == "stopped" else None
                        self.generation += 1
                        self.changed.set()
                    elif event == "output":
                        self.capture(body.get("output", ""))
                elif kind == "request":
                    # No terminal launch, child-session attach, or adapter-chosen execution.
                    await self._send({"type": "response", "request_seq": message["seq"],
                                      "command": message.get("command"), "success": False,
                                      "message": "unsupported adapter request"})
                else:
                    raise DebugError("invalid DAP message")
        except (Exception, asyncio.CancelledError) as exc:
            self.failure = f"adapter disconnected: {type(exc).__name__}"
            self.state = "disconnected"
            self.generation += 1
            self.changed.set()
            self.initialized.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(DebugError(self.failure))

    async def _send(self, message):
        self.sequence += 1
        message["seq"] = self.sequence
        data = json.dumps(message, separators=(",", ":")).encode()
        self.process.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        await self.process.stdin.drain()
        return self.sequence

    async def request(self, command, arguments=None):
        if self.failure:
            raise DebugError(self.failure)
        # Register before yielding to the pipe so even an immediate response is owned.
        sequence = self.sequence + 1
        future = asyncio.get_running_loop().create_future()
        self.pending[sequence] = future
        try:
            await self._send({"type": "request", "command": command, "arguments": arguments or {}})
            return await asyncio.wait_for(future, REQUEST_TIMEOUT)
        except TimeoutError as exc:
            if self.state not in {"terminated", "disconnected"}:
                self.state = "unknown"
                self.thread = None
                self.generation += 1
            raise DebugError(f"DAP {command} request timed out; debug state is {self.state}") from exc
        finally:
            self.pending.pop(sequence, None)
            if not future.done():
                future.cancel()

    async def wait_stop(self):
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                while self.state not in {"stopped", "terminated", "disconnected"}:
                    self.changed.clear()
                    await self.changed.wait()
        except TimeoutError as exc:
            raise DebugError(f"Timed out waiting for debuggee to stop; debug state is {self.state}") from exc
        if self.failure:
            raise DebugError(self.failure)

    async def close(self):
        try:
            await self.owner.close()
        finally:
            for task in (self.reader, self.stderr):
                task.cancel()
            await asyncio.gather(self.reader, self.stderr, return_exceptions=True)
