"""Context exports without loading inference clients for pure redaction."""

from garuda.context.shaper import shape_observation

__all__ = ["ContextManager", "shape_observation"]


def __getattr__(name):
    if name == "ContextManager":
        from garuda.context.manager import ContextManager

        return ContextManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
