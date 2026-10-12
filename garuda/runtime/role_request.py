"""Closed JSON snapshots for owned native and ACP roles; no executable serialization."""

from __future__ import annotations

import json
from dataclasses import asdict, fields, replace
from pathlib import Path

from garuda.agents.loader import AgentProfile
from garuda.agents.native_plan import NativeRunPlan
from garuda.agents.setup import RuntimeCatalog
from garuda.model.config import (
    CollectionBudget,
    CollectionFallback,
    CollectionPolicy,
    ConfigError,
    ModelBindings,
    ModelSpec,
    Provenance,
    ResolvedField,
)
from garuda.runtime.protocol import RuntimeCapabilities, RuntimeKind
from garuda.runtime.registry import AuthProbe, LoginFlow, RuntimeManifest, RuntimeRegistry
from garuda.runtime.roles import RolePlan
from garuda.types import AgentConfig


def _json_value(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError("owned role configuration must contain JSON values")


def encode_document(document):
    return json.dumps(document, default=_json_value, allow_nan=False, separators=(",", ":"))


def freeze_native(plan: NativeRunPlan) -> dict:
    """Capture the resolved run and trust-filtered MCP values before activation."""
    if plan.sdk_reasoning is not None or plan.sdk_collection is not None:
        raise ConfigError("owned role workers require configured model transports, not SDK objects")
    if plan.collection_policy.enabled and plan.bindings.collection is not None:
        raise ConfigError("owned role workers do not yet support nested collection profile resolution")
    from garuda.config.agent_home import resolve_agent_home
    from garuda.mcp.client import McpClientManager

    if resolve_agent_home(plan.workspace).load_project_tools:
        raise ConfigError("owned role workers do not yet support executable project tool imports")
    configs = McpClientManager.resolve_configs(
        list(plan.mcp_paths), plan.profile.mcp_servers, workspace=plan.workspace,
        user_paths=list(plan.mcp_user_paths))
    snapshot = replace(plan, mcp_paths=(), mcp_configs=tuple(configs))
    return json.loads(encode_document(asdict(snapshot)))


def native_from_document(value: dict) -> NativeRunPlan:
    from garuda.mcp.config import McpServerConfig

    profile = dict(value["profile"])
    profile["source_path"] = Path(profile["source_path"]) if profile["source_path"] else None
    profile["declared_fields"] = set(profile["declared_fields"])
    bindings = value["bindings"]
    policy = value["collection_policy"]
    provenance = {}
    for role, entry in value["provenance"].items():
        resolved_value = ModelSpec(**entry["value"]) if entry["value"] is not None else None
        provenance[role] = ResolvedField(resolved_value, Provenance(entry["provenance"]), entry["source"])
    if value["sdk_reasoning"] is not None or value["sdk_collection"] is not None or value["mcp_paths"]:
        raise ValueError("role snapshot contains live or unresolved inputs")
    return NativeRunPlan(
        workspace=value["workspace"], profile=AgentProfile(**profile), config=AgentConfig(**value["config"]),
        bindings=ModelBindings(ModelSpec(**bindings["reasoning"]),
                               ModelSpec(**bindings["collection"]) if bindings["collection"] else None),
        provenance=provenance,
        collection_policy=CollectionPolicy(
            **{k: v for k, v in policy.items() if k not in {"budget", "fallback"}},
            budget=CollectionBudget(**policy["budget"]), fallback=CollectionFallback(**policy["fallback"])),
        mcp_paths=(), mcp_user_paths=tuple(value["mcp_user_paths"]),
        toolkit_names=tuple(value["toolkit_names"]) if value["toolkit_names"] is not None else None,
        mcp_configs=tuple(McpServerConfig(**config) for config in value["mcp_configs"]),
    )


def freeze_acp(catalog, runtime_id):
    # Selection checks trusted disablement and alias narrowing before transfer.
    resolved = catalog.registry.get(runtime_id)
    manifest = catalog.registry.manifest_for(runtime_id)
    return json.loads(encode_document(asdict(replace(manifest, capabilities=resolved.capabilities))))


def catalog_from_document(value):
    manifest = dict(value)
    manifest["kind"] = RuntimeKind(manifest["kind"])
    manifest["capabilities"] = RuntimeCapabilities(frozenset(manifest["capabilities"]["names"]))
    manifest["command"] = tuple(manifest["command"]) if manifest["command"] else None
    manifest["version_args"] = tuple(manifest["version_args"]) if manifest["version_args"] else None
    manifest["warnings"] = tuple(manifest["warnings"])
    probe = manifest["auth_probe"]
    manifest["auth_probe"] = AuthProbe(**{**probe, "argv": tuple(probe["argv"])}) if probe else None
    manifest["login"] = LoginFlow(**manifest["login"])
    return RuntimeCatalog(RuntimeRegistry([RuntimeManifest(**manifest)]))


def freeze_role(plan):
    if plan is None:
        return None
    return json.loads(encode_document({field.name: getattr(plan, field.name) for field in fields(plan)
                                      if field.name not in {"agent_spec", "identity_registry"}}))


def role_from_document(value):
    return RolePlan(**value) if value is not None else None
