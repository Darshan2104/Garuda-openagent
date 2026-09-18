"""Runtime and handoff contract matrix (P1.10, issue #42).

Every adapter runs the common scenarios its declared capabilities allow —
lifecycle, cancellation, permission, diff, resume, handoff, recovery — and
each run publishes a CI-readable JSON report. An adapter with any failure is
not supported; skips name the missing capability so coverage gaps stay
visible instead of silently passing.

Run: `python -m garuda.eval.contract_matrix --out contract-reports`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCENARIOS = (
    "lifecycle",
    "cancellation",
    "permission",
    "diff",
    "resume",
    "handoff",
    "recovery",
)

PASS, FAIL, SKIP = "pass", "fail", "skip"


@dataclass
class CheckResult:
    scenario: str
    status: str
    detail: str = ""


@dataclass
class AdapterEntry:
    id: str
    kind: str
    capabilities: frozenset[str] = frozenset()
    make: Callable[[Path], Any] | None = None
    manifest: Any = None


@dataclass
class AdapterReport:
    adapter_id: str
    kind: str
    capabilities: tuple[str, ...]
    results: list[CheckResult] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)

    @property
    def supported(self) -> bool:
        return bool(self.results) and all(r.status != FAIL for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "generated_at": self.generated_at,
            "supported": self.supported,
            "results": [
                {"scenario": r.scenario, "status": r.status, "detail": r.detail}
                for r in self.results
            ],
        }


def _fake_entries() -> list[AdapterEntry]:
    from garuda.runtime.fake import FakeRuntime, FakeScenario

    def make(scenario: FakeScenario):
        def _make(scratch: Path, _s=scenario) -> FakeRuntime:
            return FakeRuntime(_s, runtime_id=f"fake-{_s.value}")

        return _make

    caps = {
        FakeScenario.SUCCESS: frozenset({"lifecycle", "cancellation", "handoff", "recovery"}),
        FakeScenario.STREAMING: frozenset({"lifecycle", "cancellation", "recovery"}),
        FakeScenario.APPROVAL: frozenset({"lifecycle", "cancellation", "permission", "recovery"}),
        FakeScenario.CANCELLATION: frozenset({"lifecycle", "cancellation", "recovery"}),
    }
    return [
        AdapterEntry(id=f"fake-{s.value}", kind="fake", capabilities=c, make=make(s))
        for s, c in caps.items()
    ]


def _acp_entries() -> list[AdapterEntry]:
    from garuda.acp.adapter import AcpRuntime

    profiles = {
        "success": frozenset({"lifecycle", "cancellation", "resume", "handoff", "recovery"}),
        "streaming": frozenset({"lifecycle", "cancellation", "recovery"}),
        # approval and slow need a driver (approve) or never answer: their
        # lifecycle is proven under permission/timeout checks, not unattended.
        "approval": frozenset({"cancellation", "permission", "recovery"}),
        "slow": frozenset({"cancellation", "recovery"}),
        "diff": frozenset({"lifecycle", "cancellation", "diff", "recovery"}),
    }

    def make(profile: str):
        def _make(scratch: Path, _p=profile) -> AcpRuntime:
            return AcpRuntime(
                [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", _p],
                runtime_id=f"acp-{_p}",
            )

        return _make

    return [
        AdapterEntry(id=f"acp-{p}", kind="acp", capabilities=c, make=make(p))
        for p, c in profiles.items()
    ]


def _vendor_entries() -> list[AdapterEntry]:
    from garuda.acp.catalog import adapter_for_manifest, builtin_manifest_dicts
    from garuda.runtime.registry import parse_global_manifests

    manifests = {
        m.runtime_id: m
        for m in parse_global_manifests(
            builtin_manifest_dicts(), source="contract matrix"
        )
    }
    entries = []
    for runtime_id, manifest in sorted(manifests.items()):
        def _make(scratch: Path, _m=manifest) -> Any:
            return adapter_for_manifest(
                _m,
                argv_override=[
                    sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success",
                ],
            )

        entries.append(
            AdapterEntry(
                id=runtime_id,
                kind="vendor",
                capabilities=frozenset({"lifecycle", "cancellation", "handoff", "recovery"}),
                make=_make,
                manifest=manifest,
            )
        )
    return entries


def _native_entry() -> AdapterEntry:
    def _make(scratch: Path):
        from garuda.core.loop import DefaultAgent
        from garuda.core.permissions import PermissionEngine
        from garuda.core.sessions import SessionStore
        from garuda.model.protocol import ModelResponse
        from garuda.model.script_model import ScriptModel
        from garuda.runtime.native import NativeGarudaRuntime
        from garuda.tools import tools_for_names
        from garuda.types import AgentConfig, ToolCall
        from garuda.workspace.local import LocalEnvironment

        model = ScriptModel(
            responses=[
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="1",
                            name="task_complete",
                            arguments={"summary": "Matrix run."},
                        )
                    ],
                )
            ]
        )
        agent = DefaultAgent()
        tools = tools_for_names(["task_complete", "write_file"])
        config = AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo")
        permissions = PermissionEngine(mode="yolo")
        store = SessionStore(scratch / "sessions")
        workspace = scratch / "ws"
        workspace.mkdir(parents=True, exist_ok=True)

        async def _driver(*, task: str, turn: int, trail) -> Any:
            env = LocalEnvironment(workspace_root=workspace)
            return await agent.run(
                task=task, model=model, env=env, tools=tools,
                config=config, events=trail, permissions=permissions,
            )

        return NativeGarudaRuntime(
            agent=agent, model=model, tools=tools, config=config,
            permissions=permissions, store=store, run=_driver,
        )

    return AdapterEntry(
        id="native",
        kind="native",
        capabilities=frozenset(
            {"lifecycle", "cancellation", "permission", "diff", "resume", "handoff", "recovery"}
        ),
        make=_make,
    )


def build_matrix() -> list[AdapterEntry]:
    """All adapters under contract, in deterministic order."""
    return sorted(
        [*_fake_entries(), *_acp_entries(), *_vendor_entries(), _native_entry()],
        key=lambda e: e.id,
    )


async def _check(entry: AdapterEntry, scenario: str, scratch: Path) -> CheckResult:
    from garuda.runtime.conformance import run_conformance_suite

    assert entry.make is not None
    try:
        if scenario == "lifecycle":
            await run_conformance_suite(lambda: entry.make(scratch))
        elif scenario == "cancellation":
            await _check_cancellation(entry, scratch)
        elif scenario == "permission":
            await _check_permission(entry, scratch)
        elif scenario == "diff":
            await _check_diff(entry, scratch)
        elif scenario == "resume":
            await _check_resume(entry, scratch)
        elif scenario == "handoff":
            await _check_handoff(entry, scratch)
        elif scenario == "recovery":
            await _check_recovery(entry, scratch)
        else:
            return CheckResult(scenario, SKIP, "unknown scenario")
    except AssertionError as exc:
        return CheckResult(scenario, FAIL, f"assertion: {exc}")
    except Exception as exc:
        return CheckResult(scenario, FAIL, f"{type(exc).__name__}: {exc}")
    return CheckResult(scenario, PASS, "")


async def _check_cancellation(entry: AdapterEntry, scratch: Path) -> None:
    from garuda.runtime.protocol import LifecycleState, RuntimeClosedError

    if entry.kind == "native":
        runtime = entry.make(scratch)
        await runtime.start(task="t", session_id="cancel-1")
        await runtime.cancel(reason="matrix")
        # Idle cancel lands terminal; a later prompt is rejected, not run.
        try:
            await runtime.prompt("too late")
        except RuntimeClosedError:
            return
        raise AssertionError("cancelled native runtime accepted a prompt")
    runtime = entry.make(scratch)
    await runtime.start(task="t", session_id=f"cancel-{entry.id}")
    await runtime.cancel(reason="matrix")
    assert runtime.state is LifecycleState.CLOSED, runtime.state
    await runtime.close()


async def _check_permission(entry: AdapterEntry, scratch: Path) -> None:
    from garuda.runtime.protocol import LifecycleState

    if entry.kind == "native":
        runtime = entry.make(scratch)
        await runtime.start(task="t", session_id="perm-1")
        answered: list[bool] = []
        runtime.park_approval("apr-1", answered.append)
        await runtime.permission_response(approval_id="apr-1", allow=True)
        assert answered == [True]
        await runtime.close()
        return
    if entry.kind == "fake":

        assert entry.id == "fake-approval", entry.id
        runtime = entry.make(scratch)
        await runtime.start(task="t", session_id="perm-1")
        await runtime.prompt("needs approval")
        await runtime.permission_response(approval_id="apr-1", allow=True)
        assert runtime.state is LifecycleState.IDLE
        await runtime.close()
        return
    runtime = entry.make(scratch)
    await runtime.start(task="t", session_id="perm-1")
    prompting = asyncio.ensure_future(runtime.prompt("needs approval"))
    approval_id: str | None = None
    for _ in range(100):
        await asyncio.sleep(0.05)
        events, _ = await runtime.poll_events(0)
        requests = [e for e in events if e.kind.value == "approval_request"]
        if requests:
            approval_id = requests[0].payload["approval_id"]
            break
    assert approval_id, "no approval request observed"
    await runtime.permission_response(approval_id=approval_id, allow=True)
    await asyncio.wait_for(prompting, 15)
    assert runtime.state is LifecycleState.IDLE
    await runtime.close()


async def _check_diff(entry: AdapterEntry, scratch: Path) -> None:
    if entry.kind == "native":
        import subprocess

        repo = scratch / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@t.t"],
            check=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "t"], check=True, timeout=30,
        )
        (repo / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", str(repo), "add", "."],
                       check=True, timeout=30)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"],
                       check=True, timeout=30)
        from garuda.workspace.diff import capture_baseline, session_delta

        baseline = capture_baseline(repo)
        (repo / "made.txt").write_text("made by the run\n")
        delta = session_delta(baseline, repo)
        assert "made.txt" in {f.path for f in delta.files}, delta.files
        return
    runtime = entry.make(scratch)
    await runtime.start(task="t", session_id=f"diff-{entry.id}")
    try:
        await runtime.prompt("change a.py")
        events, _ = await runtime.poll_events(0)
        assert any(e.payload.get("path") == "a.py" for e in events), "no diff event"
    finally:
        await runtime.close()


async def _check_resume(entry: AdapterEntry, scratch: Path) -> None:
    from garuda.runtime.protocol import RuntimeStartError

    if entry.kind == "fake":
        runtime = entry.make(scratch)
        info = await runtime.resume(native_session_id="fake-native-s1")
        assert info.native_session_id == "fake-native-s1"
        await runtime.close()
        return
    if entry.kind == "native":
        runtime = entry.make(scratch)
        await runtime.start(task="t", session_id="resume-1")
        native_id = runtime.native_session_id
        await runtime.prompt("first")
        await runtime.close()
        second = entry.make(scratch)
        info = await second.resume(native_session_id=native_id or "")
        assert info.native_session_id == native_id
        events, _ = await second.poll_events(0)
        assert events, "resumed runtime replays history"
        await second.close()
        return
    runtime = entry.make(scratch)
    await runtime.start(task="t", session_id=f"resume-{entry.id}")
    native_id = runtime.native_session_id or ""
    await runtime.close()
    second = entry.make(scratch)
    try:
        await second.resume(native_session_id=native_id)
    except RuntimeStartError as exc:
        assert "cross-process" in str(exc), exc
        return
    raise AssertionError("cross-process resume unexpectedly succeeded")


async def _check_handoff(entry: AdapterEntry, scratch: Path) -> None:
    from garuda.runtime.fake import FakeRuntime, FakeScenario
    from garuda.runtime.handoff import HandoffTransaction
    from garuda.runtime.protocol import LifecycleState

    source = FakeRuntime(FakeScenario.SUCCESS, runtime_id="matrix-source")
    await source.start(task="work", session_id="matrix-s")
    target = entry.make(scratch)
    tx = HandoffTransaction(session_id="matrix-s")
    await tx.begin(source, checkpoint=lambda: None)
    await tx.start_target(source, target)
    await tx.acknowledge(source, target)
    assert source.state is LifecycleState.CLOSED
    assert target.state is LifecycleState.IDLE
    await target.close()


async def _check_recovery(entry: AdapterEntry, scratch: Path) -> None:
    from garuda.core.sessions import SessionStore
    from garuda.runtime.recovery import RestartState, recover
    from garuda.runtime.session import RuntimeSegment

    store = SessionStore(scratch / "sessions")
    store.begin("rec-1", task="t", model="m", agent="a", workspace="w")
    store.ensure_unified("rec-1")
    store.attach_runtime_segment(
        "rec-1",
        RuntimeSegment(runtime_id=entry.id, kind=entry.kind, native_session_id="n1"),
    )
    store.record_handoff("rec-1", state="failed", attempts=1)
    report = recover(store, "rec-1")
    assert report.state is RestartState.RESUMABLE, report
    assert report.resume_session_id == "rec-1"


async def run_entry(entry: AdapterEntry, work_root: Path) -> AdapterReport:
    scratch = work_root / entry.id.replace("/", "_")
    scratch.mkdir(parents=True, exist_ok=True)
    report = AdapterReport(
        adapter_id=entry.id, kind=entry.kind, capabilities=tuple(sorted(entry.capabilities))
    )
    for scenario in SCENARIOS:
        if scenario not in entry.capabilities:
            report.results.append(CheckResult(scenario, SKIP, "capability not declared"))
            continue
        report.results.append(await _check(entry, scenario, scratch))
    return report


async def run_matrix(out_dir: str | Path, *, work_root: str | Path | None = None) -> list[AdapterReport]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if work_root is None:
        tmp = tempfile.TemporaryDirectory(prefix="contract-matrix-")
        work = Path(tmp.name)
    else:
        tmp = None
        work = Path(work_root)
        work.mkdir(parents=True, exist_ok=True)
    try:
        reports = []
        for entry in build_matrix():
            report = await run_entry(entry, work)
            reports.append(report)
            (out / f"{entry.id}.json").write_text(
                json.dumps(report.to_dict(), indent=2), encoding="utf-8"
            )
        summary = {
            "generated_at": time.time(),
            "adapters": len(reports),
            "supported": sorted(r.adapter_id for r in reports if r.supported),
            "unsupported": sorted(r.adapter_id for r in reports if not r.supported),
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return reports
    finally:
        if tmp is not None:
            tmp.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the runtime contract matrix.")
    parser.add_argument("--out", default="contract-reports")
    parser.add_argument("--work-root", default=None)
    args = parser.parse_args(argv)
    reports = asyncio.run(run_matrix(args.out, work_root=args.work_root))
    failed = [(r.adapter_id, c.scenario) for r in reports for c in r.results if c.status == FAIL]
    for adapter_id, scenario in failed:
        print(f"FAIL {adapter_id} :: {scenario}")
    supported = sum(1 for r in reports if r.supported)
    print(f"{supported}/{len(reports)} adapters supported; reports in {args.out}/")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
