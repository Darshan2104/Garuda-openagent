"""Runtime and handoff contract matrix (P1.10, issue #42).

Scenarios run from declared capabilities, not hand tables: each scenario
names the runtime capabilities it needs (`SCENARIO_CAPS`) and any harness
behavior it needs (`SCENARIO_BEHAVIORS`, e.g. an approval flow or diff
events). An adapter runs a scenario only when its real declarations cover
both; anything else records SKIP naming the gap. Capability sources are
real: vendor rows use their manifest's declared capabilities, fake rows use
`FakeRuntime`'s declared `{"prompt", "cancel"}`, ACP profile rows use the
pinned `fake_agent` profiles, and native uses the loop's `{"prompt",
"cancel"}`.

Vendor rows run stand-in fakes, so they are labeled `simulated`: they prove
the harness-agnostic wiring (lifecycle, cancellation, handoff, recovery),
never vendor support. An adapter is `supported` only with zero
failures AND at least one PASS — all-SKIP never passes.

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

#: Runtime capabilities each scenario needs. Wiring scenarios (resume,
#: handoff, recovery) need none beyond a startable adapter; capability
#: scenarios derive from the adapter's real declarations.
SCENARIO_CAPS: dict[str, frozenset[str]] = {
    "lifecycle": frozenset({"prompt"}),
    "cancellation": frozenset({"cancel"}),
    "permission": frozenset({"prompt"}),
    "diff": frozenset(),
    "resume": frozenset(),
    "handoff": frozenset(),
    "recovery": frozenset(),
}

#: Harness behaviors each scenario needs. Keys are fake_agent profile
#: behaviors (pinned in `garuda.acp.fake_agent.PROFILES`); the native loop
#: provides them structurally.
SCENARIO_BEHAVIORS: dict[str, frozenset[str]] = {
    # lifecycle needs an unattended answer: approval blocks for a driver and
    # slow never answers, so neither runs it.
    "lifecycle": frozenset({"responds"}),
    "cancellation": frozenset(),
    "permission": frozenset({"approval"}),
    "diff": frozenset({"diff-event"}),
    # ACP does not support cross-process resume yet; a refusal is not a
    # passing contract result. Only adapters that explicitly declare resume
    # behavior enter this row.
    "resume": frozenset({"resumable"}),
    # Handoff must consume a delivered package, so a target must be able to
    # answer a prompt rather than merely start and stop.
    "handoff": frozenset({"responds"}),
    # Recovery must be grounded in an adapter turn, not only a synthetic
    # session record. Adapters that cannot answer a turn are skipped here.
    "recovery": frozenset({"responds"}),
}

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
    behaviors: frozenset[str] = frozenset()
    simulated: bool = False
    make: Callable[[Path], Any] | None = None
    manifest: Any = None

    def scenarios(self) -> list[str]:
        """Scenarios this adapter's real declarations cover, in matrix order."""
        return [
            scenario
            for scenario in SCENARIOS
            if SCENARIO_CAPS[scenario] <= self.capabilities
            and SCENARIO_BEHAVIORS[scenario] <= self.behaviors
        ]


@dataclass
class AdapterReport:
    adapter_id: str
    kind: str
    capabilities: tuple[str, ...]
    results: list[CheckResult] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)
    simulated: bool = False

    @property
    def supported(self) -> bool:
        return (
            bool(self.results)
            and all(r.status != FAIL for r in self.results)
            and any(r.status == PASS for r in self.results)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "generated_at": self.generated_at,
            "supported": self.supported,
            "simulated": self.simulated,
            "results": [
                {"scenario": r.scenario, "status": r.status, "detail": r.detail}
                for r in self.results
            ],
        }


#: `FakeRuntime`'s declared capabilities — the real source for fake rows.
FAKE_RUNTIME_CAPS = frozenset({"prompt", "cancel"})

