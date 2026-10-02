"""Every accepted agent-definition field has a typed owner (plan task H.12a, #160).

:func:`check` validates a resolved version 1 definition before activation —
numbers finite and in range, and the cross-field budgets that would otherwise
fail mid-run — and refuses a project (or narrower delegate) that turns off a
required gate. :func:`narrow_docker` applies a definition's Docker limits,
which can only narrow what the operator granted. Compilation itself is the
profile projection (``AgentProfile.to_agent_config``): it builds plain config
and never instantiates a model client, container or MCP server.

========================================  ====================================
Field                                     Owner
========================================  ====================================
``model.binding/effort/thinking_*``       ``resolve_model_bindings`` and the
                                          model spec's reasoning settings
``model.max_output_tokens``               ``AgentConfig.max_tokens``
``context.*``                             ``AgentConfig.max_context_tokens``,
                                          ``condenser``, ``proactive_summarize_
                                          threshold``, ``max_output_bytes``,
                                          reserves and margins
``limits.max_turns/deadline_sec``         ``AgentConfig.max_turns/deadline_sec``
``completion.mode/verifier/...``          ``apply_mode_preset`` (declared fields
                                          win), ``enable_verifier``,
                                          ``enable_acceptance_contract``
``workspace.kind/docker.*``               the authorized environment;
                                          ``docker.*`` only narrows it
========================================  ====================================
"""

from __future__ import annotations

from garuda.agents import spec

#: Postures whose completion gate requires the acceptance contract.
REQUIRES_CONTRACT = ("eval", "rigorous")


def _budget(path: str, message: str, source) -> spec.AgentSpecError:
    return spec.AgentSpecError("agent.invalid_budget", path, message, source=str(source or ""))


def check(agent) -> None:
    """Refuse an impossible budget or a project switching off a required gate."""
    from garuda.types import AgentConfig

    leaves = agent.leaves
    source = agent.source.path

    def get(path, default):
        return leaves.get(path, default)

    context = get("context.max_tokens", AgentConfig.max_context_tokens)
    output = get("model.max_output_tokens", None)
    reserve = get("context.reserved_output_tokens", AgentConfig.reserved_output_tokens)
    margin = get("context.safety_margin_tokens", AgentConfig.context_safety_margin_tokens)
    if (output or reserve) + margin >= context:
        path = "model.max_output_tokens" if output else "context.reserved_output_tokens"
        raise _budget(path, f"the output reserve ({output or reserve}) plus the safety margin "
                            f"({margin}) must stay below context.max_tokens ({context})", source)
    summarize = get("context.summarize_after_tokens", AgentConfig.proactive_summarize_threshold)
    if summarize >= context:
        raise _budget("context.summarize_after_tokens",
                      f"{summarize} must be below context.max_tokens ({context})", source)
    low = get("context.min_tool_output_bytes", AgentConfig.min_output_bytes)
    high = get("context.max_tool_output_bytes", AgentConfig.max_output_bytes)
    if low > high:
        raise _budget("context.min_tool_output_bytes",
                      f"{low} is above context.max_tool_output_bytes ({high})", source)
    if get("limits.max_turns", 1) < 1:
        raise _budget("limits.max_turns", "must be at least 1", source)
    # The verifier is always required; the acceptance contract is required by
    # the eval and rigorous postures. A project may not switch either off.
    required = ["completion.verifier"]
    if leaves.get("completion.mode") in REQUIRES_CONTRACT:
        required.append("completion.acceptance_contract")
    for path in required:
        origin = agent.provenance.get(path)
        if leaves.get(path) is False and origin is not None and origin.authority == "project":
            raise spec.AgentSpecError(
                "agent.required_gate", path,
                "a project definition cannot turn off a required completion gate; set it in "
                "your user agents or pass a flag for one run", source=str(origin.path))


def _size(value) -> float | None:
    """Docker memory like ``2g`` / ``512m`` in bytes, for comparing."""
    if value is None:
        return None
    text = str(value).strip().lower()
    units = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}
    try:
        return float(text[:-1]) * units[text[-1]] if text[-1] in units else float(text)
    except ValueError:
        return None


def narrow_docker(config, profile) -> None:
    """Apply a definition's Docker limits only where they narrow the grant."""
    if profile.docker_network is False:
        config.docker_network = "none"
    if profile.docker_memory is not None:
        mine, granted = _size(profile.docker_memory), _size(config.docker_memory)
        if mine is not None and (granted is None or mine < granted):
            config.docker_memory = str(profile.docker_memory)
    if profile.docker_cpus is not None:
        try:
            granted = float(config.docker_cpus) if config.docker_cpus is not None else None
        except ValueError:
            granted = None
        if granted is None or float(profile.docker_cpus) < granted:
            config.docker_cpus = f"{float(profile.docker_cpus):g}"
