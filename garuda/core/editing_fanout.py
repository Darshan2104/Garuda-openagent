"""Admission and ordered receipts for model-driven editing-worker batches."""

from __future__ import annotations

from garuda.core.child_result import build_child_result_envelope, canonical_json
from garuda.core.permissions import PermissionDecision
from garuda.core.subagent import DelegationBudget
from garuda.workspace.health import HealthMonitoredEnvironment
from garuda.workspace.local import LocalEnvironment

TOOL_NAME = "fan_out_editing_workers"
RESULT_VERSION = 1
MAX_TASK_CHARS = 20_000
_UNAVAILABLE = {"delivery": {"state": "unavailable", "reason": "delivery_budget_too_small"}}


def fanout_exposure_refusal(config, *, permissions, tool_names, allowed_tool_effects) -> str | None:
    """Return the first wiring-time refusal, without changing run state."""
    if config.editing_worker_fanout is not True:
        return "fanout.not_enabled"
    if config.editing_workers is None:
        return "worker.disabled"
    if "invoke_subagent" not in tool_names:
        return "fanout.delegation_not_admitted"
    if TOOL_NAME in (config.removed_tools or ()):
        return "fanout.removed"
    if allowed_tool_effects is not None:
        return "fanout.restricted_effects"
    if config.workspace_kind != "local":
        return "worker.unsupported_environment"
    if permissions.mode == "readonly":
        return "fanout.readonly"
    if permissions.check_tool(TOOL_NAME) == PermissionDecision.DENY:
        return "fanout.denied"
    if config.subagents == []:
        return "agent.subagent_not_allowed"
    budget = config.delegation_budget or DelegationBudget()
    if config.delegation_depth + 1 > budget.max_depth:
        return "agent.delegation_too_deep"
    return None


def _valid_arguments(arguments) -> bool:
    if not isinstance(arguments, dict) or set(arguments) - {"tasks", "integrate"}:
        return False
    tasks = arguments.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False
    if arguments.get("integrate", "none") not in ("none", "in_order"):
        return False
    return all(
        isinstance(item, dict) and set(item) == {"profile", "task"}
        and isinstance(item["profile"], str) and bool(item["profile"].strip())
        and isinstance(item["task"], str) and bool(item["task"].strip())
        and len(item["task"]) <= MAX_TASK_CHARS
        for item in tasks
    )


def _size(payload) -> int:
    return len(canonical_json(payload).encode("utf-8"))


def _refused(code, budget, **extra):
    payload = {"version": RESULT_VERSION, "state": "refused", "code": code, **extra}
    if _size(payload) > budget:
        payload = {"version": RESULT_VERSION, "state": "refused", "code": code}
    # No work is admitted when even a refusal cannot be delivered. The tool
    # adapter handles the degenerate zero/one-byte budget without clipping JSON.
    return (payload if _size(payload) <= budget else {}), True


def _local(env) -> bool:
    backend = env.inner if type(env) is HealthMonitoredEnvironment else env
    return type(backend) is LocalEnvironment


def _compact(entries):
    for entry in entries:
        entry["child_result"] = _UNAVAILABLE
        for key in ("summary", "summary_truncated", "workspace", "patch", "base_commit", "state"):
            entry.pop(key, None)
        if "integration" in entry:
            entry["integration"].pop("message", None)


