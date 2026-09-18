"""Provider-agnostic construction of reasoning/collection model clients.

``ModelFactory`` builds one client per role from a resolved ``ModelBindings``.
Built-in transport is ``litellm``; SDK callers may supply objects satisfying
the ``Model`` protocol for either role (kept by identity, never wrapped).

No spec stores an API key — keys arrive via provider-supported environment or
explicit caller arguments only, and never appear in repr, serialized config,
or events.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from garuda.model.config import ConfigError, ModelBindings, ModelSpec, Provenance


@dataclass(frozen=True)
class ResolvedModels:
    reasoning: Any
    collection: Any | None
    bindings: ModelBindings
    provenance: dict[str, object]


TransportBuilder = Callable[[ModelSpec], Any]

_registry: dict[str, TransportBuilder] = {}


def register_transport(name: str, builder: TransportBuilder) -> None:
    _registry[name] = builder


def _litellm_builder(spec: ModelSpec) -> Any:
    from garuda.model.litellm_model import LitellmModel

    return LitellmModel(
        model_name=spec.model,
        api_base=spec.api_base,
        reasoning_effort=spec.reasoning_effort,
        thinking_budget_tokens=spec.thinking_budget_tokens,
        request_timeout=spec.timeout_sec or 600.0,
    )


register_transport("litellm", _litellm_builder)


def _coerce_sdk_model(candidate: Any, *, role: str) -> Any | None:
    if candidate is None:
        return None
    name = getattr(candidate, "model_name", None)
    complete = getattr(candidate, "complete", None)
    if isinstance(name, str) and callable(complete):
        return candidate
    raise ConfigError(f"{role}: supplied model object does not satisfy the Model protocol")


class ModelFactory:
    """Builds role clients once per run; validates collection tool support."""

    def __init__(self, transports: dict[str, TransportBuilder] | None = None):
        self._transports = dict(_registry) if transports is None else dict(transports)

    def build_spec(self, spec: ModelSpec, *, role: str) -> Any:
        builder = self._transports.get(spec.transport)
        if builder is None:
            raise ConfigError(
                f"{role}: unsupported transport {spec.transport!r} "
                f"(known: {sorted(self._transports)})"
            )
        return builder(spec)

    def build(
        self,
        bindings: ModelBindings,
        provenance: dict[str, object] | None = None,
        *,
        reasoning_model: Any | None = None,
        collection_model: Any | None = None,
        require_collection_tools: bool = True,
    ) -> ResolvedModels:
        if reasoning_model is not None:
            reasoning = _coerce_sdk_model(reasoning_model, role="reasoning")
        else:
            reasoning = self.build_spec(bindings.reasoning, role="reasoning")
        if collection_model is not None:
            collection = _coerce_sdk_model(collection_model, role="collection")
        elif bindings.collection is not None:
            collection = self.build_spec(bindings.collection, role="collection")
        else:
            collection = None
        if collection is not None and require_collection_tools:
            if not getattr(collection, "supports_tool_calling", False):
                raise ConfigError("collection: model must support tool calling")
        return ResolvedModels(
            reasoning=reasoning,
            collection=collection,
            bindings=bindings,
            provenance=dict(provenance or {}),
        )


def safe_model_identity(model: Any) -> str:
    """Event-safe model name without credentials or query parameters."""
    name = getattr(model, "model_name", None) or getattr(model, "_model_name", None) or "unknown"
    return str(name).split("?", 1)[0]


def describe_provenance(provenance: dict[str, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    for role, item in provenance.items():
        prov = getattr(item, "provenance", None)
        if isinstance(prov, Provenance):
            out[role] = prov.value
        elif isinstance(item, Provenance):
            out[role] = item.value
        else:
            out[role] = str(getattr(item, "provenance", "unknown"))
    return out
