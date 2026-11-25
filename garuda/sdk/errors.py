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


class OptionsUnsupportedOnRuntime(ValueError):
    """A model or agent choice cannot reach the selected runtime (#712).

    An ACP harness runs with its own configured model and agent, so a
    ``model=``, ``reasoning_model=``, ``collection_model=``, ``model_binding=``
    or non-default ``agent=`` would be ignored while the run still reported
    success. The run is refused before the harness starts.
    """

    code = "sdk.options_unsupported"


def refuse_unservable_options(runtime: str, *, model, agent, reasoning_model=None,
                              collection_model=None, model_binding=None) -> None:
    """Refuse native model or agent options on a runtime that ignores them."""
    if runtime == "native":
        return
    from garuda.model.protocol import DEFAULT_MODEL

    ignored = []
    if model is not None and not (isinstance(model, str) and model == DEFAULT_MODEL):
        ignored.append("model")
    for name, value in (("reasoning_model", reasoning_model),
                        ("collection_model", collection_model),
                        ("model_binding", model_binding)):
        if value is not None:
            ignored.append(name)
    if not (isinstance(agent, str) and agent == "build"):
        ignored.append("agent")
    if ignored:
        raise OptionsUnsupportedOnRuntime(
            f"{', '.join(ignored)} cannot be applied on the ACP runtime {runtime!r}: the harness "
            "runs its own configured model and agent. Configure the harness instead, use "
            "runtime='native', or leave them unset")
