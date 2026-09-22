"""TEMPORARY Ubuntu reaping diagnostics (to be deleted before merge).

Reproduces test_killing_a_task_reaps_its_children with full process-tree
dumps so CI logs show why the survivor escapes the group kill.
"""

import asyncio
import os
import signal
import subprocess
from pathlib import Path

from garuda.core.side_effects import SideEffectLedger
from garuda.tools.background import BashBackgroundTool, KillTaskTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment


def _sh(cmd: str) -> str:
    try:
        out = subprocess.run(
            ["sh", "-c", cmd], capture_output=True, text=True, timeout=15
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"<sh failed: {exc}>"


def _dump(label: str, pid: str, script: str) -> str:
    lines = [f"===== {label} ====="]
    lines.append(f"-- leader pid={pid} --")
    lines.append(
        _sh(
            f"ps -o pid=,ppid=,pgid=,sid=,stat=,args= -p {pid} 2>&1; "
            f"echo '--- pgrep -P {pid}:'; pgrep -P {pid} 2>&1; echo 'rc=$?'; "
            f"echo '--- pgrep -g {pid}:'; pgrep -g {pid} 2>&1; echo 'rc=$?'"
        )
    )
    lines.append("-- full table (script-related + sh/sleep/python) --")
    lines.append(
        _sh(
            "ps -Ao pid=,ppid=,pgid=,sid=,stat=,args= 2>/dev/null "
            "| grep -E 'garuda-reap-dbg|sleep 60|sh -c' | grep -v grep | head -30"
        )
    )
    lines.append(f"-- pattern probe for {script} --")
    lines.append(_sh(f"pgrep -af '{script}' 2>&1; echo 'rc=$?'"))
    return "\n".join(lines)


async def test_ubuntu_reap_debug_dump(tmp_path: Path):
    script = tmp_path / "garuda-reap-dbg-probe.sh"
    script.write_text("sleep 60\n")
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="bg-dbg")
    probe = SideEffectLedger()._probe_pattern

    started = await BashBackgroundTool().execute(
        {"command": f"sleep 0.1; sh {script}"}, env, ctx
    )
    task_id = started.content.split("task ")[1].split(" ")[0]
    from garuda.tools import background as bgmod

    task = bgmod._TASKS[(ctx.session_id, task_id)]
    pid = task.pid
    await asyncio.sleep(0.5)
    print(_dump("BEFORE KILL", pid, str(script)))

    # Manual kill with per-target error logging (apply_kill_tree swallows OSError).

    seen: set[int] = set()
    ordered: list[int] = []

    def collect(x: int) -> None:
        if x in seen or x <= 1:
            return
        seen.add(x)
        for flag in ("-P", "-g"):
            try:
                out = subprocess.check_output(
                    ["pgrep", flag, str(x)], text=True, stderr=subprocess.DEVNULL
                )
            except Exception:  # noqa: BLE001
                out = ""
            for raw in out.split():
                try:
                    collect(int(raw))
                except ValueError:
                    pass
        ordered.append(x)

    collect(int(pid))
    print(f"COLLECTED ordered={ordered} seen={sorted(seen)}")
    for target in ordered:
        for neg in (True, False):
            t = -target if neg else target
            try:
                os.kill(t, signal.SIGKILL)
                print(f"os.kill({t}, KILL) -> delivered")
            except OSError as exc:
                print(f"os.kill({t}, KILL) -> {type(exc).__name__}: {exc}")
    await asyncio.sleep(0.5)
    print(_dump("AFTER MANUAL KILL", pid, str(script)))
    survivors = await probe(env, str(script))
    print(f"SURVIVORS={survivors}")
    for s in survivors:
        print(_sh(f"ps -o pid=,ppid=,pgid=,sid=,stat=,args= -p {s} 2>&1"))
        print(_sh(f"cat /proc/{s}/status 2>&1 | head -8"))
    await KillTaskTool().execute({"task_id": task_id}, env, ctx)
    raise AssertionError(f"DEBUG DUMP DONE survivors={survivors}")
