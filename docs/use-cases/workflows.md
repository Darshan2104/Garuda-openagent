# Ready-to-use workflows

Garuda Starters package five common journeys around the roles, flows, checks,
sessions and workspace controls you already configure. They use your configured
harnesses and models. Preview shows the effective bindings, readiness, supplied
sources, review policy and the exact quoted equivalent Garuda command; starting
work is always explicit. Starters do not require OpenRig.

## Set up and try the reconnect example

Install and authenticate an official supported harness using its own CLI, or
configure a native provider and its credential. Garuda does not import vendor
subscription credentials. See [External harnesses](../guides/external-harnesses.md)
and [Configuration](../guides/configuration.md).

Choose a **new directory outside any existing Git repository**. Its parent must
exist and contain no symlinked path components. The command refuses existing
files/directories and links, creates only the selected project, and initializes
and commits its local Git repository. It neither installs dependencies nor runs
an agent. Git initialization failure leaves the new directory for inspection.

```bash
garuda starter example reconnect ~/garuda-reconnect
cd ~/garuda-reconnect
python -m pytest
garuda init
garuda starter list
garuda starter show plan-change
garuda starter run plan-change --goal "Add reconnect status using the current client" --exclude "Transport rewrite" --constraints "Preserve retry behavior" --source client.py --preview
```

Run the last command again without `--preview` to produce a plan. For native
inference, choose your exact model during setup with
`garuda init --model native=YOUR_PROVIDER_MODEL`. No example model is hard-coded.

The example starts with a synchronous retry client and six deterministic tests.
The task is to add observable disconnected/reconnecting/connected status and
new transition tests while retaining retry attempts and delays. Existing tests
protect retry behavior; passing them alone does not prove the new feature.
Neither the project nor these docs contain a staged agent result.

