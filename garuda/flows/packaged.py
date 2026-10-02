"""Packaged flows (plan task C.6b, #158).

Three flows ship with Garuda and run by name. Their scout, planner and
reviewer steps are ``no-edits``, so with Claude Code and Codex as harnesses
they need no Docker:

- ``plan-only`` — scout, then planner: a plan, no changes;
- ``pair`` — coder, reviewed by reviewer (up to three rounds);
- ``plan-build-review`` — planner, then coder reviewed by reviewer.

A flow of the same name in your ``garuda.yaml`` replaces a packaged one.
``garuda config show --flow NAME`` prints one to copy. A packaged flow names
roles you define; running one whose roles are missing refuses and lists them.
"""

from __future__ import annotations

from garuda.config import garuda_yaml as gy

TEXT = """\
version: 1
flows:
  plan-only:
    description: A read-only look at the task, then a plan. Changes nothing.
    steps:
      - {id: scout, role: scout, write_policy: no-edits, outputs: [notes]}
      - {id: plan, role: planner, write_policy: no-edits, inputs: [notes], outputs: [plan]}
  pair:
    description: A coder and an independent reviewer, up to three rounds.
    steps:
      - {id: build, role: coder, outputs: [patch], review: {by: reviewer, max_rounds: 2}}
      - {id: review, role: reviewer, write_policy: no-edits, inputs: [patch], outputs: [review]}
  plan-build-review:
    description: A plan, an implementation, and an independent review of it.
    steps:
      - {id: plan, role: planner, write_policy: no-edits, outputs: [plan]}
      - {id: build, role: coder, inputs: [plan], outputs: [patch],
         review: {by: reviewer, max_rounds: 2}}
      - {id: review, role: reviewer, write_policy: no-edits, inputs: [patch], outputs: [review]}
"""

FLOWS: dict[str, dict] = gy.load_text(TEXT)["flows"]


def available(resolved) -> dict[str, tuple[dict, str]]:
    """``{name: (flow, source)}``: packaged flows, replaced by same-name configured ones."""
    out = {name: (flow, gy.PACKAGE) for name, flow in FLOWS.items()}
    if resolved is not None:
        for name, flow in resolved.config.get("flows", {}).items():
            out[name] = (flow, resolved.provenance.get(f"flows.{name}", gy.USER))
    return out


def required_roles(flow: dict) -> list[str]:
    roles = []
    for step in flow["steps"]:
        for name in [step.get("role"), *step.get("parallel", []),
                     step.get("review", {}).get("by")]:
            if name and name not in roles:
                roles.append(name)
    return roles


def missing_roles(flow: dict, resolved) -> list[str]:
    defined = set((resolved.config.get("roles", {}) if resolved else {}))
    return [r for r in required_roles(flow) if r not in defined]


def show(name: str, resolved) -> str:
    flows = available(resolved)
    if name not in flows:
        raise KeyError(name)
    flow, source = flows[name]
    header = f"# flow {name} ({source}); roles it needs: {', '.join(required_roles(flow))}\n"
    return header + gy.dump({"flows": {name: flow}})
