"""Trusted tools at the parent/collection-worker boundary."""

from __future__ import annotations

from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.health import EnvironmentUnavailableError
from garuda.workspace.protocol import Environment


class DelegateCollectionTool:
    """Ask the configured collection model for a bounded evidence report."""

    name = "delegate_collection"
    effect = ToolEffect.READ_ONLY
    description = (
        "Delegate a bounded, non-mutating evidence-gathering job to the collection model. "
        "Use it for independent reads; you remain responsible for edits and task completion."
    )
    parameters = {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "description": "Specific collection objective."},
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete questions the report must answer or mark unknown.",
            },
            "allowed_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional workspace-relative files/directories the worker may inspect. "
                    "Empty means the workspace root."
                ),
            },
            "allowed_sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional http(s) source URL prefixes; empty disables web reads.",
            },
            "handoff": {
                "type": "string",
                "enum": ["none", "brief"],
                "description": "none sends only this request; brief also sends bounded run state.",
            },
            "max_turns": {"type": "integer", "minimum": 1},
            "max_tokens": {"type": "integer", "minimum": 1},
        },
        "required": ["objective", "questions"],
        "additionalProperties": False,
    }

    async def execute(
        self, arguments: dict, env: Environment, ctx: ToolContext
    ) -> ToolResult:
        coordinator = ctx.collection_coordinator
        if coordinator is None:
            return ToolResult(
                tool_call_id="",
                content="Collection delegation is not enabled for this run.",
                is_error=True,
            )
        try:
            content = await coordinator.delegate(arguments)
        except EnvironmentUnavailableError:
            raise
        except Exception as exc:  # validation/provider failures are a tool result
            return ToolResult(
                tool_call_id="",
                content=f"Collection failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )
        return ToolResult(tool_call_id="", content=content)


class SubmitCollectionTool:
    """Internal terminal tool exposed only inside collection child runs."""

    name = "submit_collection"
    effect = ToolEffect.READ_ONLY
    description = (
        "Submit the structured evidence report. This completes only this collection job, "
        "never the parent task."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["file", "command", "document", "url", "buffer"],
                        },
                        "location": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                    "required": ["kind", "location", "detail"],
                    "additionalProperties": False,
                },
            },
            "unknowns": {"type": "array", "items": {"type": "string"}},
            "buffer_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "findings", "evidence", "unknowns", "buffer_ids"],
        "additionalProperties": False,
    }

    async def execute(
        self, arguments: dict, env: Environment, ctx: ToolContext
    ) -> ToolResult:
        raise RuntimeError("submit_collection is handled by CollectionCompletionGate")
