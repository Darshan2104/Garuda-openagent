# Sessions and flows

Every run is recorded as a **session**. A **flow** runs several roles one
after another on one task (for example a planner, then a coder, then an
independent reviewer), each as its own session, in one workspace that nothing
else can touch while the flow runs.

For copy-ready recipes, see [Level 4 · Run work in parallel](../use-cases/parallel.md)
and [Level 5 · Build a team of roles](../use-cases/teams.md).

## Sessions

A session lives in `~/.agent/sessions/<id>/`: its metadata, event log,
approvals and, for a background run, `worker.log`.

### The four facts

Garuda keeps four separate facts about every session, so "it finished" is
never confused with "it worked":

| Fact | Values | Answers |
|---|---|---|
| `process` | `live`, `exited`, `missing`, `unknown` | Is its process still running? |
| `work` | `queued`, `working`, `waiting`, `done`, `stopped` | Where is the work? |
| `outcome` | `completed`, `failed`, `refused`, `cancelled` | How did it end? |
| `verification` | `passed`, `failed`, `unavailable`, `invalidated` | Did a check you trust pass? |

`garuda sessions` shows one word per session: the work state while it is
active, the outcome once it ends, or **crashed** when the process is gone but
the work never ended. The agent's own completion check is recorded apart, as
`self_check`.

```bash
garuda sessions                 # this project's recent sessions
garuda sessions show NAME       # everything about one session
garuda sessions --json          # the same model the dashboard serves
```

### Names and references

- `--name NAME` names a session (unique in the project). Without it, Garuda
  makes one from the task.