#: Behaviors each fake scenario affords. Keyed by scenario name; the matrix
#: refuses unknown names so a renamed scenario cannot silently change coverage.
FAKE_BEHAVIORS: dict[str, frozenset[str]] = {
    "success": frozenset({"responds", "resumable"}),
    "streaming": frozenset({"responds", "resumable"}),
    # Approval intentionally does not claim an unattended response: its
    # prompt remains blocked until the permission contract answers it.
    "approval": frozenset({"approval", "resumable"}),
    "cancellation": frozenset({"responds", "resumable"}),
}

#: Behaviors each fake_agent profile affords. Subset of the pinned
#: `garuda.acp.fake_agent.PROFILES`; negative-path fixtures (malformed,
#: slow, exit-early, resume, version-mismatch, capabilities-*) are
#: intentionally absent — they prove failure modes elsewhere, not contract
#: behaviors here.
PROFILE_BEHAVIORS: dict[str, frozenset[str]] = {
    "success": frozenset({"responds"}),
    "streaming": frozenset({"responds"}),
    "approval": frozenset({"approval"}),
    "slow": frozenset(),
    "diff": frozenset({"responds", "diff-event"}),
}


def _fake_entries() -> list[AdapterEntry]:
    from garuda.runtime.fake import FakeRuntime, FakeScenario

    wanted = {
        FakeScenario.SUCCESS: "success",
        FakeScenario.STREAMING: "streaming",
        FakeScenario.APPROVAL: "approval",
        FakeScenario.CANCELLATION: "cancellation",
    }

    def make(scenario: FakeScenario):
        def _make(scratch: Path, _s=scenario) -> FakeRuntime:
            return FakeRuntime(_s, runtime_id=f"fake-{_s.value}")

        return _make

    return [
        AdapterEntry(
            id=f"fake-{s.value}",
            kind="fake",
            capabilities=FAKE_RUNTIME_CAPS,
            behaviors=FAKE_BEHAVIORS[name],
            make=make(s),
        )
        for s, name in wanted.items()
    ]


