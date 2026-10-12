"""Resolved native configuration, before any client or tool activation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from garuda.agents.loader import AgentProfile
    from garuda.mcp.config import McpServerConfig
    from garuda.model.config import CollectionPolicy, ModelBindings, ResolvedField
    from garuda.types import AgentConfig


@dataclass(frozen=True)
class NativeRunPlan:
    """Per-request configuration snapshot with optional caller-owned SDK clients.

    Nested configuration remains ordinary Python data for the existing SDK;
    activation copies it for each run. This is not a serialized worker grant.
    SDK clients deliberately retain identity and are never copied.
    """

    workspace: str
    profile: AgentProfile
    config: AgentConfig
    bindings: ModelBindings
    provenance: dict[str, ResolvedField]
    collection_policy: CollectionPolicy
    mcp_paths: tuple[str, ...]
    mcp_user_paths: tuple[str, ...]
    toolkit_names: tuple[str, ...] | None
    mcp_configs: tuple[McpServerConfig, ...] | None = field(default=None, repr=False)
    sdk_reasoning: Any = field(default=None, repr=False)
    sdk_collection: Any = field(default=None, repr=False)
