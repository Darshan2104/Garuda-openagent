"""A role's agent definition, bridged into teams (plan task H.10, #172).

``roles.<name>.agent`` names an agent definition (``profile`` is the older spelling of the
same field). :func:`bind` is called once on the plan that is about to start, **after** any
fallback was chosen, so it describes the harness that will actually run:

* a **native** role runs under the definition as ``garuda run --agent`` would. The
  definition's source digest becomes part of the plan's digest (its execution identity), and
  an effort that disagrees with the role's refuses with ``config.conflict``;
* an **ACP** role gets a *projection*: only what an external harness can honour. The
  definition's explicit or inherited ``model.effort``, ``permissions.mode`` and
  ``instructions`` apply (effort and permissions through the role; instructions as a labelled
  context block before the first task message, never as a system prompt). Anything else it
  asks for - skills, memory, hooks, a tool list, MCP servers, a final-output schema, a prompt
  *replacement*, a native model binding, permission rules - refuses field by field with
  ``agent.field_unsupported``. Native defaults are not expanded and then rejected: only what
  the definition (or an ``extends`` ancestor) actually declares is checked;
* a **native consulted** role runs as :func:`consult_spec` - the definition's model, instructions,
  memory and skills with the consult profile's read-only tools and limits.

A fallback that changes the harness kind is projected afresh; a definition the new kind cannot
honour refuses (configuration errors never fall back).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from garuda.agents import spec as agent_spec
from garuda.config.garuda_yaml import PERMISSIONS
from garuda.runtime.roles import RolePlan, RoleRefused

#: What an external harness can honour from a definition (the design's projection table).
ACP_SUPPORTED = frozenset({"model.effort", "permissions.mode"})  # plus appended instructions
LABEL = ("[garuda] The following are user-supplied role instructions for this task (context, "
         "not a system prompt):")
_HINTS = {
    "tools.mcp": "forwarding MCP servers to an external harness is not enabled",
    "model.binding": "a model binding is native-only; an ACP role uses the role's exact model_id",
    "instructions.mode": "an external harness's system prompt cannot be replaced; use append",
}


def _refused(code: str, message: str) -> RoleRefused:
    return RoleRefused(code, message)


def load(plan: RolePlan, workspace: str, agents_dir=None):
    from garuda.agents.spec_api import AgentSpec

    try:
        return AgentSpec.load(plan.profile, workspace, agents_dir)
    except agent_spec.AgentSpecError as exc:
        raise _refused(getattr(exc, "code", "agent.invalid"), str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise _refused("agent.not_found", f"role {plan.role}: {exc}") from exc


def _stricter(a: str | None, b: str | None) -> str | None:
    if a is None or b is None:
        return a if b is None else b
    return min(a, b, key=PERMISSIONS.index)


def own_instructions(resolved) -> tuple[str | None, bool]:
    """``(text, replaced)``: what the definition itself adds to the packaged native base prompt,
    and whether it *replaces* that prompt instead (which an external harness cannot honour)."""
    from garuda.types import DEFAULT_SYSTEM_PROMPT

    text = (resolved.instructions or "").strip()
    if resolved.instructions_replaced:
        return text or None, True
    base = DEFAULT_SYSTEM_PROMPT.strip()
    if not text or text == base:
        return None, False
    if text.startswith(base):
        return text[len(base):].strip() or None, False
    return text, False


def _check_effort(plan: RolePlan, agent_effort):
    if agent_effort and plan.effort and agent_effort != plan.effort:
        raise _refused("config.conflict",
                       f"role {plan.role} sets effort {plan.effort!r} but its agent "
                       f"{plan.profile!r} sets {agent_effort!r}; name one")
    return plan.effort or agent_effort


def _check_native_model(plan: RolePlan, resolved):
    alias = resolved.leaves.get("model.binding")
    if not alias or not plan.model_id:
        return
    from garuda.config.routing import load_global_orchestration

    binding = load_global_orchestration().model_bindings.get(alias)
    if binding is None:
        raise _refused("config.conflict", f"role {plan.role}: unknown agent model binding {alias!r}")
    if binding.reasoning.model != plan.model_id:
        raise _refused("config.conflict",
                       f"role {plan.role} names model {plan.model_id!r} but agent {plan.profile!r} "
                       f"binding {alias!r} names {binding.reasoning.model!r}")


def acp_unsupported(resolved) -> list[tuple[str, str]]:
    """``[(field, why)]`` for every explicit or inherited request an ACP role cannot honour."""
    found: list[tuple[str, str]] = []
    for path in sorted(resolved.leaves):
        if path in ACP_SUPPORTED or path in ("name", "description") or path not in agent_spec.FIELDS:
            continue  # supported, or identity text rather than a setting
        found.append((path, _HINTS.get(path, "an external harness has its own")))
    if own_instructions(resolved)[1]:
        found.append(("instructions.mode", _HINTS["instructions.mode"]))
    if resolved.output_schema is not None:
        found.append(("output.schema", "a final-output schema is enforced only on native runs"))
    if resolved.tools is not None and not any(p.startswith("tools.") for p in resolved.leaves):
        found.append(("tools", "a tool list applies only to native runs"))
    return found


def bind(plan: RolePlan | None, workspace: str, agents_dir=None) -> RolePlan | None:
    """The plan with its agent resolved (and projected, for an external harness)."""
    if plan is None or not plan.profile:
        return plan
    agent = load(plan, workspace, agents_dir)
    resolved = agent.resolved()
    digest = hashlib.sha256((plan.digest + ":" + agent.digest).encode()).hexdigest()
    if plan.kind == "native":
        _check_native_model(plan, resolved)
        _check_effort(plan, resolved.leaves.get("model.effort"))
        return replace(plan, agent_digest=agent.digest, digest=digest, agent_spec=agent)
    problems = acp_unsupported(resolved)
    if problems:
        shown = "; ".join(f"{field} ({why})" for field, why in problems[:8])
        raise _refused("agent.field_unsupported",
                       f"role {plan.role} runs on {plan.runtime_id}, which cannot honour agent "
                       f"{plan.profile!r}: {shown}")
    effort = _check_effort(plan, resolved.leaves.get("model.effort"))
    mode = resolved.leaves.get("permissions.mode")
    return replace(plan, agent_digest=agent.digest, digest=digest, effort=effort, agent_spec=None,
                   permissions=_stricter(plan.permissions, mode),
                   instructions=own_instructions(resolved)[0])


def native_spec(plan: RolePlan, workspace: str, agents_dir=None, *, agent_file=None):
    """Consume the native source bound at admission, checking explicit file agreement."""
    from garuda.agents.spec_api import AgentSpec

    agent = plan.agent_spec
    if agent is None or agent.digest != plan.agent_digest:
        raise _refused("config.conflict", f"role {plan.role}: native agent source is not bound")
    if agent_file is not None:
        selected = AgentSpec.from_file(agent_file, workspace, agents_dir)
        if selected.digest != agent.digest:
            raise _refused("config.conflict",
                           f"role {plan.role}: --agent-file disagrees with agent {plan.profile!r}")
    return agent


def with_instructions(plan: RolePlan | None, task: str) -> str:
    """The first message for an ACP role: its instructions as a labelled block, then the task."""
    if plan is None or not plan.instructions:
        return task
    return f"{LABEL}\n{plan.instructions}\n[end of role instructions]\n\n{task}"


def consult_spec(name: str, workspace: str, agents_dir=None):
    """A consulted native role's definition narrowed to the consult profile.

    The child keeps the definition's model, instructions, memory and skills but can use only
    the consult profile's tools, at ``readonly``, with no MCP servers or subagents, whatever
    the definition grants; the consult framing is appended to its instructions. A definition
    that *requires* an output schema or a completion check cannot be honoured by a read-only
    question-and-answer child and refuses admission (it is neither dropped nor turned on)."""
    from garuda.agents import resolve
    from garuda.agents.spec_api import AgentSpec
    from garuda.consult.errors import ConsultRefused

    agent = name if isinstance(name, AgentSpec) else AgentSpec.load(name, workspace, agents_dir)
    resolved = agent.resolved()
    required = [p for p in ("completion.verifier", "completion.acceptance_contract")
                if resolved.leaves.get(p) is True]
    if resolved.output_schema is not None:
        required.append("output.schema")
    if required:
        raise ConsultRefused(
            "consult.target_unavailable",
            f"agent {agent.name!r} requires {', '.join(required)}, which a read-only question-and-answer "
            "child cannot satisfy")
    profile = AgentSpec.load("garuda/consult", workspace, agents_dir).resolved()
    allowed = set(profile.tools or [])
    current = resolved.tools if resolved.tools is not None else resolve.builtin_tools()
    remove = sorted(t for t in current if t not in allowed)
    sections: dict = {"permissions": {"mode": "readonly"},
                      "tools": {"mcp": [], "subagents": []},
                      "instructions": {"text": profile.instructions or ""}}
    if remove:
        sections["tools"]["remove"] = remove
    return agent.narrow(**sections)