def _acp_entries() -> list[AdapterEntry]:
    from garuda.acp.adapter import AcpRuntime
    from garuda.acp.fake_agent import PROFILES as PINNED_PROFILES

    unknown = set(PROFILE_BEHAVIORS) - {p.split("capabilities-")[-1] if p.startswith("capabilities-") else p for p in PINNED_PROFILES}
    if unknown:
        raise ValueError(f"contract profiles not in fake_agent.PROFILES: {sorted(unknown)}")

    def make(profile: str):
        def _make(scratch: Path, _p=profile) -> AcpRuntime:
            return AcpRuntime(
                [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", _p],
                runtime_id=f"acp-{_p}",
            )

        return _make

    return [
        AdapterEntry(
            id=f"acp-{profile}",
            kind="acp",
            capabilities=FAKE_RUNTIME_CAPS,
            behaviors=behaviors,
            make=make(profile),
        )
        for profile, behaviors in PROFILE_BEHAVIORS.items()
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
                # The manifest's own declared capabilities — never a hand table.
                # The stand-in answers prompts unattended, hence "responds".
                capabilities=frozenset(manifest.capabilities.names),
                behaviors=frozenset({"responds"}),
                simulated=True,
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
        # The loop's real capabilities; approval, diff, and unattended
        # answers hold structurally (parked approvals, git diffs, real model).
        capabilities=frozenset({"prompt", "cancel"}),
        behaviors=frozenset({"responds", "approval", "diff-event", "resumable"}),
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
    package = "handoff package for matrix-s"
    await tx.begin(source, checkpoint=lambda: None)
    await tx.start_target(source, target)
    # A newly-started target is not enough: deliver the package as a real
    # target turn and require observable progress before ownership is
    # acknowledged.  Different adapters expose different event payloads
    # (messages, diffs, or tool calls), so the turn boundary—not a particular
    # response shape—is the portable consumption proof.
    await target.prompt(package)
    target_events, _ = await target.poll_events(0)
    assert target_events, f"{entry.id} emitted no events while consuming handoff"
    assert target.state is LifecycleState.IDLE, target.state
    await tx.acknowledge(source, target)
    assert source.state is LifecycleState.CLOSED
    assert target.state is LifecycleState.IDLE
    await target.close()


async def _check_recovery(entry: AdapterEntry, scratch: Path) -> None:
    from garuda.core.sessions import SessionStore
    from garuda.runtime.recovery import RestartState, recover
    from garuda.runtime.session import RuntimeSegment

    # The adapter itself starts and stops here, using the same persisted session
    # identity that recovery later audits rather than a separate fixture id.
    store = SessionStore(scratch / "sessions")
    session_id = f"rec-{entry.id}"
    store.begin(session_id, task="t", model="m", agent="a", workspace=str(scratch))
    store.checkpoint_messages(session_id, [])
    store.ensure_unified(session_id)
    runtime = entry.make(scratch)
    await runtime.start(task="t", session_id=session_id)
    await runtime.prompt("recovery evidence")
    events, event_cursor = await runtime.poll_events(0)
    assert events, f"{entry.id} produced no evidence before recovery"
    store.checkpoint_state(
        session_id,
        {
            "runtime_id": runtime.runtime_id,
            "native_session_id": runtime.native_session_id,
            "event_cursor": event_cursor,
            "event_kinds": [event.kind.value for event in events],
        },
    )
    native_id = runtime.native_session_id or "n1"
    if entry.kind != "native":
        authority = getattr(runtime, "authority", None)
        capabilities = (
            frozenset(set(authority.owners) | set(authority.to_snapshot()))
            if authority is not None
            else frozenset()
        )
        store.attach_runtime_segment(
            session_id,
            RuntimeSegment(
                runtime_id=runtime.runtime_id,
                kind=entry.kind,
                native_session_id=native_id,
                capabilities=capabilities,
                event_cursor=event_cursor,
            ),
        )
    await runtime.close()
    store.record_handoff(session_id, state="failed", attempts=1)
    report = recover(store, session_id)
    assert report.state is RestartState.RESUMABLE, report
    assert report.resume_session_id == session_id


def _skip_reason(entry: AdapterEntry, scenario: str) -> str:
    missing_caps = sorted(SCENARIO_CAPS[scenario] - entry.capabilities)
    if missing_caps:
        return f"capability not declared: {', '.join(missing_caps)}"
    missing = sorted(SCENARIO_BEHAVIORS[scenario] - entry.behaviors)
    return f"behavior not afforded: {', '.join(missing)}"


async def run_entry(entry: AdapterEntry, work_root: Path) -> AdapterReport:
    scratch = work_root / entry.id.replace("/", "_")
    scratch.mkdir(parents=True, exist_ok=True)
    report = AdapterReport(
        adapter_id=entry.id, kind=entry.kind, capabilities=tuple(sorted(entry.capabilities)),
        simulated=entry.simulated,
    )
    wanted = set(entry.scenarios())
    for scenario in SCENARIOS:
        if scenario not in wanted:
            report.results.append(CheckResult(scenario, SKIP, _skip_reason(entry, scenario)))
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
            "supported": sorted(
                r.adapter_id for r in reports if r.supported and not r.simulated
            ),
            "simulated_supported": sorted(
                r.adapter_id for r in reports if r.supported and r.simulated
            ),
            "unsupported": sorted(r.adapter_id for r in reports if not r.supported),
            "simulated": sorted(r.adapter_id for r in reports if r.simulated),
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
    unsupported = [r.adapter_id for r in reports if not r.supported]
    for adapter_id, scenario in failed:
        print(f"FAIL {adapter_id} :: {scenario}")
    supported = sum(1 for r in reports if r.supported)
    print(f"{supported}/{len(reports)} adapters supported; reports in {args.out}/")
    return 1 if failed or unsupported else 0


if __name__ == "__main__":
    raise SystemExit(main())