- Anywhere a session is expected (`--resume`, `sessions show`, `approvals`,
  `runtime --session`) you can give a full ID, a unique prefix, a name, or
  `latest` (this project's newest).
- Names, prefixes and `latest` are looked up in the current project; add
  `--all-projects` to `--resume latest` to search every project. A full ID
  always resolves.

### Briefs: reuse what another session did

`@name` in a task, or `--with NAME`, attaches a session's **brief**: its task,
state, changed files, checks and final answer, as labelled data. The
transcript is never attached. Another project's session needs
`--with-id FULL_ID` and your explicit grant (a terminal prompt, or
`--allow-cross-project-context`).

`--resume` is different: it continues the conversation itself. `--resume S
--as RUNTIME` starts a linked session on another runtime from `S`'s brief.

### Verification

A session is **verified** only by a check you stand behind: `--check COMMAND`,
a `checks:` entry in your `garuda.yaml`, or one in a project `garuda.yaml` you
trusted. Garuda runs them after the session and records a receipt for each
(who asked, the exact command, the code it ran against, the result). With none,
verification is `unavailable`. A check that changes the code it checks, or
that depends on test setup the session changed, can't pass.

### Background sessions and the queue

`garuda run --bg` writes the session as `queued`, adds it to a durable
first-in, first-out queue for its runtime, and starts a detached worker. The
worker waits for a slot under the runtime's
[capacity](configuration.md#runtime-capacity) without holding the workspace,
then runs the normal `run` path. `garuda sessions cancel` removes a queued
entry, or stops the worker once its recorded process identity matches.

### Worktree sessions

`--isolation worktree` gives a session its own linked Git worktree on a
`garuda/<session>` branch under `~/.agent/worktrees/`; `--isolation auto`
does so only while another session is editing. Uncommitted changes in your
checkout aren't carried over.

`garuda sessions merge S --check CMD` snapshots the worktree (uncommitted
edits included), previews the merge into your branch (or `--into BRANCH`),
runs each check in Docker against the merged tree (read-only mount, no
network, no Docker socket, not root), and publishes
`refs/garuda/integration/<S>` with the `git merge --ff-only` command to apply
it. It never changes your checkout. `garuda sessions remove-worktree S`
removes the worktree once its work is published (`--force` otherwise).

A worktree is a separate place to edit, not confinement.

### Approvals from elsewhere

While an interactive prompt waits (in `garuda chat` or a dashboard
conversation), the request is also published to the session's `approvals/`
folder. `garuda approvals list S` shows it and `garuda approvals answer S ID
--allow` (or `--deny`) answers it; the dashboard's Approvals page does the
same. The first answer wins and is recorded once. An answer is bound to that
exact request: a replayed, edited, expired or late answer is a denial, and so
is a change in the permission ceiling after the request.

Headless runs (`garuda run`, `--bg`) deny approval requests at once, so they
never wait for an answer.

## Flows

### Before you start: roles

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

### Run a packaged flow

Three flows ship with Garuda:

| Flow | Steps | Changes the workspace? |
|---|---|---|
| `plan-only` | scout → planner | No |
| `pair` | coder, reviewed by reviewer (up to three rounds) | Yes (the coder) |
| `plan-build-review` | planner → coder, reviewed by reviewer | Yes (the coder) |

```bash
garuda flow run plan-build-review -t "Add a --version flag"
garuda flow show FLOW_ID        # steps, attempts, receipts, outputs
```

`flow show` and `flow resume` take the full flow ID printed at the end of
`flow run`.

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

### What passes between steps

Steps hand each other typed **artifacts** (`plan`, `patch`, `review`,
`findings`, `notes`, `summary`) and nothing else. A step's output becomes an
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

### Reviews

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
verification: to verify, add checks.

By default the reviewer must be **independent**: it may not run as the same
harness and model as the role it reviews, as one of that role's fallbacks, or
as a role it may consult. The decision is recorded with the identities that
*actually* ran (after any fallback, and the roles really consulted) beside the
configured ones. A reviewer that shares one stops the flow with
`flow.review_not_independent`.

### Interruptions

Before each step starts, Garuda records that it is about to run it; after
it ends, it writes an immutable receipt. If Garuda is killed in between, that
step is **quarantined** and never run again automatically. `garuda flow
resume FLOW_ID` continues after the last step that has a receipt; with a
quarantined step it refuses, so you can look at the workspace first.

### Current limitations

- **Native steps.** A native step's output is the summary it passes to
  `task_complete`, so its artifact block must be inside that summary, or the
  flow stops with `flow.output_missing`. A read-only native step can also be
  held back by the completion checker, which asks for evidence a read-only
  step can't produce. The workaround is in
  [Level 5](../use-cases/teams.md#run-a-plan-build-review-flow).
- **Consults in flows.** Flow steps don't offer the `consult` tool yet; use
  consults from `garuda run --role NAME -t "…"`.

## Consults

A role can ask another role one question mid-task. In your `garuda.yaml` a
role lists the roles it may ask:

```yaml
roles:
  coder:   {harness: native, model_id: MODEL_A, consult: [planner]}
  planner: {harness: native, model_id: MODEL_B}
```

`garuda run --role coder -t "…"` then has a `consult` tool whose `target` is limited
to those roles. Each call:

- runs the target role as its own session, **read-only, in a separate snapshot
  of the workspace**: it can't edit your files, run commands or call other
  tools, and the snapshot is checked afterwards (`consult.unexpected_changes`
  withholds the answer);
- is bounded by `consults:` limits (questions per session, timeout, question and
  answer size, child turns), with a deadline from admission to cleanup;
- can't be nested: a consulted role can't consult;
- returns the answer as labelled, untrusted advice. It is not verification;
- uses a durable request ID, so a retried call never asks twice, and leaves a
  receipt without the question or answer text.

A consulted native role keeps its agent's model, instructions, memory and
skills, but uses the consult profile's read-only tools and limits.

**Seeing consults.** A run prints `[garuda] consults: 1 (1 answered)` when it
finishes, and `garuda sessions show` prints a `consults:` line (the rows with
`--json`). Each row has the request ID, asker, target, the identity that ran,
admission, outcome, duration, denied operations and observed changes. Rows hold
identities and counts, never the question or answer. Denied operations are
counted from recorded permission refusals, not from error text.

**External targets** run only inside the Docker read-only confinement
([Safety](safety-and-workspaces.md#read-only-external-harnesses-run-in-docker));
without it the call is refused (`consult.isolation_unavailable`) and nothing
runs on the host.

**External askers.** A role on an external harness could use the same tool
through a small MCP server Garuda starts for that session (`garuda
_consult-mcp`, over a private socket with a per-session token). It is offered
only for an adapter version proven to forward the server, to name it in
permission requests, and to pause its workspace for a snapshot. No installed
adapter has proven the last two, so today the tool is never offered to an
external role (`consult.transport_unsupported`). See
[External harnesses → consult transport](external-harnesses.md#consult-transport).

**When a consult is refused.** Quiescing the asker means "tool calls run one
at a time"; a run with background processes, or one whose workspace keeps
changing while the snapshot is taken, is refused with
`consult.snapshot_unstable` and its admission and slot are returned. A target
whose agent requires a final-output schema or a completion check can't answer
a read-only question and is refused (`consult.target_unavailable`). This
read-only profile is a guardrail, not a sandbox boundary.
