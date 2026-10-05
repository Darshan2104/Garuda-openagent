# Teams and sessions: safe orchestration for Garuda

**Status:** Final implementation contract — capabilities ship only with acceptance evidence

**Date:** 2026-10-01

**Plan:** [Teams and sessions implementation roadmap](../plans/2026-10-01-teams-and-sessions-implementation.md)

**Target base:** `origin/main` at `3456f25`; re-audit against the then-current
main before each set.

**Tracking:** epic [#140](https://github.com/Darshan2104/Garuda-openagent/issues/140);
the configurable harness is epic
[#141](https://github.com/Darshan2104/Garuda-openagent/issues/141).

## Scope

Garuda already owns the runtime boundary around a model or external coding
harness: workspace selection, permissions, context, evidence, sessions and
observability. This design adds task-oriented orchestration without turning
Garuda into a persistent fleet manager.

The useful ideas taken from adjacent orchestration systems are deliberately
small:

- from OpenRig: stable addresses, durable work ownership, explicit readiness,
  and a work ledger distinct from chat;
- from Agentlas OS: immutable execution identity, typed handoff artifacts,
  independent verification provenance, and receipts;
- from Garuda itself: `AgentRuntime`, capability negotiation, leases,
  baselines/deltas, the approval broker, and fail-closed recovery.

Garuda does not adopt tmux driving, terminal scraping, a resident daemon,
global harness reconfiguration, an agent marketplace, or an ontology system.

Version 1 supports local Linux/macOS filesystems with exercised locking,
atomic replacement and process-identity checks. New orchestration operations
refuse on unsupported or network-mounted stores; historical sessions remain
readable. Concurrent hostile processes running as the same OS user are outside
the security boundary. Host permissions are guardrails; external read-only
consults require Docker. Integration publishes a checked commit, not an
automatic rewrite of the user's live checkout. These are release contracts,
not capabilities to guess at runtime.

## Problems to solve

1. Roles, model bindings, runtime selection and permissions are difficult to
   understand as one effective configuration.
2. More than one mutating session cannot safely share a checkout. Generated
   context is also workspace-global rather than session-private.
3. Session references are not consistently project-scoped, and external ACP
   sessions have uneven resume, model-selection, usage and close support.
4. Chat, web chat, SDK conversations and recipes do not all share the same
   lease, baseline, approval and session lifecycle.
5. Garuda has no compact, enforced sequence for planner/coder/reviewer work.
6. Background work has no durable cross-process scheduler or approval channel.
7. Native and ACP runs expose different outcome words and different evidence.

Current-main correctness work remains separate and small: non-editable package
imports, explicit `--runtime native`, lazy harness discovery, project-scoped
`latest`, and stale contributor wording. README commands, documentation command
validation and duplicate-table-row checks already landed and must not be
reimplemented.

## Principles

1. **Add before removing.** Existing commands, `.agent` configuration and
   specialist guides remain supported throughout this work. Removals require a
   later, versioned compatibility proposal.
2. **One lifecycle owner.** Every CLI, SDK and web entry point uses one session
   service for configuration, workspace, lease, baseline, runtime, approvals
   and final evidence.
3. **Evidence is attributed.** A passing self-selected check is useful evidence,
   but is not equivalent to a trusted acceptance contract.
4. **A worktree is not a sandbox.** It separates Git changes. Only Docker-class
   isolation is confinement.
5. **No hidden spend multiplication.** A plain first run stays single-role.
   Multi-role flows show their runtime/model plan and cost status before launch.
6. **Protocol claims are measured.** ACP behavior is enabled per exact adapter
   version and exercised capability, never inferred from a flag alone.
7. **Durable records are append-only or atomically replaced under a lock.**
   Cross-process ambiguity refuses rather than guessing.

## Goals

- `garuda init`, `garuda doctor` and `garuda config show` make configuration and
  setup understandable without hiding provenance.
- Named sessions resolve within a project and can safely use linked worktrees.
- Native and ACP sessions resume when the exact runtime capability supports it;
  otherwise Garuda starts a linked session from a bounded brief and says so.
- Sequential flows enforce step order, artifact handoff, permissions, review
  mechanics and bounded retry.
- Background sessions have a durable queue, liveness, cancellation and
  single durable approval decisions with at-most-once delivery, without a daemon.
- The web dashboard shows session state, evidence, approvals, flows, usage and
  setup health without becoming an authority bypass.
- The dashboard also shows:
  - every conversation, with the models used for each kind of work;
  - each provider's connection and usage-limit status;
  - usage statistics for every harness and model combination.

  Each value is labelled with its source, or shown as `unknown`.

## Non-goals

- A resident daemon, tmux-hosted seats, screen scraping or terminal typing.
- Automatic mid-run runtime switching.
- Reading, copying or proxying vendor credentials.
- Claiming that a reviewer model establishes correctness.
- Enforcing ACP read-only behavior with a worktree alone.
- Removing existing commands, environment variables, `.agent` homes, recipes,
  rigorous mode, evaluation commands or specialist documentation in this epic.
- A general DAG engine. Version 1 flows are ordered steps plus bounded parallel
  review groups.
- Agents messaging, steering, stopping or approving for other live sessions.
  Consults are the only agent-to-agent channel.

## User model

| Concept | User meaning | Runtime representation |
|---|---|---|
| Role | A named runtime, exact model choice and permission ceiling | Resolved role plus provenance and identity digest |
| Flow | Ordered work with declared handoff artifacts and optional review | Parent session and child step receipts |
| Session | One named task, workspace, runtime tenure and evidence trail | Versioned session record, lease, baseline/delta and context |
| Check | A command whose source and code version are known | Verification receipt with authority and exit evidence |

Process liveness, work state, run outcome and verification are separate facts.

## Configuration

### Locations and compatibility

- Existing global and project `.agent` settings, profiles and environment
  variables retain their current resolution rules for legacy fields.
- The additive files are the user file and `<project-root>/garuda.yaml`
  (project proposals). The user file (user authority) is `garuda.yaml` in the
  same directory as `global_settings_path()` (including its existing `.agent`
  versus `.garuda` choice and `GARUDA_GLOBAL_SETTINGS` override). They own only
  the new `defaults.role`, `harnesses`, `roles`, `flows`, `checks`, `sessions`
  and `consults` fields.
  Duplicating legacy model, runtime, permission, endpoint or credential settings
  at the top level produces `config.conflict`; precedence never hides a conflict.
- New selections resolve in this order: explicit CLI selection, trusted project
  selection, user selection, packaged defaults. Authorization is intersected at
every layer; precedence cannot widen a ceiling. Roles and flows are replaced
  as whole named definitions, not assembled from leaves of different layers.
- Legacy CLI runtime/model selectors bypass an implicit `defaults.role` and
  retain their current routing overrides, including `--runtime native`. When
  `--role` or `--flow` is explicit, an incompatible runtime/model flag refuses
  with `config.conflict`; `config show` explains the compatible invocation.
  Existing invocations without role/flow configuration keep their current plan.
- Acceptance checks are accumulated from user configuration, trusted project
  configuration and repeated `--check` arguments. A project cannot remove a user
  check. Identical execution specifications (argv or exact shell text, cwd,
  environment and execution mode) execute once and retain all provenance;
  whitespace edits cannot silently change or combine shell semantics.
- `sessions.keep_days` and provider concurrency/allowlists are user-only.
  Project isolation and role permissions can only tighten the effective legacy
  workspace and permission ceilings. C.1 tests this matrix as a public contract.
- `garuda config migrate` previews additive suggestions and a semantic diff.
  `--write` adds supported fields with a backup; it never translates legacy keys
  into an unsupported new namespace, overwrites conflicting definitions, or
  deletes the old file. Reapplying an identical migration is a no-op.
- No existing configuration home or environment variable is removed here.
- A future proposal may make `~/.garuda/garuda.yaml` canonical only after named
  release versions, rollback instructions and a compatibility window exist.

### Proposed additive schema

The example below shows every field in one block for reference. In practice,
`harnesses`, fallback chains and `sessions.keep_days` live in the user file,
and `checks` usually live in the project file.

```yaml
version: 1

defaults:
  role: coder                 # a plain run remains one runtime invocation

harnesses:                    # user file only
  claude-code:
    allowed_models: [exact-advertised-option-id]
    max_parallel: 2
  codex:
    allowed_models: [exact-advertised-option-id]

roles:
  planner:
    harness: claude-code
    model_id: exact-advertised-option-id
    permissions: smart
    write_policy: no-edits   # host guardrail; ACP readonly requires Docker
  coder:
    harness: codex
    model_id: exact-advertised-option-id
    effort: high              # only where the runtime exposes a proved effort option
    fallback:                 # optional, user file only, evaluated before start
      - {harness: claude-code, model_id: exact-advertised-option-id}
    consult: [reviewer]       # optional, user file only; see Consults
  reviewer:
    harness: claude-code
    model_id: exact-advertised-option-id
    permissions: smart
    write_policy: no-edits

flows:
  build:
    steps:
      - role: planner
        produces: [plan]
      - role: coder
        consumes: [plan]
        produces: [workspace-delta, check-suggestions]
      - role: reviewer
        consumes: [plan, workspace-delta]
        produces: [review-findings]
    review: {by: reviewer, retry: coder, max_rounds: 2}

consults:                      # user grants; projects may only lower these limits
  max_per_session: 5
  timeout_sec: 600
  max_question_chars: 4000
  max_brief_tokens: 2048
  max_answer_chars: 8000
  max_turns: 4                 # native consulted runs
  max_output_tokens: 2048      # native consulted runs

checks:                       # project file; authority comes from where it is defined
  - pytest -q

sessions:
  isolation: auto            # auto | worktree | shared
  keep_days: 30               # user file only
```

Users do not have to write flows. Garuda ships three example flows that run
as they are, or can be copied as a starting point (see
[Example flows](#example-flows)). The `build` flow above shows the full form.

**`harnesses`** is user-file only:

- `allowed_models` lists the exact model ids that roles in any layer may use on
  that harness.
- `max_parallel` is one cross-process ceiling across foreground, background,
  SDK, web, flow and consult invocations, not a queue-only setting. Each running
  runtime consumes a slot; a flow coordinator consumes none. Native capacity is
  keyed by its user-authorized transport/provider binding. Missing limits use
  a packaged finite default of one; raising them requires user authority.

`write_policy: no-edits` is independent of the existing permission enum
(`readonly`, `smart`, `auto`, `yolo`). It intersects the broker's decisions; it
never weakens a permission, network or confinement ceiling. Unknown schema
keys, duplicate YAML keys and invalid or unbounded limits refuse validation.
Use a safe YAML loader without custom object tags. Version 1 permits at most
32 ordered steps, 16 parallel reviewers, 10 coder retries and 100 consults per
root task; lower user/project limits apply. Preflight prints the worst-case
invocation count including retries and consults, not just the happy-path count.

**Check authority comes from where the check is defined, never from the
file:**

| Where the check is defined | Authority |
|---|---|
| The user file | `user-config` |
| A project file, once this user's trust record for it exists | `trusted-project` |
| `--check "<command>"` on the command line | `user-request` |

A configuration file that contains an `authority` key is rejected.

Checks in the user file run in every project. Use them only for commands that
are valid everywhere. Project-specific commands such as `pytest -q` belong in the
project file, which `garuda init --project` writes.

Interactive setup may accept a friendly model name, but it resolves and writes
the exact advertised option id. Runtime configuration never uses substring
matching. If an exact id disappears, the run refuses and shows current options.

`effort` maps to the native reasoning-effort setting. For an ACP harness it maps
to an effort configuration option, but only when the capability spike proved
that option for the exact adapter version; otherwise setting it refuses.

A native role may name an agent definition with `agent: careful-coder`; the
definition supplies its instructions, memory, skills, tools and limits. See
[Agent definitions](2026-10-01-agent-definitions-design.md). ACP roles accept
only the proved subset that design lists.
Native roles consume the frozen agent compiled by H.8, not a separate loader.
An explicit role model/effort and explicit agent declaration must agree; role
selection may override only implicit defaults. Effective parent/operator,
permission, tools, workspace and budget ceilings still intersect. Agent source
digests join the runtime identity. Consult purpose profiles deny hooks, MCP,
network and further delegation; incompatible required agent output/terminal
contracts refuse rather than silently disappearing. ACP projections cannot
inherit native memory/tools defaults or weaken the exact-version/Docker gates.

**Role fallbacks.**

- Only the user file may authorize fallback candidates. A project's replacement
  role may retain an ordered subset or set `fallback: []`; omission removes the
  chain. Reordering or adding candidates refuses.
- Garuda evaluates the list once, before a session starts, and moves to the next
  entry only for that candidate's recorded reason:
  - that candidate's harness/adapter is missing;
  - that candidate's harness is demonstrably logged out;
  - a fresh, account-bound limit window is at 100 %, or a fresh, account-bound
    `limit_reached` event has an explicit future reset time.
- An unknown limit is not a reason to fall back.
- Every candidate must independently satisfy the authorized model, permission,
  confinement and capability contract. Configuration, trust and confinement
  failures refuse; they never trigger a different provider.
- Garuda never falls back after a prompt has been sent.
- The preflight, the session record and the usage ledger show the primary, the
  entry taken and the reason.

### Trust rules

The project layer may narrow authority but never create it.

| Project configuration may | Project configuration may not |
|---|---|
| Refer to user-authorized runtime ids and exact model ids | Introduce harness launch executables, endpoints or credentials |
| Narrow permissions or select stricter isolation | Loosen permissions or confinement |
| Assemble flows from trusted role definitions | Inject role instructions or step prompt templates without hash-based trust |
| Add labels and non-injected descriptions | Put project text into a classifier or system prompt as trusted instructions |
| Recommend file/language routes | Auto-enable task-text routing |

Project-defined checks, setup commands, native model strings and prompt
templates require an interactive trust record keyed by canonical repository
identity, file hash and schema version. Execution uses the exact parsed bytes
whose hash was trusted, not a reopened file. Replacement, symlink changes or
hash changes invalidate trust. Headless execution cannot create trust.

ACP model choice can consume subscription capacity. The user layer therefore
defines allowed model ids and per-harness concurrency ceilings in `harnesses`.
A project may choose only within those sets.

## Setup and cost visibility

`garuda init` detects executable presence, version and documented login status
without reading credential values. Unexpected status-command failures produce
`unknown`, not `logged_out`.

Login status is enabled only for an exact version and trusted manifest whose
official status argv and documented output/exit mapping have been exercised.
Probes use a minimal environment, closed stdin, a timeout and an output bound;
no authentication action or output is persisted. Results cache for at most
60 seconds and are refreshed before a login-based fallback. Undocumented or
changed status behavior remains `unknown`. This deliberately supersedes the
current durable decision that builtin login state stays unknown until a run;
the implementing PR must record that change.

Before writing configuration it shows:

- each proposed role, harness and exact model id;
- whether the model choice is confirmed or still unknown;
- the default single-role behavior;
- optional flows and their number of runtime invocations;
- known cost information or `unknown` where subscription/API pricing cannot be
  computed.

`--install` remains a separate explicit confirmation for each visible argv.
Nothing is downloaded during discovery or a normal run.

**Project checks in one step.** `garuda init --project` inspects the
repository's marker files. It reuses the bounded trait detection of runtime
selection: no shell, no project import, no symlink following. It then proposes
acceptance checks:

| Marker | Proposed check |
|---|---|
| pytest configuration or a `tests/` directory | `pytest -q` |
| `package.json` with a `test` script | `npm test`, `pnpm test` or `yarn test`, from the lockfile |
| `Cargo.toml` | `cargo test` |
| `go.mod` | `go test ./...` |
| a `Makefile` with a `test` target | `make test` |

On confirmation, Garuda writes the checks to the project `garuda.yaml` and
records this user's trust for that file in the same step. Teammates who clone
the repository are asked once before the checks run for them.

**Diagnostics.** Every refusal or setup problem is one
`Diagnostic(code, message, fix)` with a stable code, for example:
`harness.cli_missing`, `harness.adapter_missing`, `harness.logged_out`,
`harness.limit_reached`, `harness.capability_missing`, `config.invalid`,
`config.conflict`, `config.untrusted`, `config.project_narrowed`, `lease.busy`,
`workspace.readonly_unenforced`, `workspace.unexpected_changes`,
`worktree.main_dirty`, `session.tag_unknown`,
`session.cross_project_context_denied`, `verification.no_trusted_check`,
`consult.unauthorized`, `consult.depth_exceeded`, `consult.budget_exhausted`,
`consult.busy`, `consult.capacity_unavailable`, `consult.isolation_unavailable`,
`consult.timeout`, `consult.unexpected_changes` and
`consult.transport_unsupported`. The CLI, `--json` and the dashboard show
the same three fields.

A flow launch prints a preflight summary. Headless automation opts in with an
explicit flow name; `garuda run` never silently changes from one invocation to
three.

## Sessions

### Strict storage and ownership

Session mutations, capacity, leases, queue entries and approvals use one
versioned local storage contract: validated owner-only directories, no-follow
opens, exclusive creation, OS cross-process locking, atomic same-filesystem
replacement and file/directory fsync. Lock failure is an error, never the
existing best-effort unlocked metadata path. Partial/corrupt/future-version
records quarantine the affected operation, preserving bytes for diagnosis.
Lock order is store/capacity transaction, then workspace acquisition outside
that transaction; code never waits on a workspace while holding a store lock.
Owner epochs fence late writes from superseded workers. Expiry is advisory:
only confirmed process/descendant death permits ownership reclamation.

Workspace lease acquisition proves the current creator and refuses an existing
session id. Only its successful issuing store/process may heartbeat or release
the full retained owner/workspace/mode binding; a copied epoch or an inspected
record gives no authority. Forked, changed and duplicate-holder bindings refuse
without writing. Renewal and explicit delegation reuse authority; legitimate
release precedes new acquisition. Descendant cleanup receipts remain separate.


Quarantine is sticky in the shared lease guard: stop renewal and retain
workspace/capacity through later release/finalizer calls. A borrowed step
quarantines its parent and retains its own reservation; parent quarantine also
retains child slots on teardown after revocation. Normal reaped completion
releases normally. This introduces no cleanup override or recovery receipt.

### Identity and project scope

Session metadata adds `name`, opaque `project_id`, role, harness, exact
model id, flow, parent, origin, tagged sessions, worktree, branch and execution
identity digest.

- `project_id` is a versioned, domain-separated HMAC-SHA256 digest of the
  canonical Git common-directory identity (or real workspace path for non-Git
  work). A user-local random key is created atomically with mode `0600` in the
  Garuda home; it is never exported. Linked worktrees and symlink aliases share
  an identity; bare repositories are handled explicitly. Moving a repository
  changes its identity unless an explicit local migration maps it. Losing the
  key refuses identity allocation until recovery; it must not silently generate
  a replacement while existing identities are present.
- Recovery is `garuda doctor --recover-project-ids`. After confirmation it
  first refuses while any owner is live or its liveness is unknown. Under the
  strict store lock it journals a new key epoch, validates stored paths against
  recorded repository identity, previews name collisions, and stages an
  old-id/new-id alias map and migrated metadata. One atomic epoch manifest
  publishes the result; interrupted recovery resumes from its journal rather
  than mixing keys. Missing or replaced repositories stay explicitly unmapped;
  a path alone is not proof of identity. Ledger rows are never rewritten, and
  grouping resolves verified aliases without losing totals. A recovery receipt
  records mapped, unmapped and conflicting ids; conflicts refuse publication.
- The user-facing project path is stored separately in local session metadata.
  Ledgers and aggregate exports contain only the opaque id, never a path,
  user-assigned project label, remote URL or the identity key.
- Names are unique per project. Allocation uses an atomic directory creation
  under the session-store lock; suffix selection cannot race.
- `latest`, names and short prefixes default to the current project. Global
  lookup requires `--all` or an exact full id.

### State model

The session record keeps four orthogonal fields:

| Field | Values |
|---|---|
| Process | `starting`, `live`, `exited`, `missing`, `unknown` |
| Work state | `queued`, `working`, `waiting`, `done`, `stopped` |
| Outcome | unset while active; `completed`, `failed`, `refused`, `cancelled` when terminal |
| Verification | `passed`, `failed`, `unavailable`, `invalidated` |

Every verification receipt also records its authority. UI labels may summarize
these fields, but storage and APIs never collapse them into one ambiguous word.

`crashed` is such a derived label, not a stored value. It means the process is
`exited` or `missing` while the work state is still `working` or `waiting`, and
no outcome is recorded.

### Worktree isolation and integration

`shared` retains today's conflict refusal. `worktree` always creates a linked
worktree. `auto` works in place only when no live mutating owner exists and uses
a worktree otherwise.

Worktree creation:

- allocates a sanitized path and `garuda/<session-id>` branch atomically;
- records source HEAD and dirty-state fingerprint;
- clearly states that source uncommitted changes are not inherited by ordinary
  independent sessions;
- runs trusted setup commands through the permission engine;
- acquires a lease for the exact resolved worktree.

Parallel review and consults need a stable snapshot including tracked
modifications, deletions and untracked non-ignored files. The owning runtime
must be quiescent, with all workspace operations gated during capture. Garuda
uses a temporary Git index and plumbing without changing the source index or
stash. Capture must not execute project hooks, filters or signing commands.
Enumerate NUL-delimited tracked/non-ignored paths, read bounded entries without
following symlinks, hash with `hash-object --no-filters`, and populate the
temporary index directly with recorded path/mode/object ids before `write-tree`
and `commit-tree`. Do not use ordinary `git add -A`, which can run clean filters.
Use a sanitized Git environment with external fsmonitor disabled; verify the
source manifest before and after capture and refuse any observed change.
Version 1 refuses snapshots requiring submodules, sparse checkout, custom Git
filters, non-Git workspaces or a bounded manifest it cannot finish; ordinary
single-session work remains supported. Symlinks remain link entries and are
never followed outside the snapshot. The receipt records source HEAD, snapshot
tree/commit, manifest hash and exclusions (including ignored files). Snapshot
consumers must not assume excluded content was inspected.

`sessions merge`:

1. locks integration and records the requested destination/source commit ids;
2. previews conflicts and prepares an integration commit in an owned temporary
   worktree using hook-free/signing-free Git plumbing; mandatory hook/signing
   requirements refuse rather than being silently bypassed;
3. runs required trusted checks against that exact commit in the existing
   Docker workspace boundary, with no destination checkout, shared Git metadata,
   host credential store or Docker socket mounted. No trusted check or no usable
   confined checker means refusal, not host execution. A check that changes the
   candidate tree invalidates evidence;
4. revalidates both refs and publishes only
   `refs/garuda/integration/<session-id>` with Git's expected-old-id compare and
   swap; conflicting publication refuses;
5. returns the commit, evidence and a quoted
   `git -C <destination> merge --ff-only <commit-id>` application command. It never
   changes the destination branch, HEAD, index or checkout.

Garuda's lock cannot fence unrelated Git clients, and updating a Git ref is not
an atomic update of a live checkout. Therefore automatic checkout application
(`--apply`), integration hooks and signing are explicitly deferred. This bound
also prevents a trusted-but-buggy check from editing the user's destination.
Cleanup removes only owned temporary
resources after confirmed process exit; live or unmerged worktrees require
explicit force confirmation. See the Git [worktree contract](https://git-scm.com/docs/git-worktree)
and [ref transaction contract](https://git-scm.com/docs/git-update-ref).

### Session-private and shared context

Generated `current-task.md` and `handoff.md` move into the session directory.
Only `ContextPackManager` writes them.

**Sharing context by tagging sessions.** A session knows nothing about other
sessions unless the user tags them. There is no automatic cross-session index.

- **How to tag.** Tag an earlier session in the current project, from any
  harness, in either of two ways:
  - `--with <name>` on `run`, `chat` or `resume`, which can be repeated;
  - `@name` inside a task or chat message.
- **What counts as a tag.** An `@name` token is a tag only when it exactly
  matches a session name in the current project, bounded by whitespace,
  punctuation or the start or end of a line. Anything else, such as
  `@pytest.fixture`, stays plain text. Message text never authorizes a
  cross-project lookup, including when it contains a full session id.
- **Cross-project sharing.** This requires `--with-id <full-id>`. Interactive
  use confirms a preview of the source project/session, destination
  harness/model and the fields shared. Headless use additionally requires
  `--allow-cross-project-context`. Neither project configuration nor model/tool
  output can set either option. Ordinary `--with` refuses another project's id
  with `session.cross_project_context_denied`. Tag resolution reads only local
  session records; it cannot dereference artifact paths in another workspace.
- **Preview.** Before sending, Garuda echoes the resolved tags, for
  example `tagged: fix-login (codex · gpt-…)`. An unknown name passed with
  `--with` refuses with `session.tag_unknown`.
- **What is shared.** Each tagged session contributes the same bounded brief
  that brief-fallback resume uses (below):
  - its task, final output, changed files and diff stat;
  - its checks, labelled current or stale;
  - its artifact references.

  Briefs come from Garuda's own records, so a Codex session can be tagged into a
  Claude Code session, and the other way round.
- **Safety.** Briefs are redacted, escaped and source-labelled with session,
  harness, model and code version, and they are sent as data, not
  instructions. Adversarial task or output strings must not escape the data
  envelope.
- **Budget.** Several tags share one total budget. If a brief is trimmed, Garuda
  says which.
- **Where tags show.** The receiving session records which sessions it tagged,
  and the dashboard shows the link from both sides. Cross-project sharing also
  records an immutable receipt with source ids, destination identity, shared
  field names, code fingerprint and confirmation/explicit-flag provenance.
  The receipt contains no copied brief text.

Resume uses the exact workspace and execution identity:

- native sessions use existing restoration and baseline-inheritance checks;
- ACP resume is used only after the exact adapter version successfully exercises
  the negotiated method;
- unsupported or changed ACP capability starts a linked session from a redacted,
  bounded brief and records `resume_mode: brief`;
- changing role/runtime/model creates a linked session rather than rewriting
  the original tenure;
- a live owner refuses resume.

## Background scheduler and approvals

No daemon is required. Every accepted background request starts one worker
process. If capacity is unavailable, that worker persists its queue entry and
waits without owning a workspace lease.

Within one queue store, an enqueue item id binds one scope, user, harness,
session and frozen configuration digest. For current-version records, retrying
that exact binding returns the existing id without changing the document,
durable sequence or FIFO position, including after a claim. A conflicting
retry refuses under the queue lock. Duplicate records for an id refuse retry
and stay available for diagnosis; this does not authorize migration or recovery.

For dispatch-ready work, the scope must match the declared user/harness pair.
Enqueue and ticket construction both check this. A mismatched scope label cannot
give the same pair a second FIFO lane, including through preexisting waiting
records; selection refuses before intent or capacity publication and preserves
the source. Missing session/configuration evidence is never inferred to make
an incomplete allocation dispatch-ready.

Background admission resolves the launch reference through the shared trusted
runtime catalog before storage or spawn, using the canonical runtime id for
capacity and scope while retaining the original reference for downstream
capability resolution.
Workers compare the resolved lane and session with the queued admission and
verify the serialized launch arguments and receipt against its digest before
identity publication or selection. Alias retargeting and changed argument/receipt
records refuse without rebinding. This argument receipt does not freeze all
referenced configuration files or prove runtime descendant cleanup.

Queue mutations match the complete owner (pid, process start identity, process
group and epoch). An omitted owner on heartbeat or release uses only a successful
claim retained by that instance in the claiming process; opening another store
or inheriting it through a fork grants no implicit authority. Workspace requeue
uses the owner from the original claim and cannot release a replacement's slot.

Queue construction and both inspection APIs (`entries()` and `snapshot()`)
write nothing and do not wait for a writer lock. They read the atomically
published document, project historical records in memory and leave missing
stores absent. Storage initialization, permission changes and migration
publication belong only to mutations. Legacy work cannot migrate without
verified user/session/configuration bindings, regardless of owner liveness.
Validated empty queues and fully bound, ordered version 2 waiters can migrate
to version 3 after a durable byte-exact backup. Version 2 claims refuse because
activation history is missing. Nonempty capacity version 1 records refuse
mutation because their reservation origin is missing; empty ones archive before
upgrading to version 2. Publication uses the locked directory descriptor.
Existing archives
must be private regular files matching the source; ambiguity refuses without
overwriting them. Legacy capacity never supplies a configured ceiling.

`QueueStore` provides a cross-process lock on supported local filesystems. Under
that lock, workers:

1. reclaim an expired claim only after the recorded process identity and all
   runtime descendants are proved dead; TTL alone never authorizes takeover;
2. select the oldest eligible entry for that user and harness;
3. journal queue intent, acquire a protected shared slot and commit selection;
4. acquire the workspace lease and start the runtime;
5. release the claim only after the runtime and its descendants are reaped.

Background admission resolves effective `garuda.yaml` roles through shared
agent setup before creating launch state, retaining the selected role and
original harness reference. The worker compares the effective configuration
digest before selection. Dynamic role fallback refuses until a persisted
selection decision is available. Referenced runtime/agent/MCP files and later concurrent
configuration edits remain outside this pre-selection check.

The implemented queue journal orders durable publications across separate files;
it does not make them one atomic transaction. Ordinary capacity callers retain
all queue slots regardless of owner liveness. Ordinary reservations also stay
counted after parent death: their current records do not prove descendant
cleanup. Neither ordinary nor queue admission deletes them to make room, and
matching dead/unknown ordinary owner retries cannot activate. Explicit matched
release remains a coordinator cleanup assertion; receipt-based recovery stays
session-kernel work. Selection grants activation only
to the successful claiming process with frozen user/session/configuration
bindings. Activation is persisted before the runner, including unlimited
harnesses. Recovery reconciles confirmed-dead pre-activation transactions,
fencing captured activation tickets before publishing claim removal and slot
release. The fence retains activation history. Activated or ambiguous dispatch
stays quarantined and is never automatically replayed. Workspace refusal
restores waiting order by the original durable sequence. Direct dispatch also requires the complete unrevoked process-local ticket,
in addition to the claiming instance's owner. Ordinary capacity activation
rechecks adoption under its file lock and after publication before returning
launch authority. Failed release intent cannot resurrect retained claimant
handles or captured tickets; a partially published activation with revoked
authority retains its protected slot for quarantine. The adoption mutex is
never held over filesystem I/O. Full descendant supervision and proof of
cleanup on every terminal path remain D.2 work.

One strict `CapacityStore` owns slots for every entry point. Admission attempts
capacity before workspace ownership, but releases the slot immediately if the
lease is unavailable; no worker holds capacity while waiting for a workspace.
The parent flow's lease capability permits only its own sequential children.
Capacity and lease records bind PID, process start identity and owner epoch,
not PID alone. A missed heartbeat with a live/unknown owner quarantines the
claim; it cannot permit a second mutator or runtime.
Role aliases and model changes share the same canonical authorized runtime/
provider capacity key; they cannot create extra slots. The supervisor journals
launch intent and process-group identity before dispatch. Runtime descendants
must remain in a supervised process group or confined container; escaping or
uninspectable descendants quarantine ownership, not a false successful reap.

Waiting workers retry with bounded jitter and a heartbeat. Unsupported locking
semantics refuse background queueing rather than approximating atomicity.
Cancellation can remove a queued entry without launching its runtime.

Approval requests and responses are stored under a session-owned directory with
mode `0700`; files use `0600`. Each response binds session id, request id,
request digest, nonce, ceiling version, decision and expiry. Creation is
exclusive and no-follow, content is validated before use, the directory is
fsynced, and ceilings are rechecked at consumption time. A replayed, expired,
foreign or malformed response is denied and audited.

TTY and file channels race through one broker transaction. The first valid
answer wins. Background requests deny on timeout. The dashboard is a client of
the broker, never a direct runtime writer.

The durable state machine is `pending -> decided -> delivering -> acknowledged`.
The decision is immutable and delivery is reserved before sending. A crash
after reservation is ambiguous unless the runtime proves an idempotent
acknowledgement; recovery refuses to resend and asks for a new request or linked
session. This promises at most one delivery, not exactly-once external effects.

## Flows and receipts

A flow is a parent session. It holds one workspace lease through an internal
lease capability tied to the parent id, resolved workspace and process identity.
Only child sessions created by that parent can use it.

Each step writes an immutable `StepReceipt` containing:

- parent, step and attempt ids;
- role/config/runtime/model/adapter identity digests;
- permission ceiling and workspace snapshot;
- declared input artifacts and produced artifact references;
- baseline and delta fingerprints;
- process outcome and check evidence;
- bounded usage/cost information.

Artifacts are structured references to session evidence, not copied raw
transcripts. A step cannot start until its required inputs exist and match the
declared producer and workspace version.

Artifacts have versioned type, producer/attempt id, content digest, bounded
size and a session-store-relative path. Admission rejects escaping paths,
symlinks, wrong producers and stale workspace-bound evidence. A durable attempt
intent precedes every launch; immutable terminal receipts follow it. An intent
without a receipt after a crash is an interrupted attempt, never permission to
replay a possibly mutating step. Recovery reconciles processes, workspace and
evidence, then requires explicit retry or linked continuation. It does not
replay completed steps. Retry means a new attempt id, not receipt overwrite.
Garuda, not the model, writes artifacts into the evidence store from validated
structured output. Native and ACP text outputs use a bounded, versioned JSON
artifact envelope; malformed/missing required artifacts stop the step. Producing
a plan/review never requires permission to write into the source workspace.

`review.by` names exactly one declared terminal reviewer step. That step runs
once after the initial coder, and once after each retry. `max_rounds` is the
maximum number of coder retries, excluding the initial attempt: value two
permits three coder attempts and three reviews. Retry recreates all
workspace-bound inputs and records which planning artifacts remain valid.
Missing findings, invalid JSON, exceeded rounds or unresolved required findings
stop the flow; they cannot imply approval.

The review JSON contract controls retry mechanics only. Code validates shape,
severity and bounded rounds, but the product describes the result as
`review_approved` or `review_changes_requested`, never as proof of correctness.
An independence policy can require the reviewer to use a different role,
runtime, model family or vendor; the chosen policy and actual identity are
recorded. Actual resolved identities, including fallbacks and earlier consults,
are checked before reviewer launch. A consulted reviewer is not independent
of that attempt under a policy requiring independence. The packaged examples
promise a second opinion only, not vendor/model-family independence.

### Example flows

Garuda ships three example flows as package data:

- each can be run by name (`garuda run --flow pair -t "…"`);
- each can be printed to copy (`garuda config show --flow pair`);
- a user flow with the same name replaces the example.

| Flow | Steps | Use it for |
|---|---|---|
| `pair` | coder → reviewer (retry the coder once) | Everyday changes with a second opinion |
| `plan-build-review` | planner → coder → reviewer (up to two coder retries) | Larger changes that need a plan first |
| `plan-only` | scout → planner, both `no-edits` | Investigating and planning without touching code |

The examples refer to the roles `scout`, `planner`, `coder` and `reviewer`.
`garuda init` proposes all four. A flow that names a missing role refuses
before launch and lists the roles it needs.

In the examples, the scout, planner and reviewer steps use `no-edits`, not
`readonly`, so the examples run with Claude Code and Codex without Docker.

### No-edits steps

`write_policy: no-edits` is a step and role policy for work that should not change the
workspace, when Docker confinement is not available. It is a guardrail, not
confinement, and the product always labels it that way.

- The approval broker denies the step's file-edit and command-execution
  permission requests. ACP harnesses can still act without asking, so this is
  not enforcement.
- Sequential steps only. A `no-edits` step runs in the flow's workspace after the
  previous step has finished, and never runs in parallel with another step.
- After runtime/descendant exit, Garuda compares a bounded no-follow manifest
  with the baseline: tracked, untracked and ignored workspace files, modes and
  symlink entries, plus repository HEAD/index/ref fingerprints. Session-owned
  scratch outside the workspace is excluded and reported. Unsupported entries,
  incomplete capture or attribution failure refuse; an empty diff is not a
  substitute for evidence. This detects final observed state, not writes outside
  the workspace or transient writes that were undone.
- If observed state changed or the comparison is unavailable, outputs are
  withheld and the flow stops before the next step with
  `workspace.unexpected_changes`. The diff appears in the CLI and the dashboard.
  Garuda reverts nothing, and the user decides whether to keep, discard or
  continue.
- An unchanged workspace is recorded as "no changes detected", never as "no
  changes possible".

`readonly` keeps its permission-engine meaning for native tools (a host
guardrail); external ACP roles additionally require Docker-class confinement.
Use external `readonly` when host writes must be prevented, and `no-edits` only
after accepting that it detects observed changes rather than preventing them.

Every external ACP role marked `readonly` requires Docker-class confinement,
including standalone runs and sequential scouts, planners and reviewers. Source
mounts are read-only, scratch is bounded, and no other host workspace, credential
store or Docker socket is mounted. Privileged container mode is forbidden. The
runtime inside the container must already be usable
through a documented, user-authorized setup. Preflight refuses with
`workspace.readonly_unenforced` if these conditions cannot be proved; it never
launches a host process as a substitute. Native roles use the existing permission
engine; their permission mode is still a guardrail rather than host confinement.

No-edits steps do not automatically execute acceptance checks or bypass their
command ceiling. Their verification stays unavailable unless they reference
separately produced valid evidence for the exact workspace fingerprint. A
flow's permitted coder/check phase owns acceptance execution and the parent
result; a planner/reviewer cannot manufacture a verification pass.

Parallel reviewers additionally use the same immutable snapshot. The confinement
gate must exercise a deliberately writing runtime in real Docker; a fake ACP
server can prove protocol behavior but cannot establish filesystem confinement.

## Consults: agents asking other roles
A consult is a bounded question/answer child session, not a connection to a
live reviewer. It cannot steer, stop, approve for or control another session.

### Authorization and admission

- Only user configuration grants role targets through `consult: [reviewer]`.
  Project replacement roles may retain a subset or remove all targets; omission
  removes the grant. Neither prompts nor tool output can authorize a target.
- The `consults` limits in the schema are user ceilings. Projects can lower
  them, never raise them. Limits apply to the root task and all flow children
  combined, persist through resume/restart, and include failed launched consults.
- Admission validates authenticated asker identity, ancestry/depth, target grant,
  bounded question/brief, remaining count, trust, target resolution (including
  fallback), snapshot support, isolation and shared capacity before launch.
- A durable request id binds the payload digest and child id. Reserving the
  root counter and request is atomic. Duplicate ids return the same result or
  pending status without another launch; changed payloads refuse. There is one
  active consult per root. A concurrent request returns `consult.busy`, not
  an unbounded queue. Consult descendants are denied by the service even if a
  client forges a tool definition; absence of the tool is not the enforcement.

### Snapshot and child boundary

1. Pause the asker at an exercised workspace-operation boundary. Gate all of its
   writes while capturing and checking the stable snapshot manifest. Native
   tool operations share that gate. An ACP asker needs a proved adapter-specific
   quiescence handshake; receiving an MCP request alone is not such proof.
   Without it, ACP consult initiation is unsupported.
2. Build a detached snapshot repository with its **own Git metadata**, no object
   alternates/hardlinks, linked-worktree administrative paths, inherited hooks
   or local Git configuration. Copy only verified snapshot content. Linked
   worktrees share refs/configuration and are not sufficient for this boundary.
   Record exclusions, then let the asker continue independently.
3. Launch the child with `origin: consult`, parent/root/request ids and the
   actual resolved role/runtime/model identity. Its question and bounded brief
   are structured, redacted, source-labelled untrusted data. Escaping prevents
   envelope breakout, not semantic prompt injection; model text has no authority.
4. Native children use a dedicated read-only question/answer profile: scoped
   file/search tools, no shell, edits, custom tools, hooks, network, MCP,
   cross-session tags, approvals or further consults. All permission requests
   outside the profile deny without prompting. The ordinary task-completion
   verifier is not invoked and no task acceptance is claimed.
5. ACP children require the proved Docker read-only contract, regardless of the
   target's ordinary `write_policy`. Only the detached source is mounted
   read-only; bounded scratch is disposable. No caller checkout, Git metadata,
   parent endpoint/token, host credential store or Docker socket is inherited.
   If this exact adapter cannot operate in that environment, return
   `consult.isolation_unavailable`; never substitute a host ACP process.
6. Wait for terminal outcome, close/reap the runtime and descendants, then
   compare the snapshot/metadata manifest. If it changed or cannot be compared,
   withhold the answer and record `consult.unexpected_changes`. Persist the
   receipt/result before releasing capacity and removing owned scratch. Only
   a successful checked result is returned as bounded, labelled advice.

The independent repository protects Git identity, and native scoped tools limit
access; neither is OS confinement. External child isolation is provided by
Docker, not by a worktree or a prompt. A consulted role may be stricter than its
normal permissions, never less restricted.

### Bounded work and recovery

- Capacity uses the same store as all foreground/background runtimes. A consult
  does not wait for a slot its parent or flow holds; no slot means
  `consult.capacity_unavailable` immediately, before dispatch.
- `timeout_sec` is a wall-clock deadline from admission through cleanup, not
  just prompt execution. Native children also have finite turn/output-token
  caps. Answer clipping bounds returned text, not generation cost. ACP token
  budgets are enforced only when exercised adapter support exists; otherwise
  request count and runtime deadline are the available bounds and preflight
  says the cost ceiling is unknown. Unknown cost is not free.
- Parent cancellation propagates to the child. Timeout, crash or cancellation
  never frees a slot/lease while a child may still run. Failed reaping leaves
  the owner quarantined with an operator recovery diagnostic. Confirmed death
  is required before cleanup, takeover or resume.
- Persist an admission intent before launch and a terminal result afterwards.
  An unresolved intent after restart is reconciled, never automatically sent
  again; the caller gets an interrupted-result error. A refunded reservation is
  allowed only when dispatch provably never occurred. This is at-most-once
  dispatch, not a promise of exactly-once model completion.

### Receipts and accounting

A `ConsultReceipt` records request/asker/root/child ids, source turn, role and
actual identity (including fallback), snapshot manifest/tree, admission result,
policy, outcome, diagnostic, answer size, elapsed time, denied operations,
observed changes and references to unique usage events. Raw questions/answers
remain in the child transcript, never the ledger, durable context or receipt.

The caller timeline links the child; flow step receipts list its consults.
Independence rules compare **actual** consulted identities, not just role
aliases, before the later review. Parent totals include descendant events by
unique session/event id once; global totals do not add the parent's rollup again.
Keep `origin: consult` separate from `call_purpose` so summarization and other
inference purposes remain attributable.

### ACP transport and secrets

Native askers receive the built-in tool. ACP askers receive one MCP stdio server
via `session/new.mcpServers` only after G.1 proves MCP forwarding, structured
permission identity and quiescence for the exact adapter version.

- The stdio executable has a trusted absolute path; only Garuda can supply its
  argv/env. It connects to a session-local Unix socket inside a `0700`
  directory using a fresh 256-bit capability token passed only in the narrowly
  scoped server environment. No project-defined executable is accepted.
- Bind socket authority to asker id, process identity and owner epoch. Requests
  carry a unique id, payload digest and replay protection; clients cannot choose
  another session. Resume rotates/revokes tokens and endpoints.
- Register the token for redaction **before** serializing the ACP handshake.
  Protocol tracing, error logs, support bundles and child environments must not
  expose it. It is never persisted. This protects separate sessions/users, not
  a hostile process already running with the same OS privileges.
- Automatic broker allowance requires exercised structured server/tool identity
  and active session request provenance. A display title/name is not proof.
  Other tools retain ordinary policy. If identity, forwarding or quiescence
  cannot be proved, do not expose the ACP consult tool; report
  `consult.transport_unsupported`. Native askers remain usable.

Primary protocol contracts: [ACP stdio](https://agentclientprotocol.com/protocol/v1/transports)
and [MCP session setup](https://agentclientprotocol.com/protocol/v1/session-setup).
MCP forwarding alone proves neither filesystem isolation nor caller quiescence.

## ACP capability contract

Garuda stays on ACP protocol version 1. This design extends Garuda's owned v1
subset with these stable v1 methods and notifications:

- `session/set_config_option`;
- `session/load` and `session/resume`;
- `session/close`;
- `usage_update`.

A move to a different protocol version needs its own approved upgrade. The
capability spike records the exact ACP schema commit, the adapter binary digest
and version, and the observed behavior.
ACP stdio remains UTF-8 newline-delimited JSON-RPC, not the older checkout's
Content-Length framing. Pin fixtures and generated schemas to the recorded v1
commit; do not mix v2 SDK/schema fields into this epic.

Model selection, load/resume, close and usage are independent capabilities.
Support requires an exercised request/response path; an advertised flag alone
is insufficient. Missing support produces a documented fallback or refusal.
Usage and cost remain unknown when an adapter cannot provide them.

Live capability checks are opt-in and budgeted. Session creation can validate
initialization and options without a paid prompt. Resume, usage and completion
claims that require a prompt are enabled only after a separately approved,
minimal live test or remain unverified for that adapter version.

## Verification model

A `VerificationReceipt` records:

- command and normalized argv;
- source authority:
  - `user-config`, `trusted-project` or `user-request`, which are the
    acceptance authorities derived as described under Configuration;
  - `agent-suggested`, for commands the native agent offers to its completion
    gate or an ACP harness lists in its final reply;
- exact workspace tree and dirty fingerprint;
- permission decision, exit status and bounded evidence reference;
- stability repetitions where required.

Only acceptance checks may produce `verification: passed`. Agent-suggested
checks are kept as self-check evidence. They cannot produce that state on their
own, and the UI shows them as a separate badge: "self-check passed" or "self-check
failed".

**Native sessions.** The native completion gate is unchanged. Its outcome is
recorded separately as a completion-gate receipt, including acceptance-contract,
grader and side-effect evidence when present. Agent-proposed commands remain
self-checks; an authoritative user-configured domain grader retains its recorded
authority rather than being relabelled agent-suggested. Configured acceptance
checks run afterward. This epic does not replace the native acceptance contract
or claim that a command alone proves every task requirement.

**When no acceptance check exists.** Verification is `unavailable` with
`verification.no_trusted_check`, whose fix is `garuda init --project` or
`--check "<command>"`.

Legacy records migrate without losing their raw status:

| Legacy record | Outcome | Verification | Self-check |
|---|---|---|---|
| native `success` | `completed` | `unavailable` | `passed` |
| native `failed` | `failed` | `unavailable` | `failed` when the gate rejected, otherwise none |
| ACP `completed` with `verified: false` | `completed` | `unavailable` | none |
| `running` | none (process `unknown`, work `working` until liveness is checked) | `unavailable` | none |

This table defines the new v2 evidence view, not a rewrite of old API meanings.
Existing CLI/SDK/JSON-RPC result fields, exit codes, native `success` and legacy
`verified` semantics remain available through a compatibility projection for
the whole epic. Archived raw values and richer gate evidence are preserved.
New outcome/verification/self-check fields are additive and schema-versioned;
clients opt into their interpretation. Removing the old projection requires a
separate versioned deprecation proposal. The dashboard explains that completion
gate acceptance and trusted-check verification are separate facts.

Changes to test infrastructure do not automatically invalidate all results.
They invalidate checks whose authority or execution semantics they can alter.
Garuda then runs an unaffected trusted check, requests an explicit user check,
or records `invalidated` with the touched surfaces.

Failed checks produce `verification: failed`; they do not rewrite a completed
run into a refusal. Runtime failure and permission refusal remain outcome facts.

## Usage records and limit sources

### Usage ledger

The ledger in the Garuda home (`usage/<YYYY-MM>.jsonl`) is versioned and appended
under the store lock. Record kinds have distinct meanings:

| Kind | Meaning | Aggregation |
|---|---|---|
| `native_model_call` | One inference attempt, including retry and unpriced/failed attempts | Tokens and known cost reported for that attempt; one native call |
| `acp_usage_snapshot` | Adapter-reported context occupancy or cumulative usage/cost | Latest snapshot per segment; never summed as calls or tokens |
| `acp_usage_delta` | Per-turn usage or a delta from a proved cumulative source | Sum only within that exact source's proved accounting semantics; no inferred model-call count |
| `limit_event` | Structured provider limit or quota stop | Count deduplicated events; never count as a call |
| `fallback_start` | Recorded pre-start choice of another authorized candidate | Count the selection once; never count as an inference attempt |

Each record has a stable idempotency key, source kind and adapter/schema identity,
session/runtime segment, turn or call id where available, sequence and observed
time. ACP reports without a stable source id or a persisted originating prompt
id can update snapshots but cannot become per-turn charges. Native attempts
receive distinct call ids before inference. The writer
deduplicates replays under the lock; crash recovery may finish an identified
record but cannot create a second charge. Missing accounting after a crash is
shown as incomplete, never zero-filled. ACP records identify their accumulation
window and units explicitly; context occupancy is not billable input tokens.

For cumulative sources, normalization persists its high-water mark with the
event cursor. Replay, duplicate and out-of-order snapshots cannot create a
second delta. A reported reset starts a new epoch; an unexplained decreasing
counter, missing baseline, reconnect gap or adapter change preserves a snapshot
and marks the delta unavailable. Never clamp a negative delta to zero and call
it complete. Per-turn usage and cumulative usage reporting the same consumption
are alternatives selected by the proved source policy, not additive inputs.

The A.3/A.6 spikes record these semantics for the exact adapter version. Stable
v1 `usage_update` is not synonymous with a billable per-call report; the
[upstream accounting issue](https://github.com/agentclientprotocol/agent-client-protocol/issues/1860)
also documents disagreement over per-turn versus cumulative end-turn usage.
Unproved usage remains a snapshot or `unknown`. Snapshot totals are display
evidence; range aggregates consume only deltas with a proved baseline/window.
The unit-price source and revision are pinned at ingestion, so later pricing
changes cannot retroactively alter historical charges.

As applicable to its kind, a record holds identities and counts only:

- time, project id, session id, flow step and attempt;
- role, flow step, origin and original native `call_purpose` as separate
  dimensions; the UI derives work-type groupings without erasing any of them;
- harness, adapter version and exact model id;
- input, output and cache tokens, plus context used and size when reported;
- known cost or `null`, and its pricing source;
- duration and call outcome when reported, retry identity and fallback reason.
  Unknown values remain `null`; ACP reports do not invent an internal model,
  call duration, model-call count or exact model id from the selected role.

**What it never holds:** task text, prompts, outputs, tool arguments, account
names, raw provider payloads, or paths. `project_id` is the opaque digest above.

**Retention.** The ledger outlives session cleanup (`sessions.keep_days`), so
statistics survive pruned sessions. It keeps 13 months by default.

**Completeness.** Calls that are untagged or unaccounted today, notably
summarizer calls (see the backlog), are tagged before the ledger counts as
complete.

**Per-session metrics are unchanged.** Native sessions keep their existing
`by_model_binding_role` and `by_call_purpose` metrics (`core/metrics.py`). The
ledger is the cross-session source, and its native totals must equal those
metrics for the same session. ACP totals instead reconcile to versioned source
fixtures with hand-calculated expected values and explicit coverage. A ledger
record does not imply that all internal calls of an ACP harness were reported.

### Provider limit status

Subscription windows, such as a 5-hour and a weekly window, appear only from a
source the usage-source spike proved for that exact harness version:

- a vendor-documented status interface of the official CLI; or
- structured limit data the ACP adapter reports during a session.

Each value records its source, adapter/version, observed time, used fraction,
reported reset time and a freshness TTL of at most 60 seconds for automatic
fallback. Without a proved source the window is `unknown`. Garuda never reads
credential or auth files, browser cookies or undocumented endpoints, and never
calls a vendor service with the user's subscription credentials.

Two facts Garuda records itself are always available:

- **Observed use:** sessions, turns, tokens and known cost that Garuda sent
  through each harness in the last 5 hours, 7 days and 30 days. It is labelled
  "through Garuda only", because use outside Garuda draws on the same quota.
- **Limit events:** when a runtime ends with a structured rate-limit or quota
  error, Garuda records `limit_reached` with the reported reset time, if any.

Limits bind to an opaque account/profile identifier only when a documented
interface supplies one. Garuda hashes that identifier locally and never stores
the account name. A login transition, profile/version change or expired TTL
invalidates fallback eligibility. Refresh before selection must confirm the
same account and a current limit; without account identity, freshness or an
explicit future reset, an event is historical display evidence only. It cannot
choose another provider. An unknown reset time never creates an indefinite ban.

**API providers used by native roles.** Garuda records the documented
rate-limit values that LiteLLM exposes (remaining requests or tokens, and reset
time) where available, and nothing else from the response headers.

## Dashboard

The local web dashboard is the only dashboard. Views render the shared read
model; accounting views additionally consume the usage ledger. Session and
approval views ship independently of accounting, with unknown usage shown until
the ledger is available. Every write goes through the existing service/broker.

### Sessions

- process, work state, outcome and verification, shown separately;
- project, session, role, runtime/model identity, workspace and branch;
- queued workers, pending approvals and crash recovery guidance;
- a text filter over name, task and branch, and a toggle for all projects.

### Conversations

Every session in every project has a conversation page with:

- **Timeline.** The full timeline from persisted records:
  - native messages, tool calls and model calls;
  - ACP prompts, agent messages, tool calls, plans and permission requests,
    from the normalized trail.

  There is one lane per runtime tenure (`observability/lanes.py`), so linked and
  fallback sessions read as one story.
- **Model badges.** A badge on every model call or turn shows its harness,
  reported exact model id or labelled selected model, and work type. Missing
  reported identity stays unknown.
- **"Models used" table.** One row per work type, harness and model, with calls,
  input/output/cache tokens, known cost plus an unpriced count, and duration.
  - Native work types are the recorded call purposes: `reasoning`,
    `controller`, `collector`, `classifier`, `critic`, `subagent` and
    `buffer_query`, plus `summarizer` once it is tagged.
  - Flow steps add their step role (planner, coder, reviewer, …).
  - ACP rows distinguish the role's selected model from reported usage
    attribution. A report without model identity is labelled `unattributed`;
    internal calls and their count show as `not reported`. Context occupancy,
    billable usage and cumulative cost snapshots have separate labels.
- **Flow detail.** Flow steps, attempts, artifact edges, deltas and review
  outcomes.
- **Tags.** The sessions this conversation tagged, and the sessions that tagged
  it, each with harness, model and a link.
- **Evidence.** Verification receipts with their authority, the self-check
  badge, and stale or invalidated evidence.
- **Usage and cost.** `unknown` stays `unknown`.

### Providers

One card per harness and per API provider in use, showing:

- **Connection:** login status (`logged_in`, `logged_out` or `unknown`) and when
  it was checked.
- **Versions:** CLI and adapter versions, and whether the capability matrix was
  captured on that exact version. If it was not, the card says so and gives the
  capture command.
- **Capabilities:** model selection, effort, resume, close and usage, each
  `supported`, `declared` or `unknown`.
- **Roles:** the roles bound to this harness or provider, including fallback
  positions.
- **Limits:** limit windows with used fraction, reset time, source and observed
  time, or `unknown`.
- **Observed use:** use through Garuda over 5 hours, 7 days and 30 days.
- **Limit events:** the latest `limit_reached` event, shown as current until
  its reported reset time passes. An event with no reported reset time is shown
  as historical, with when it was observed.

Refresh re-runs the cached doctor probes. It never sends a model prompt.

### Usage

For a chosen range (24 hours, 7 days or 30 days):

- the share of tokens and calls by work type: reasoning against collector, or
  planner against coder against reviewer;
- a table per harness and model: sessions, calls, tokens, known cost and
  unpriced attempts, median reported call duration, limit events and fallback
  starts. Native call counts and ACP reported turns/usage are separate measures;
  unreported ACP calls are never included in the native-call denominator;
- a daily trend per harness and model;
- tables by role and by project;
- CSV or JSON export of these aggregates, which hold counts and identities only.

Statistics are evidence for the user. They are never a routing input. Outcome,
verification and approval detail stays in session/flow views for this version;
the accounting ledger does not infer those facts from model-call outcomes.

### Setup

- doctor results and effective-config provenance, with copyable fix commands;
- the role table, giving each role's harness, exact model id, effort and
  fallback chain;
- the available flows, with the example flows marked and any missing roles
  named.

### Live updates and writes

Server-sent events may replace polling once the read model is stable.

Approve, deny and stop remain capability-token-protected broker and service
operations. Merge and configuration edits stay CLI-only.

## Compatibility and documentation

This epic adds `init`, `doctor`, role/flow selection, richer sessions and
dashboard views without removing current commands. Deprecation messages may be
added only when an equivalent path has shipped and is documented.

The site as of `3456f25` stays as it is: a quickstart, "How Garuda works", the
command builder, five use-case levels, and the guides for safety and
workspaces, configuration, external harnesses and the web dashboard.

This design adds one guide, `guides/sessions-and-flows.md`, covering sessions,
tags, worktrees, the example flows and background work. It is linked from the
"Level 4 · Automate" and "Level 5 · Advanced" use-case pages, and from the
`mkdocs.yml` navigation.

Architecture and contributor docs remain separate. Current behavior stays in
guides/reference; dated design and plan records do not become user reference.

## Delivery boundaries

This is an umbrella design. It ships as seven independently reviewable sets;
their labels identify contracts rather than a strictly serial release order:

1. **Foundation and proof:** current-main corrections plus ACP, scheduler,
   transactional-worktree and usage-source spikes.
2. **Session kernel:** identity, session-local context, all-entry leases,
   worktrees, one session service, resume, session tags and secure approval
   storage.
3. **Configuration and flows:** additive config, trust, setup UX with project
   checks, attributed verification, exact models and effort, typed sequential
   flows with example flows, confined read-only ACP execution and start-time
   fallbacks for missing/logged-out candidates.
4. **Background control:** durable queue/workers, shared read models,
   session/flow views, broker approvals and focused browser validation.
5. **Usage accounting:** idempotent native records, typed ACP snapshots/deltas,
   account-bound limit observations and quota-fallback eligibility. The ledger
   can land after the session service, without waiting for background control.
6. **Dashboard observability:** conversations, provider status, usage aggregates,
   exports and setup views, consuming the earlier records without adding new
   accounting or execution authority.
7. **Consults:** a transport spike, the consult engine for native askers, the
   MCP consult tool for ACP askers, and consult visibility. This set depends on
   strict storage/shared capacity, the session kernel, roles, snapshot capture
   and the dedicated native-read-only or Docker-confined child profile.

Broad removals and config-home migration are explicitly outside these sets.

## Decisions to record in implementing PRs

1. Session state, run outcome and verification are separate public fields.
2. Verification records authority. Agent-suggested checks, from the native gate
   or an ACP harness, are self-checks.
3. Project prompt templates require hash-based trust.
4. Worktrees isolate Git changes but do not enforce ACP read-only behavior.
5. Every external ACP role marked `readonly` requires Docker-class confinement;
   parallel review additionally shares one immutable snapshot.
6. Background queueing uses worker contention over a durable locked store, not
   an unowned queue or resident daemon.
7. Flow handoffs use typed artifact references and immutable step receipts.
8. Existing configuration and command surfaces remain until a later versioned
   deprecation proposal.
9. The usage ledger records identities and counts only. It outlives session
   cleanup, and it is the single source of cross-session statistics.
10. Provider limit windows come only from vendor- or adapter-documented sources,
    proved per harness version; otherwise they are `unknown`. Observed use is
    labelled as covering Garuda only.
11. Role fallbacks are user-configured, evaluated only before a session starts,
    and recorded with their reason.
12. Context crosses sessions only through explicit tags. There is no automatic
    cross-session index.
13. Check authority is derived from where a check is defined; configuration
    cannot declare it.
14. Garuda ships example flows that users can run directly or copy.
15. New configuration has a disjoint namespace and fixed precedence; trust and
    permission ceilings compose by intersection, never by last-writer wins.
16. Project ids in accounting and exports are opaque user-local digests; paths
    stay in local session metadata.
17. Cross-project context requires a distinct explicit request and a sharing
    receipt; free-form message text cannot authorize it.
18. Ledger kinds separate inference attempts, ACP snapshots/deltas, limit events
    and fallback starts; ingestion is idempotent and preserves unknown coverage.
19. Automatic quota fallback requires fresh, version- and account-bound evidence
    with a future reset. Unbound events are display evidence only.
20. Documented, exercised official login-status probes may replace the existing
    unknown-until-run behavior; authentication remains user-driven.
21. Versioned richer session evidence is additive. Legacy result projections and
    native gate/grader evidence remain until a separate deprecation proposal.
22. `write_policy: no-edits` is a labelled guardrail: broker denials plus post-step change
    detection that stops the flow without reverting. It never claims
    confinement; native `readonly` is also a host guardrail, while external
    `readonly` requires Docker.
23. Consults are the only agent-to-agent channel. They are:
    - user-authorized per role, and one level deep;
    - run on an independent immutable snapshot with scoped native read tools
      or mandatory Docker confinement for external children;
    - unable to create approval prompts;
    - bounded by count, time and capacity rules that cannot deadlock.

    Their answers return as data. Agents never control another live session.
24. One strict storage/capacity contract covers every entry point. TTL alone
    never steals ownership from a live or unknown process.
25. Approval decisions are single-winner; delivery and consult dispatch are
    at-most-once, with explicit interrupted-state recovery.
26. Integration publishes a checked commit under a Garuda-owned ref. Automatic
    application to a live checkout is deferred.
27. Recovery publishes one identity-key epoch and alias manifest atomically;
    append-only usage records are not rekeyed or rewritten.

## Acceptance boundary

The feature is not complete merely because fake runtimes pass. Completion
requires cross-process race tests, transactional Git tests, adversarial approval
and prompt-data tests, an opt-in real-adapter capability report, and browser
checks of the same persisted records exposed by CLI and SDK surfaces. Usage
statistics additionally require native ledger totals to equal per-session
metrics, ACP accounting to match independent source fixtures, and every limit
value to carry a proved source or render `unknown`. Filesystem confinement claims
require the live Docker gate; unavailable integrations leave those capabilities
disabled rather than counting a fake test as proof.
