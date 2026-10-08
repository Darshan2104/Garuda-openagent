"""Shared flow orchestration for the CLI and other in-process callers.

The engine owns admission, workspace leases, step receipts and recovery.
This facade resolves the effective flow and refuses missing roles before
passing execution to that owner. It does not run acceptance checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from garuda.config.garuda_yaml import load_effective
from garuda.core.sessions import SessionStore
from garuda.flows import engine, packaged


@dataclass(frozen=True)
class FlowExecutionResult:
    """Engine outcome and recorded review/state evidence; no inferred verification."""

    flow: engine.FlowResult
    review: dict | None
    state: dict | None


class FlowExecutionService:
    def __init__(self, store: SessionStore | None = None):
        self.store = store if store is not None else SessionStore()

    async def run(self, name: str, task: str, workspace: str) -> FlowExecutionResult:
        return await self._execute(name, task, workspace)

    async def resume(self, flow_session: str) -> FlowExecutionResult:
        meta = self.store.load_meta(flow_session)
        return await self._execute(meta["flow"]["name"], meta["task"], meta["workspace"],
                                   flow_session=flow_session)

    async def _execute(self, name: str, task: str, workspace: str, *,
                       flow_session: str | None = None) -> FlowExecutionResult:
        from garuda.flows.launch import launch_step

        resolved = load_effective(workspace)
        flows = packaged.available(resolved)
        if name not in flows:
            raise engine.FlowStopped("flow.unknown", f"no flow named {name!r} "
                                     f"(have: {', '.join(sorted(flows))})")
        flow, _source = flows[name]
        missing = packaged.missing_roles(flow, resolved)
        if missing:
            raise engine.FlowStopped(
                "flow.missing_roles", f"flow {name} needs the roles "
                f"{', '.join(packaged.required_roles(flow))}; define "
                f"{', '.join(missing)} in garuda.yaml (`garuda init` proposes them)")
        runner = engine.FlowRunner(self.store, workspace, name, flow, resolved, task=task,
                                   launcher=launch_step, flow_session=flow_session)
        result = await runner.run(resume=flow_session is not None)
        meta = self.store.load_meta(result.flow_session)
        return FlowExecutionResult(result, meta.get("review"), meta.get("state"))
