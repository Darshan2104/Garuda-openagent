"""Bounded stdio cells; confinement and process lifetime belong to environments."""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from garuda.workspace.local import _kill_process_tree
from garuda.workspace.settlement import settle_task

# Self-contained: neither Garuda nor workspace dependencies are imported in a cell.
WORKER = r'''
import json, sys, traceback
wire_in, wire_out = sys.stdin, sys.stdout

def send(value):
    wire_out.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
    wire_out.flush()

class Output:
    def write(self, text):
        for start in range(0, len(text), 1024):
            send({"output": text[start:start + 1024]})
        return len(text)
    def flush(self):
        pass

def tool(name, arguments):
    send({"tool": name, "arguments": arguments})
    line = wire_in.readline()
    if not line:
        raise RuntimeError("Cell bridge closed")
    return json.loads(line)

request = json.loads(wire_in.readline())
sys.stdout = sys.stderr = Output()
try:
    scope = {"inputs": request["inputs"], "tool": tool}
    exec(compile(request["code"], "<garuda-cell>", "exec"), scope)
    send({"result": scope.get("result")})
except BaseException:
    send({"error": traceback.format_exc(limit=5)})
'''


@runtime_checkable
class CellEnvironment(Protocol):
    """Implemented by trusted confinement owners, never an unconfined adapter."""

    def open_code_cell(self) -> contextlib.AbstractAsyncContextManager[asyncio.subprocess.Process]: ...


@dataclass(frozen=True)
class CellLimits:
    timeout: float = 10.0
    output_bytes: int = 32_768
    nested_calls: int = 8


class CellError(RuntimeError):
    pass


async def finish_cleanup(task: asyncio.Task) -> None:
    """Settle cleanup without swallowing cancellation arriving during cleanup."""
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            await settle_task(task)
        except Exception as exc:
            raise cancelled from exc
        raise


@asynccontextmanager
async def uncharged(budget: asyncio.Timeout, *, resume_by: float | None = None) -> AsyncIterator[None]:
    """Pause a cell's own compute budget while one nested tool call runs.

    The nested call stays bounded by its own tool timeout and approval path; on
    return the unspent budget resumes, capped at ``resume_by`` (loop time).
    """
    loop = asyncio.get_running_loop()
    remaining = budget.when() - loop.time()
    budget.reschedule(None)
    try:
        yield
    finally:
        when = loop.time() + remaining
        budget.reschedule(when if resume_by is None else min(when, resume_by))


@asynccontextmanager
async def cell_process(
    argv: list[str], *, env: dict[str, str] | None = None,
) -> AsyncIterator[asyncio.subprocess.Process]:
    """Own launch, pipes and process group, including cancellation during launch.

    Only environment owners may supply argv here. This helper is not confinement.
    Docker owners additionally remove the container; bwrap owns a PID namespace.
    """
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, env=env,
        limit=65_536,
    ))

    async def cleanup():
        process = await launch
        _kill_process_tree(process)
        await asyncio.wait_for(process.communicate(), 5)
        if process.stdin is not None:
            process.stdin.close()

    try:
        yield await asyncio.shield(launch)
    finally:
        await finish_cleanup(asyncio.create_task(cleanup()))


async def exchange_cell(
    process: asyncio.subprocess.Process, code: str, inputs: dict, limits: CellLimits,
    callback: Callable[[str, dict, int], Awaitable[dict]],
) -> dict:
    """Exchange bounded JSON frames with an untrusted worker, never replaying calls."""
    output: list[str] = []
    used = 0
    calls = 0
    callback_active = False

    def invalid_constant(value):
        raise ValueError(f"Non-JSON constant: {value}")

    def retain(text: str) -> None:
        nonlocal used
        used += len(text.encode("utf-8"))
        if used > limits.output_bytes:
            raise CellError("Cell output byte limit exceeded")
        output.append(text)

    async def stderr():
        while chunk := await process.stderr.read(1024):
            retain(chunk.decode("utf-8", errors="replace"))

    async def exchange():
        nonlocal calls, used, callback_active
        process.stdin.write((json.dumps({"code": code, "inputs": inputs}) + "\n").encode())
        await process.stdin.drain()
        while True:
            try:
                line = await process.stdout.readline()
            except ValueError as exc:
                raise CellError("Cell frame byte limit exceeded") from exc
            if not line:
                raise CellError("Cell exited without a result; side effects unknown; no replay")
            try:
                frame = json.loads(line, parse_constant=invalid_constant)
            except (ValueError, UnicodeError) as exc:
                raise CellError("Invalid cell protocol") from exc
            if not isinstance(frame, dict):
                raise CellError("Invalid cell frame")
            if set(frame) == {"output"} and isinstance(frame["output"], str):
                retain(frame["output"])
            elif set(frame) == {"tool", "arguments"}:
                calls += 1
                if calls > limits.nested_calls:
                    raise CellError("Cell nested-call limit exceeded")
                if not isinstance(frame["tool"], str) or not isinstance(frame["arguments"], dict):
                    raise CellError("Invalid cell tool request")
                if process.returncode is not None:
                    raise CellError("Cell exited before tool dispatch; no replay")
                callback_active = True
                try:
                    # Nested dispatch (including approval waits) is not cell compute.
                    async with uncharged(budget):
                        reply = await callback(frame["tool"], frame["arguments"], calls)
                finally:
                    callback_active = False
                encoded = (json.dumps(reply, ensure_ascii=True) + "\n").encode()
                if len(encoded) > 65_536:
                    raise CellError("Cell bridge response byte limit exceeded")
                process.stdin.write(encoded)
                await process.stdin.drain()
            elif set(frame) in ({"result"}, {"error"}):
                encoded = json.dumps(frame, ensure_ascii=False)
                used += len(encoded.encode("utf-8"))
                if used > limits.output_bytes:
                    raise CellError("Cell output byte limit exceeded")
                process.stdin.close()
                if await process.stdout.read(1):
                    raise CellError("Unexpected cell output after result")
                await process.wait()
                if process.returncode:
                    raise CellError("Cell exited unsuccessfully; side effects unknown; no replay")
                return {**frame, "nested_calls": calls}
            else:
                raise CellError("Invalid cell frame")

    budget = asyncio.timeout(limits.timeout)
    exchange_task = asyncio.create_task(exchange())
    stderr_task = asyncio.create_task(stderr())
    exit_task = asyncio.create_task(process.wait())
    tasks = (exchange_task, stderr_task, exit_task)
    try:
        async with budget:
            pending = set(tasks)
            while not exchange_task.done():
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if stderr_task in done:
                    stderr_task.result()
                if exit_task in done and callback_active:
                    raise CellError("Cell exited during tool dispatch; side effects unknown; no replay")
            result = await exchange_task
            await stderr_task
            result["output"] = "".join(output)
            return result
    except TimeoutError as exc:
        raise CellError("Cell wall-time limit exceeded; side effects unknown; no replay") from exc
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def minimal_cell_env() -> dict[str, str]:
    return {"PATH": os.defpath, "LANG": "C.UTF-8"}
