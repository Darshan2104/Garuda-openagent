# Sessions and flows

A **flow** runs several roles one after another on one task — for example a
planner, then a coder, then an independent reviewer — each as its own
session, in one workspace that nothing else can touch while the flow runs.

## Before you start: roles

A flow is written in terms of **roles**: `scout`, `planner`, `coder`,
`reviewer`, or any you define. A role says which harness and exact model to
use, with what permissions. `garuda init` proposes the four standard ones on
the harnesses it finds on your `PATH` and writes them only after you agree:

```bash
garuda init
garuda doctor        # are those harnesses installed and logged in?
```

See [Roles and flows: garuda.yaml](configuration.md#roles-and-flows-garudayaml)
for the file itself.

## Run a packaged flow

Three flows ship with Garuda:

| Flow | Steps | Changes the workspace? |
|---|---|---|
| `plan-only` | scout → planner | No |
| `pair` | coder, reviewed by reviewer (up to three rounds) | Yes (the coder) |
| `plan-build-review` | planner → coder, reviewed by reviewer | Yes (the coder) |

```bash
garuda flow run plan-build-review -t "Add a --version flag"
garuda flow show <flow-id>      # steps, attempts, receipts, outputs
```

The scout, planner and reviewer steps are `no-edits`: their requests to
change files or run commands are refused, and afterwards Garuda checks that
the workspace really is unchanged. If it is not, the flow stops, that step's
output is withheld and the changes are left for you to inspect. This is a
guardrail, not confinement.

A flow that needs a role you have not defined refuses and lists the roles it
needs. To change a packaged flow, print it, copy it into your `garuda.yaml`
and edit it there; a flow of the same name in your file replaces the
packaged one:

```bash
garuda config show --flow plan-build-review
```

## What passes between steps

Steps hand each other typed **artifacts** — `plan`, `patch`, `review`,
`findings`, `notes`, `summary` — and nothing else. A step's output becomes an
artifact only in this envelope at the end of its answer:

```text
<garuda-artifact type="plan">
1. ...
</garuda-artifact>
```

Garuda stores each artifact once, records its digest, and checks it again
before the next step reads it. An artifact that describes the workspace
(`patch`, `review`, `findings`) is refused if the workspace has changed since
it was produced. The next step receives it as labelled data, not as
instructions.

## Reviews

A step with `review: {by: reviewer, max_rounds: 2}` is reviewed by the flow's
last step. The reviewer answers with a `review` block:

```text
verdict: changes
findings:
- [major] the retry loop never ends
- [nit] typo in the help text
```

A `blocker` or `major` finding means changes are requested, and only the
reviewed step runs again, with the findings, before the reviewer looks
again. `max_rounds: 2` means at most three coder/reviewer rounds. The flow
then ends `review_approved` or `review_changes_requested`. A review is not
verification: to verify, add acceptance checks (see
[How Garuda works](how-garuda-works.md)).

By default the reviewer must be independent: it may not run as the same
harness and model as the role it reviews, as one of that role's fallbacks,
or as a role it consulted.

## Consults

A role can ask another role one question mid-task. In `garuda.yaml` (user file
only) a role lists the roles it may ask:

```yaml
roles:
  coder: {harness: native, model_id: gpt-5, consult: [reviewer]}
  reviewer: {harness: native, model_id: claude-sonnet-5}
```

The coder then has a `consult` tool whose `target` is limited to those roles.
Each call:

- runs the target role as its own session, **read-only, in a separate snapshot
  of the workspace**: it cannot edit your files, run commands or call other
  tools, and the snapshot is checked afterwards (`consult.unexpected_changes`
  withholds the answer);
- is bounded by `consults:` limits (questions per session, timeout, question and
  answer size, child turns), with a deadline from admission to cleanup;
- cannot be nested: a consulted role cannot consult;
- returns the answer as labelled, untrusted advice. It is not verification and
  does not replace acceptance checks;
- uses a durable request id, so a retried call never asks twice, and leaves a
  receipt without the question or answer text.

Native targets only for now. An external (ACP) target runs only inside the
Docker read-only confinement; without it the call is refused
(`consult.isolation_unavailable`) and nothing runs on the host. Quiescing the
asker is "tool calls run one at a time"; a run with background processes
refuses the consult rather than guessing (`consult.snapshot_unstable`). This
read-only profile is a guardrail, not a sandbox boundary.

## Interruptions

Before each step starts, Garuda records that it is about to run it; after
it ends, it writes an immutable receipt. If Garuda is killed in between, that
step is **quarantined** and never run again automatically. `garuda flow
resume <flow-id>` continues after the last step that has a receipt; with a
quarantined step it refuses, so you can look at the workspace first.