async def run_fanout(runner, arguments, *, env, session_id, permissions, output_budget) -> tuple[dict, bool]:
    """Validate before admission; settle children before serialized integration."""
    if not _valid_arguments(arguments):
        return _refused("fanout.invalid_arguments", output_budget)
    if runner is None:
        return _refused("fanout.no_runner", output_budget)
    if runner.editing_workers is None:
        return _refused("worker.disabled", output_budget)
    if permissions is None:
        return _refused("fanout.denied", output_budget)
    if permissions.mode == "readonly":
        return _refused("fanout.readonly", output_budget)
    if permissions.check_tool(TOOL_NAME) == PermissionDecision.DENY:
        return _refused("fanout.denied", output_budget)
    if runner.workspace_kind not in (None, "local") or not _local(env) or not _local(runner.env):
        return _refused("worker.unsupported_environment", output_budget)
    gate = runner.spend_gate
    if gate is not None and gate.limited and gate.would_refuse(0) is not None:
        return _refused("spend.exhausted", output_budget)
    tasks = arguments["tasks"]
    integrate = arguments.get("integrate", "none")
    # Reserve enough space for identities, outcomes and compact typed delivery
    # before launching. Paths and summaries alone can exceed the live budget.
    if output_budget < 256 + sum(512 + len(canonical_json(t["profile"]).encode("utf-8")) for t in tasks):
        return _refused("fanout.output_budget_too_small", output_budget)
    try:
        results = await runner.run_editing_workers([(t["profile"], t["task"]) for t in tasks])
    except ExceptionGroup as group:
        return _refused(
            "worker.batch_failed", output_budget,
            message="A worker raised; live siblings were quarantined. Inspect with "
                    "`garuda sessions show SESSION --json`.",
            errors=[type(e).__name__ for e in group.exceptions][:8],
        )
    codes = {r.metadata.get("refused") for r in results}
    if len(codes) == 1 and None not in codes and all("editing_worker" not in r.metadata for r in results):
        return _refused(next(iter(codes)), output_budget)

    entries = []
    per_worker_budget = max(0, (output_budget - 1024) // len(tasks))
    for index, (task, result) in enumerate(zip(tasks, results, strict=True)):
        receipt = result.metadata.get("editing_worker") or {}
        state = receipt.get("state")
        if not receipt or state == "refused":
            outcome = "refused"
        elif state == "uncertain":
            outcome = "quarantined"
        elif state == "ready" and result.success:
            outcome = "ready"
        else:
            outcome = "failed"
        summary_bytes = result.final_message.encode("utf-8")
        entry = {
            "index": index, "profile": task["profile"], "outcome": outcome,
            "code": result.metadata.get("refused") or receipt.get("admission_refused"),
            **{key: receipt[key] for key in (
                "candidate_id", "provenance_digest", "base_commit", "patch", "workspace", "state",
            ) if key in receipt},
            "child_result": build_child_result_envelope(
                result, task["profile"], storage=runner.parent_buffer,
                parent_tools=runner.parent_tools, output_budget=per_worker_budget,
                workspace=result.metadata.get("workspace_evidence"),
            ),
            "summary": summary_bytes[:512].decode("utf-8", errors="ignore"),
            "summary_truncated": len(summary_bytes) > 512,
        }
        entries.append(entry)

    if integrate == "in_order":
        halted = False
        for entry in entries:
            if entry["outcome"] == "quarantined":
                halted = True
            if entry["outcome"] != "ready":
                continue
            if halted:
                entry["integration"] = {"state": "skipped", "code": "fanout.halted_after_uncertain"}
                continue
            outcome = await runner.editing_workers.integrate(
                entry["candidate_id"], entry["provenance_digest"],
                env=env, session_id=session_id, permissions=permissions,
            )
            entry["integration"] = {k: outcome[k] for k in ("state", "code", "message") if k in outcome}
            entry["code"] = outcome.get("code")
            if outcome["state"] == "integrated":
                entry["outcome"] = "integrated"
            elif outcome["state"] == "uncertain":
                entry["outcome"] = "quarantined"
                halted = True
            elif outcome.get("code") == "integration.stale_base":
                entry["outcome"] = "stale_base"
            else:
                entry["outcome"] = "integration_refused"

    payload = {"version": RESULT_VERSION, "state": "completed", "integrate": integrate, "workers": entries}
    if _size(payload) > output_budget:
        _compact(entries)
    if _size(payload) > output_budget:
        # A custom trusted service may return identities larger than the local
        # service's fixed-size receipts. Do not publish clipped success claims.
        return {
            "version": RESULT_VERSION, "state": "delivery_failed",
            "code": "fanout.receipt_budget_exceeded",
        }, True
    return payload, not any(e["outcome"] in {"ready", "integrated"} for e in entries)
