from garuda.model.config import (
    CollectionBudget,
    CollectionFallback,
    CollectionPolicy,
    ModelBindings,
    ModelSpec,
    Provenance,
    narrow_collection_policy,
    resolve_model_bindings,
    with_compat_reasoning_settings,
)
from garuda.model.factory import ModelFactory, ResolvedModels, safe_model_identity
from garuda.model.protocol import Model, ModelResponse
from garuda.model.script_model import ScriptModel

__all__ = [
    "CollectionBudget",
    "CollectionFallback",
    "CollectionPolicy",
    "LitellmModel",
    "Model",
    "ModelBindings",
    "ModelFactory",
    "ModelResponse",
    "ModelSpec",
    "Provenance",
    "ResolvedModels",
    "ScriptModel",
    "narrow_collection_policy",
    "resolve_model_bindings",
    "safe_model_identity",
    "with_compat_reasoning_settings",
]


def __getattr__(name: str):
    # Resolving configuration must not initialize provider libraries (which can
    # perform network requests at import time). Preserve the public class export.
    if name == "LitellmModel":
        from garuda.model.litellm_model import LitellmModel

        globals()[name] = LitellmModel
        return LitellmModel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
