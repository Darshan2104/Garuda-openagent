"""Provider-free editing-worker measurement development script for #384.

Real native children, SpendGate settlement and Git integration; synthetic model
usage, process start identity and Docker check dispatch. No speed/quality claim.
Run only in a dedicated process. This script substitutes Docker dispatch and
process identity and redirects lease, recovery, session and settings stores via
process-wide patches. Never import it into a process doing real agent work.

Usage::

    python scripts/measure_editing_fanout.py --delay 0.05 --out editing-fanout.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from garuda.core.events import EventStore  # noqa: E402
from garuda.core.permissions import PermissionEngine  # noqa: E402
from garuda.core.spend import SpendGate, usage_tokens  # noqa: E402
from garuda.core.subagent import DelegationBudget, SubagentRunner  # noqa: E402
from garuda.model.protocol import ModelResponse  # noqa: E402
from garuda.model.script_model import ScriptModel  # noqa: E402
from garuda.types import Role, ToolCall  # noqa: E402

TASKS = ("alpha", "beta", "gamma", "delta")
USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost_usd": 0.0}


class DelayedTaskScriptModel(ScriptModel):
    """Route each child's two calls by task, regardless of scheduling order."""

    def __init__(self, scenario: str, delay: float):
        super().__init__([], model_name="script/editing-fanout-v1")
        self.delay = delay
        self.calls = Counter()
        self.scenario = scenario

    async def complete(self, messages, **kwargs):
        task = next(m.content for m in messages if m.role == Role.USER and m.content in TASKS)
        index = self.calls[task]
        self.calls[task] += 1
        await asyncio.sleep(self.delay)
        path = "shared.txt" if self.scenario == "overlapping" else f"{task}.txt"
        if index == 0:
            call = ToolCall(f"{task}-write", "write_file", {"path": path, "content": f"{task}\n"})
        elif index == 1:
            call = ToolCall(f"{task}-done", "task_complete", {
                "summary": "Updated the fixture file in the isolated worker.",
                "result": {"edited": True}, "verification_commands": [f"test -s {path}"],
            })
        else:
            raise RuntimeError(f"unexpected extra model call for {task}")
        return ModelResponse(content=None, tool_calls=[call], usage=dict(USAGE))


async def _measure(root: Path, scenario: str, concurrency: int, delay: float) -> dict:
    # Delay imports until the caller has disabled LiteLLM's remote price-map fetch.
    from garuda.core.editing_worker import LocalEditingWorkers
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.tools import tools_for_names
    from garuda.workspace import snapshot_proto as snap
    from garuda.workspace.local import LocalEnvironment

    repo = root / "parent"
    repo.mkdir(parents=True)
    initial = {"shared.txt": "original\n"} if scenario == "overlapping" else {
        f"{task}.txt": "original\n" for task in TASKS
    }
    snap.git(repo, "init", "-q")
    for name, content in initial.items():
        (repo / name).write_text(content)
    snap.git(repo, "add", ".")
    snap.git(repo, "commit", "-qm", "fixture")
    agents = root / "agents"
    agents.mkdir()
    (agents / "result.json").write_text(json.dumps({
        "type": "object", "properties": {"edited": {"type": "boolean"}}, "required": ["edited"],
    }))
    (agents / "editor.yaml").write_text(
        "version: 1\nextends: garuda/build\noutput: {schema: result.json}\n"
        "limits: {max_turns: 3}\n"
        "tools: {preset: none, add: [write_file, task_complete]}\n"
        "completion: {mode: standard, acceptance_contract: false}\n"
    )
    events = EventStore()
    gate = SpendGate(max_tokens=1_000_000)
    model = DelayedTaskScriptModel(scenario, delay)
    admitted, ready, tokens = {}, {}, Counter()
    trace = []

    def observe(event, store):
        payload = event["payload"]
        if event["type"] == "editing_worker":
            candidate = payload["candidate_id"]
            if payload["state"] == "admitted":
                admitted.setdefault(candidate, time.perf_counter())
            if payload["state"] == "ready" and payload.get("child_settled"):
                ready.setdefault(candidate, time.perf_counter())
        elif event["type"] == "model_response":
            tokens[store.session_id] += usage_tokens(payload.get("usage"))

    events.add_observer(observe)
    guard = WorkspaceLeaseGuard(str(repo), events.session_id)
    guard.acquire()
    workers = LocalEditingWorkers(guard, checks=["test -s shared.txt" if scenario == "overlapping"
                                               else "test -s alpha.txt"],
                                  image="scripted-check-dispatch", root=root / "workers",
                                  max_concurrent=concurrency)
    runner = SubagentRunner(
        model=model, env=LocalEnvironment(repo), events=events, workspace_root=str(repo),
        agents_dir=agents, parent_tools=tools_for_names(["write_file", "task_complete"]),
        parent_permissions=PermissionEngine(mode="smart", workspace_root=repo),
        budget=DelegationBudget(max_launches=4), editing_workers=workers,
        editing_max_tokens=1000, spend_gate=gate,
    )
    candidates = []
    # Time the complete dispatch-to-integration workload, excluding fixture setup,
    # parent lease acquisition/release and final temporary-directory cleanup.
    started = time.perf_counter()
    try:
        for offset in range(0, len(TASKS), concurrency):
            tasks = TASKS[offset:offset + concurrency]
            results = await runner.run_editing_workers([("editor", task) for task in tasks])
            for task, result in zip(tasks, results, strict=True):
                receipt = result.metadata.get("editing_worker", {})
                candidate = receipt.get("candidate_id")
                state = receipt.get("state", "refused")
                row = {
                    "task": task, "candidate_id": candidate, "state": state,
                    "outcome": "quarantined" if state == "uncertain" else
                               "ready" if state == "ready" else "refused",
                    "success": result.success, "output": result.output,
                    "refusal": result.metadata.get("refused"),
                    "refusal_detail": result.final_message if not result.success else None,
                    "scripted_tokens": tokens[candidate],
                    "worker_seconds": ready[candidate] - admitted[candidate] if candidate in ready else None,
                    "integration": None, "integration_wait_seconds": None,
                    "integration_seconds": None,
                }
                if state == "ready":
                    attempt = time.perf_counter()
                    row["integration_wait_seconds"] = attempt - ready[candidate]
                    trace.append({"task": task, "phase": "integration_start", "seconds": attempt - started})
                    outcome = await workers.integrate(
                        candidate, receipt["provenance_digest"], env=runner.env,
                        session_id=events.session_id, permissions=runner.parent_permissions,
                    )
                    finished = time.perf_counter()
                    trace.append({"task": task, "phase": "integration_end", "seconds": finished - started})
                    row["integration_seconds"] = finished - attempt
                    row["integration"] = {key: outcome[key] for key in ("state", "code") if key in outcome}
                candidates.append(row)
        wall = time.perf_counter() - started
        final_files = {name: (repo / name).read_text() for name in initial}
    finally:
        await guard.release()
        await runner.env.aclose()

    stale = [row for row in candidates if (row["integration"] or {}).get("code") == "integration.stale_base"]
    integrated = sum((row["integration"] or {}).get("state") == "integrated" for row in candidates)
    expected_files = ({"shared.txt": "delta\n" if concurrency == 1 else "alpha\n"}
                      if scenario == "overlapping" else {
                          f"{task}.txt": f"{task}\n" if concurrency == 1 or task == "alpha" else "original\n"
                          for task in TASKS
                      })
    return {
        "scenario": scenario, "max_concurrent": concurrency, "wall_seconds": wall,
        "launches": runner.budget.launches, "spend_gate": gate.snapshot(),
        "scripted_calls": sum(model.calls.values()), "candidates": candidates,
        "outcomes": dict(Counter(row["outcome"] for row in candidates)),
        "integrated": integrated, "stale_base_refusals": len(stale),
        "integration_wait_seconds": sum(row["integration_wait_seconds"] or 0 for row in candidates),
        "integration_seconds": sum(row["integration_seconds"] or 0 for row in candidates),
        "work_discarded": {
            "candidates": len(stale), "scripted_tokens": sum(row["scripted_tokens"] for row in stale),
            "worker_seconds": sum(row["worker_seconds"] for row in stale),
        },
        "integration_trace": trace, "final_files": final_files,
        "fixture_passed": (all(row["outcome"] == "ready" and row["success"] and row["output"] == {"edited": True}
                               for row in candidates)
                           and integrated == (4 if concurrency == 1 else 1)
                           and len(stale) == (0 if concurrency == 1 else 3)
                           and final_files == expected_files and gate.settled_tokens == 960
                           and gate.reserved_tokens == 0 and runner.budget.launches == 4),
    }