`garuda init` proposes a read-only scout. **An ACP role with `permissions:
readonly` requires configured Docker confinement**; having a logged-in host CLI
alone is insufficient. Readiness identifies a missing image or Docker executable
without probing Docker. Follow the [role confinement instructions](teams.md#keep-a-role-from-changing-anything)
and authorize/authenticate an appropriate image yourself. Runtime preflight still
proves the actual mount and permission boundary. No-edits output detection and
worktrees do not replace confinement. Native planning uses Garuda's read-only
tool guardrails. Planning and questions need neither independent reviewers nor
acceptance checks, but still require their runtime's authority prerequisites.

Use `garuda init --project` to propose local checks. Review and confirm in a
terminal; this writes and trusts the exact project configuration. If that file
changes later, review it with `garuda config trust`. Explicit `--check` below
selects an existing role-run acceptance check without requiring project trust.

## Plan a change

```bash
garuda starter run plan-change --goal "Add reconnect status" --requirements "Cover all status transitions" --exclude "Transport rewrite" --constraints "Preserve retry delay" --source client.py
```

The packaged `plan-only` flow runs scout → planner. The scout produces `notes`,
and the planner consumes them and produces a bounded `plan` artifact. Garuda
requires the declared artifact envelopes and guards no-edits steps; the model
chooses its investigation and plan. Supplied file paths/optional sections are
hashed references, not evidence that the agent read them.

The existing intent-equivalent entry is:

```bash
garuda flow run plan-only --task "Plan reconnect status; cover transitions; preserve retry delay; exclude a transport rewrite; inspect client.py"
```

For the exact structured task and current effective overrides, add `--preview`
to the starter and copy its printed `equivalent_command`. Configuration can
replace a packaged flow; inspect the preview instead of assuming its steps.

Read the result and inspect the full historical plan:

```bash
garuda starter result FLOW_ID --json
garuda flow show FLOW_ID
```

No implementation starts automatically. To implement the selected plan, use the
result's explicit `FLOW:STEP:ATTEMPT` reference:

```bash
garuda starter run build-review --variant pair --plan-artifact FLOW_ID:plan:1
```

That requires a validated same-project completed producer, receipt/journal and
artifact identity. The full bounded plan and its original approved goal,
requirements, exclusions and constraints reach the next runtime. Missing,
changed, ambiguous or over-budget evidence refuses; a brief never substitutes
for the plan. Legacy plans without recorded constraints need explicit text.

## Plan from feedback

```bash
garuda starter run plan-feedback --feedback "Explain when reconnecting becomes connected" --current "Only connected is visible" --desired "Status follows the retry lifecycle" --constraints "Preserve retry behavior" --source client.py
```

This also uses scout → planner in `plan-only`, with feedback/current/desired
fields labelled in the runtime input. It produces notes and a plan, not a patch.
Garuda enforces the flow and artifact boundaries; the model decides what change
addresses the feedback. Its existing intent-equivalent entry is:

```bash
garuda flow run plan-only --task "Plan from feedback: explain when reconnecting becomes connected; preserve retry behavior; inspect client.py"
```

Add `--plan-artifact FLOW_ID:plan:1` when refining an earlier plan. New feedback
and constraints cannot silently erase that plan's recorded approved scope. Read
`starter result` and explicitly select implementation when the plan is ready.

## Build and review

```bash
garuda starter run build-review --goal "Add reconnect status" --requirements "Add deterministic status-transition tests" --exclude "Transport rewrite" --constraints "Preserve retry behavior" --source client.py --preview
```

The default `plan-build-review` flow runs planner → coder → reviewer. It requires
a plan, patch and structured review; a failed review can request bounded coder
retries. The existing intent-equivalent command is:

```bash
garuda flow run plan-build-review --task "Add reconnect status with deterministic transition tests; preserve retry behavior; exclude a transport rewrite"
```

A validated `--variant pair --plan-artifact FLOW_ID:plan:1` uses the existing
`pair` flow instead. Both require configured independent coder/reviewer
identities under Garuda's existing review rule. Preflight is configured evidence;
the engine rechecks identities actually launched or consulted. Primary, fallback
and configured consult runtime aliases resolve through the same trusted registry;
unknown or disabled references cannot prove required independence. Exact model
IDs retain the existing pair comparison; an absent ID means harness default.

A single-harness setup often binds coder and reviewer to the same identity.
Readiness reports `needs-setup` and offers three explicit remedies:

1. Connect a second supported harness, authenticate it through its own CLI, and
   rerun `garuda init`; inspect its role bindings and authority prerequisites.
2. **Build and check:** run the coder starter below with your acceptance check.
   This has **no review** and cannot silently discard a selected plan handoff.
3. Author a same-name flow override in your own `garuda.yaml`. First inspect
   `garuda config show --flow plan-build-review`, copy the complete definition,
   then explicitly set its review's `independent: false`. This is labelled
   **review not independent**, including on early failure. Garuda creates no
   waiver for you.

In this CLI release, reviewed flows report verification **unavailable**. They
have no acceptance-check phase, even when project checks exist. A review verdict
is separate from execution outcome and never proves test acceptance. Flow
`--check`, `--bg` and `--isolation` are unsupported starter inputs.

## Run work as a role (Build and check)

```bash
garuda starter run run-with-role --role coder --goal "Add reconnect status" --requirements "Add deterministic transition tests" --constraints "Preserve retry behavior" --exclude "Transport rewrite" --source client.py --name reconnect-status --check "python -m pytest"
```

This uses the existing role-run path, preserving native/ACP selection, workspace
admission, permissions, configured fallbacks and acceptance owners. The model
chooses the patch; the existing acceptance command produces its own receipt for
the actual candidate. The existing intent-equivalent entry is:

```bash
garuda run --role coder --task "Add reconnect status and deterministic transition tests; preserve retry behavior; exclude a transport rewrite" --name reconnect-status --check "python -m pytest"
```

Inspect execution, review (**no review**) and verification separately with
`garuda starter result reconnect-status`. A passed historical receipt is evidence
of that command on that recorded candidate, not proof of all requirements or of
the current workspace. Missing or conflicting evidence stays unknown. This
run-kind starter also supports existing explicit `--bg` and `--isolation`
options; queued requests revalidate sources/configuration before dispatch.

## Ask the specialist

```bash
garuda starter run ask-role --question "Can reconnect status use the current retry client?" --source client.py --role reviewer --name reconnect-question
```

The configured reviewer is the default; select another compatible role with
`--role`. This is a one-shot role run with no-edits, rather than live messaging
with an already-running agent. The existing intent-equivalent entry is:

```bash
garuda run --role reviewer --no-edits --task "Using client.py, can reconnect status use the current retry client?" --name reconnect-question
```

Use an idle checkout. The run reads the live workspace, not a stable consult
snapshot; another writer can cause output withholding. Garuda does not certify
that the model read each supplied source or that its answer is correct. Inspect
`starter result reconnect-question`; withheld answers remain withheld.

## Inspect, interrupt and recover

`--json` exposes the same result data. Selected reads are bounded and report
coverage; limited pages, damaged receipts/journals/artifacts, missing child
identities and unknown schemas remain incomplete. Artifacts describe historical
bytes. Reading a result probes no active process, Git, model or network and
starts no action. Context sources such as `--source session:session-name` reuse existing
project authorization; a cross-project brief requires the explicit user grant.
Full plan handoffs remain same-project even with that grant.

Use Ctrl-C to interrupt foreground work. Then inspect the existing owners:

```bash
garuda sessions show SESSION_ID
garuda flow show FLOW_ID
garuda flow resume FLOW_ID
garuda approvals list SESSION_ID
```

Flow recovery quarantines an intent with no authoritative receipt rather than
replaying uncertain work. Resume continues only from its existing recovery
boundary. Role continuation uses the existing `garuda run --resume SESSION_ID --task "Continue the recorded task"`
(and ACP recovery where applicable); starter commands add no new resume engine.
See [Sessions and flows](../guides/sessions-and-flows.md). Preview and result
next actions are suggestions: checks, approvals, implementation and recovery
remain explicit actions.

Use [recipes](automate.md#run-a-multi-step-recipe) for custom prompt sequences.
Recipes retain their existing whole-recipe workspace lease and are not aliases
for artifact/review flows. Dashboard discovery, HTTP starter launch and trusted
flow acceptance checks are later gated phases; this release is the CLI path.
