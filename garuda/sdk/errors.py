"""Typed SDK refusals raised before anything launches."""

from __future__ import annotations


class ToolsUnsupportedOnRuntime(ValueError):
    """Custom SDK tools cannot reach the selected runtime (#702).

    An ACP harness runs its own tool loop and is never offered Garuda's
    in-process tools, so a registered tool would be dropped while the run
    still reported success. The run is refused before the harness starts.
    """

    code = "sdk.tools_unsupported"


def refuse_unservable_tools(runtime: str, extra_tools, load_project_tools) -> None:
    """Refuse custom or project tools on a runtime that cannot receive them."""
    if runtime == "native":
        return
    if extra_tools:
        names = ", ".join(sorted(getattr(t, "name", repr(t)) for t in extra_tools))
        raise ToolsUnsupportedOnRuntime(
            f"custom tools ({names}) cannot be offered to the ACP runtime {runtime!r}: "
            "the harness uses its own tools. Use runtime='native' for custom tools, "
            "or run without them")
    if load_project_tools:
        raise ToolsUnsupportedOnRuntime(
            f"load_project_tools=True cannot be honoured on the ACP runtime {runtime!r}: "
            "the harness uses its own tools. Use runtime='native', or leave it unset")
