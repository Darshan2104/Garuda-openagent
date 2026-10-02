"""Garuda Open Agent — universal provider-agnostic agent harness."""

__version__ = "1.2.0"


def __getattr__(name):
    # `from garuda import AgentSpec, SoftwareAgent` without importing the SDK (and its
    # model dependencies) when only the package version is wanted.
    if name in ("AgentSpec", "SoftwareAgent", "Conversation"):
        from garuda import sdk

        return getattr(sdk, name)
    raise AttributeError(f"module 'garuda' has no attribute {name!r}")
