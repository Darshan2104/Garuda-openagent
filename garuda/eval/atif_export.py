"""Convert Garuda EventStore records to ATIF (Agent Trajectory Interchange Format)."""

import json
import uuid
from pathlib import Path
from typing import Any


def events_to_atif(
    events: list[dict[str, Any]],
    session_id: str,
    *,
    agent_name: str = "garuda",
    agent_version: str = "0.5.0",
    model_name: str | None = None,
    instruction: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """Build an ATIF-v1.7 trajectory dict from Garuda event records.

    Args:
        events: Event dicts from ``EventStore.get_all()``.
        session_id: Session identifier for the trajectory.
        agent_name: Agent name for the ATIF agent block.
        agent_version: Agent version string.
        model_name: Optional model name used during the run.
        instruction: Task instruction; inferred from events when omitted.
        prompt_tokens: Optional aggregate prompt token count.
        completion_tokens: Optional aggregate completion token count.
        cost_usd: Optional aggregate cost in USD.

    Returns:
        A dict compatible with Harbor's ATIF ``Trajectory`` schema.
    """
    task = instruction
    success: bool | None = None
    turns: int | None = None

    for event in events:
        payload = event.get("payload", {})
        if event.get("type") == "session_start" and not task:
            task = payload.get("task")
        if event.get("type") == "session_end":
            success = payload.get("success")
            turns = payload.get("turns")

    steps: list[dict[str, Any]] = []
    step_id = 1

    if task:
        steps.append(_user_step(step_id, task, _event_timestamp(events[0]) if events else None))
        step_id += 1

    current_agent_step: dict[str, Any] | None = None

    for event in events:
        event_type = event.get("type")
        payload = event.get("payload", {})
        timestamp = event.get("timestamp")

        if event_type == "user_message":
            content = payload.get("content", "")
            if content and (not steps or steps[-1].get("message") != content):
                steps.append(_user_step(step_id, content, timestamp))
                step_id += 1
            current_agent_step = None
            continue

        if event_type == "model_response":
            tool_calls = _tool_calls_from_model_response(payload)
            message = payload.get("content") or ("[tool call]" if tool_calls else "")
            current_agent_step = {
                "step_id": step_id,
                "source": "agent",
                "message": message,
                "timestamp": timestamp,
            }
            if tool_calls:
                current_agent_step["tool_calls"] = tool_calls
            steps.append(current_agent_step)
            step_id += 1
            continue

        if event_type == "tool_result" and current_agent_step is not None:
            _append_tool_result(
                current_agent_step,
                tool_name=payload.get("name", "unknown"),
                content=payload.get("content", ""),
                is_error=payload.get("is_error", False),
            )
            continue

        if event_type == "verification":
            feedback = payload.get("feedback", "")
            approved = payload.get("approved", False)
            label = "approved" if approved else "rejected"
            message = f"[verification {label}] {feedback}".strip()
            steps.append(
                {
                    "step_id": step_id,
                    "source": "agent",
                    "message": message,
                    "timestamp": timestamp,
                }
            )
            step_id += 1
            current_agent_step = None
            continue

        if event_type == "summarization":
            steps.append(
                {
                    "step_id": step_id,
                    "source": "agent",
                    "message": "[context summarized]",
                    "timestamp": timestamp,
                }
            )
            step_id += 1
            current_agent_step = None

    if not steps:
        steps.append(_user_step(1, task or "(no events recorded)", None))

    final_metrics: dict[str, Any] = {"total_steps": len(steps)}
    if prompt_tokens is not None:
        final_metrics["total_prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        final_metrics["total_completion_tokens"] = completion_tokens
    if cost_usd is not None:
        final_metrics["total_cost_usd"] = cost_usd
    extra: dict[str, Any] = {}
    if success is not None:
        extra["success"] = success
    if turns is not None:
        extra["turns"] = turns
    if extra:
        final_metrics["extra"] = extra

    return {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "agent": {
            "name": agent_name,
            "version": agent_version,
            "model_name": model_name,
        },
        "steps": steps,
        "final_metrics": final_metrics,
        "notes": "Exported from Garuda EventStore",
    }


def save_atif_trajectory(path: str | Path, trajectory: dict[str, Any]) -> None:
    """Write an ATIF trajectory dict to disk as formatted JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(trajectory, indent=2) + "\n", encoding="utf-8")


def _event_timestamp(event: dict[str, Any]) -> str | None:
    return event.get("timestamp")


def _user_step(step_id: int, message: str, timestamp: str | None) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": step_id,
        "source": "user",
        "message": message,
    }
    if timestamp:
        step["timestamp"] = timestamp
    return step


def _tool_calls_from_model_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index, call in enumerate(payload.get("tool_calls") or []):
        call_id = call.get("id") or f"call-{index}-{uuid.uuid4().hex[:8]}"
        calls.append(
            {
                "tool_call_id": call_id,
                "function_name": call.get("name", "unknown"),
                "arguments": call.get("arguments") or {},
            }
        )
    return calls


def _append_tool_result(
    agent_step: dict[str, Any],
    *,
    tool_name: str,
    content: str,
    is_error: bool,
) -> None:
    observation = agent_step.setdefault("observation", {"results": []})
    results = observation.setdefault("results", [])
    source_call_id = None
    tool_calls = agent_step.get("tool_calls") or []
    for call in tool_calls:
        if call.get("function_name") == tool_name:
            source_call_id = call.get("tool_call_id")
            break
    if source_call_id is None and tool_calls:
        source_call_id = tool_calls[min(len(results), len(tool_calls) - 1)].get("tool_call_id")

    result: dict[str, Any] = {"content": content}
    if source_call_id:
        result["source_call_id"] = source_call_id
    if is_error:
        result["extra"] = {"is_error": True}
    results.append(result)