async def run_measurement(out: str | Path, *, delay: float = 0.05) -> dict:
    """Write an aggregate JSON report; never inspect or edit the caller's repo.

    Temporary stores isolate leases, capacity, settings and sessions. Synthetic
    identity covers only this process; this is not OS lease-identity validation.
    """
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("delay must be finite and nonnegative")
    with tempfile.TemporaryDirectory(prefix="garuda-editing-fanout-") as scratch:
        root = Path(scratch).resolve()
        environment = {
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "GARUDA_SESSIONS_DIR": str(root / "sessions"),
            "GARUDA_GLOBAL_SETTINGS": str(root / "settings" / "settings.yaml"),
            "GARUDA_LEASES_DIR": str(root / "leases"),
            "GARUDA_RECOVERY_ROOT": str(root / "recovery"),
        }

        def fixture_check(directory, command, image, timeout):
            # Inspect the actually materialized candidate. Only Docker dispatch
            # is replaced; tree stability checks, permissions and apply are real.
            files = list(Path(directory).glob("*.txt"))
            contents = [file.read_text() for file in files]
            passed = bool(contents) and all(text in {"original\n", *(f"{task}\n" for task in TASKS)}
                                            for text in contents) and any(text != "original\n" for text in contents)
            return {"command": command, "passed": passed, "exit_code": 0 if passed else 1,
                    "simulated_dispatch": True}

        with patch.dict(os.environ, environment), patch(
            "garuda.runtime.recovery._process_identity",
            lambda pid: "fanout-fixture-start" if pid == os.getpid() else None,
        ), patch("garuda.workspace.worktrees._run_check", fixture_check):
            measurements = [await _measure(root / f"{scenario}-{n}", scenario, n, delay)
                            for scenario in ("disjoint", "overlapping") for n in (1, 4)]
    report = {
        "schema_version": 1, "fixture": "editing-fanout-v1", "scripted_delay_seconds": delay,
        "python": platform.python_version(), "platform": platform.platform(),
        "provider_calls": 0, "provider_cost_usd": 0,
        "caveat": "Deterministic fixture with scripted delays; not a latency or quality claim. "
                  "Docker dispatch and current-process start identity are simulated.",
        "measurements": measurements,
    }
    output = Path(out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="editing-fanout.json")
    parser.add_argument("--delay", type=float, default=0.05, help="Scripted sleep per model call, in seconds")
    args = parser.parse_args(argv)
    report = asyncio.run(run_measurement(args.out, delay=args.delay))
    print(report["caveat"])
    print(f"Report: {args.out}")
    return int(not all(row["fixture_passed"] for row in report["measurements"]))


if __name__ == "__main__":
    raise SystemExit(main())
