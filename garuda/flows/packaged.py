"""Packaged flows (plan task C.6b, #158).

Three flows ship with Garuda and run by name. Their scout, planner and
reviewer steps are ``no-edits`` and require Docker confinement when run by an
external harness:

- ``plan-only`` — scout, then planner: a plan, no changes;
- ``pair`` — coder, reviewed by reviewer (up to three rounds);
- ``plan-build-review`` — planner, then coder reviewed by reviewer; the
  reviewer receives the plan and the patch.

A flow of the same name in your ``garuda.yaml`` replaces a packaged one.
``garuda flow list`` lists them all; ``garuda config show --flow NAME`` prints one
to copy. A packaged flow names
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
      - {id: review, role: reviewer, write_policy: no-edits, inputs: [plan, patch], outputs: [review]}
"""

FLOWS: dict[str, dict] = gy.load_text(TEXT)["flows"]

#: Earlier packaged definitions, by name. A checked flow binds the digest of
#: its definition into its saved check request; resuming one started under an
#: earlier Garuda must keep the definition it started with (#818).
_PREVIOUS_TEXT = """\
version: 1
flows:
  plan-build-review:
    description: A plan, an implementation, and an independent review of it.
    steps:
      - {id: plan, role: planner, write_policy: no-edits, outputs: [plan]}
      - {id: build, role: coder, inputs: [plan], outputs: [patch],
         review: {by: reviewer, max_rounds: 2}}
      - {id: review, role: reviewer, write_policy: no-edits, inputs: [patch], outputs: [review]}
"""

PREVIOUS: dict[str, list[dict]] = {
    name: [flow] for name, flow in gy.load_text(_PREVIOUS_TEXT)["flows"].items()}


def available(resolved) -> dict[str, tuple[dict, str]]:
    """``{name: (flow, source)}``: packaged flows, replaced by same-name configured ones."""
    out = {name: (flow, gy.PACKAGE) for name, flow in FLOWS.items()}
    if resolved is not None:
        for name, flow in resolved.config.get("flows", {}).items():
            out[name] = (flow, resolved.provenance.get(f"flows.{name}", gy.USER))
    return out


#: ``garuda flow list`` labels for where a flow comes from.
SOURCE_LABELS = {gy.PACKAGE: "packaged", gy.USER: "user", gy.PROJECT: "project"}


def listing(resolved) -> list[dict]:
    """One row per available flow, by name: what ``garuda flow list`` prints."""
    rows = []
    for name, (flow, source) in sorted(available(resolved).items()):
        rows.append({"name": name, "source": SOURCE_LABELS.get(source, source),
                     "description": flow.get("description"),
                     "steps": len(flow.get("steps", [])),
                     "roles": required_roles(flow),
                     "missing_roles": missing_roles(flow, resolved)})
    return rows


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


def recorded(name: str, flow: dict, source: str, flow_digest) -> dict:
    """The packaged definition a checked flow recorded when it started.

    ``flow_digest`` is the definition digest saved in the flow's check
    request. A packaged flow whose current definition no longer matches it
    resumes with the earlier packaged definition that does; anything else
    (a configured flow, or no match) is returned unchanged, so the engine's
    own comparison still refuses a changed definition.
    """
    from garuda.core.acceptance_records import digest

    if source != gy.PACKAGE or digest(flow) == flow_digest:
        return flow
    return next((old for old in PREVIOUS.get(name, []) if digest(old) == flow_digest), flow)


def show(name: str, resolved) -> str:
    flows = available(resolved)
    if name not in flows:
        raise KeyError(name)
    flow, source = flows[name]
    header = f"# flow {name} ({source}); roles it needs: {', '.join(required_roles(flow))}\n"
    return header + gy.dump({"flows": {name: flow}})
