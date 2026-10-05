# Teams and sessions implementation roadmap

**Status:** Final execution roadmap — dependency-ordered PRs with release evidence

**Date:** 2026-10-01

**Design:** [Teams and sessions: safe orchestration for Garuda](../design/2026-10-01-teams-and-sessions-design.md)

**Related roadmap:** [Agent definitions](2026-10-01-agent-definitions-implementation.md)
(Set H: the configurable native harness that roles run)

**Target base:** `origin/main` at `3456f25`

**Tracking:** epic [#140](https://github.com/Darshan2104/Garuda-openagent/issues/140).
Task issues: A.1 #151, A.2 #145/#147/#148/#149, A.3 #152, A.4 #153, A.5 #154,
A.6 #155, G.1 #156; set trackers B #157, C #158, D #167, E #168, F #169, G #170.

## Objective

Add safe, task-oriented teams and durable sessions to Garuda while preserving
its existing runtime, configuration, CLI and documentation contracts.

The delivery is seven independently shippable sets:

1. foundation and proof;
2. session kernel;
3. additive configuration and flows;
4. background control and session views;
5. usage accounting and provider-limit records;
6. dashboard observability and exports;
7. consults between roles.

Each set receives a fresh current-main audit before its first pull request.
Broad command removal and configuration-home migration are not part of this
roadmap.

## Delivery rules

- Branch every task from the then-current `origin/main`.
- Keep one behavioral contract per pull request. Pair tasks only where neither
  half can be safely shipped alone.
- Preserve existing behavior when the new configuration or feature is absent.
- Put shared cross-entry-point orchestration in a focused service, not copied
  into CLI, SDK and web handlers.
- External coding harnesses remain behind `AgentRuntime`; they never become
  `Model` implementations.
- Fail closed on configuration authority, workspace identity, locking,
  isolation, approval, protocol capability and verification ambiguity.
- Never call a worktree, permission mode or macOS Seatbelt a sandbox.
- Never read, copy, persist or proxy vendor credentials.
- Record each accepted public/security boundary in `.context/decisions.md` in
  the pull request that makes it true.
- Update user guides, CLI reference, architecture, module map and backlog in the
  same pull request as the behavior they describe.

### Test authoring gate

Every proposed test must identify:

1. the observable behavior or independent contract it protects;
2. a credible regression that makes it fail;
3. why an existing owner-boundary test does not already protect it;
4. whether it requires a test-only production seam.

Tests that merely spy on a private call, copy a capability flag, grep an
implementation symbol, or use a mock that performs the asserted behavior do
not satisfy the gate. Prefer the public CLI/SDK/protocol/storage boundary that
owns the contract. A regression test must fail against the pre-fix behavior for
the intended reason.

## Dependency map

```text
Set A: current-main audit + four proof spikes
   ├── ACP exercised capability matrix ───────────────┐
   ├── durable queue/lock proof ──────────────────────┤
   ├── transactional Git proof ───────────────────────┤
   └── usage and limit source proof ──────────────────┤
                                                       v
Set B: strict store/shared capacity -> identity -> state -> context -> all-entry leases
       -> worktrees/integration -> session service -> resume/tags -> approvals
                                                       |
                                                       v
Set C: additive config -> trust/provenance -> exact role resolution -> setup
       -> C.8a confined ACP roles -> attributed verification -> no-edits policy
       -> typed flows/review
       -> start-time fallbacks (missing/logged-out candidates)
                                                       |
                                                       v
Set D: queue/workers -> shared read model -> session/flow views
       -> approval inbox -> focused browser acceptance

Early lane: A.3 + A.6 + B.6 -> E.1 usage ledger
            E.1 + C.4 -> E.2 account-bound limits
            E.2 + C.9 -> quota-fallback eligibility

Set F: C + D.3 + E -> conversations / providers / usage / setup views
       -> aggregate exports -> browser acceptance

Set G: G.1 transport spike (can run with Set A)
       B.0 + B.5 + B.6 + B.7 + C.1-C.3 + C.9 -> G.2 native consult engine
       G.2 + C.8a -> G.2 external-child integration
       G.1 + G.2 -> G.3 MCP consult tool (ACP askers)
       G.2 + D.3 + E.1 + F.1 -> G.4 consult visibility

Set H (separate roadmap): H.0 defect fixes ship independently;
       H.8 (including H.12) + C.1 + C.3 -> H.10 native agent bridge
       H.3 trust/authority and H.7 guard hardening ship independently of H.1
```

E.1 lands after the session service owns identity and inference recording, and
can proceed alongside C/D. E.2 adds limit observations after exercised doctor
probes exist. C.9 ships missing/logged-out fallbacks independently; quota-based
fallback is enabled by a later integration PR once E.2 satisfies its gate.
F consumes those records and cannot block CLI session or background releases.

The ACP, queue, Git and usage-source spikes are gates. If a spike cannot prove a
required contract, disable that capability and amend the design before shipping
it. Native-only paths and other independently proved capabilities may proceed.

### Canonical PR order

This table is the scheduling authority; task numbers are identifiers, not the
execution order. Each comma-separated task is its own focused PR unless the
delivery rule requires an inseparable contract. Re-audit the current main before
each PR, carry forward accepted evidence, and do not reimplement already-shipped
fixes. The lanes can overlap only once their listed prerequisites have merged.

| Lane | PR sequence | Required merged prerequisites |
|---|---|---|
| Foundation | A.1, A.2; A.3, A.4, A.5, A.6; G.1 | A.1 before spikes; G.1 shares A.3 captures |
| Ownership | B.0, B.1, B.2, B.3, B.4 | A.4; each preceding ownership task |
| Session lifecycle | B.5, B.6, B.7, B.8 | A.3, A.5 and ownership; preceding lifecycle task |
| Configuration | C.1, C.2, C.3, C.4, C.5 | B.6/B.7; preceding configuration task |
| Host guardrails | C.10 | B.5/B.6 and C.1–C.3 |
| Sequential flows | C.6a, C.7, C.6b | C.5/C.10; preceding flow task |
| External confinement | C.8a | A.3, B.5/B.6, C.3; real Docker evidence |
| Parallel review | C.8b | C.6a, C.7 and C.8a |
| Start-time fallback | C.9 | C.3/C.4; confinement for any external readonly candidate |
| Background/read model | D.1, D.2, D.3, D.4, D.5, D.6 | B.0–B.8 and C.3; C.6b for flow views; prior D task |
| Accounting | E.1, E.2 | A.3/A.6/B.6 for E.1; E.1/C.4 for E.2 |
| Quota fallback integration | C.9 quota-enabling PR | C.9 and E.2 freshness/account/reset gates |
| Dashboard | F.1, F.2, F.3, F.4, F.5 | C, D.3 and E; prior F task |
| Native consults | G.2 | B.0/B.5–B.7, C.1–C.3 and C.9 |
| External consulted children | G.2 external-child PR | G.2 and C.8a; real isolated-runtime evidence |
| ACP consult initiation | G.3 | G.1 and G.2; every exact-version transport gate |
| Consult observability | G.4 | G.2, D.3, E.1 and F.1 |

No cycle exists between C.6a's engine, C.7's review mechanics and C.6b's
examples. C.8a does not depend on the parallel engine. Shared capacity is in
B.0, so consults do not secretly depend on D.1's scheduler. F consumes records;
it never gates native CLI/background delivery. Native-only consult delivery
does not require external confinement or ACP MCP support.

### Release evidence and completion

Every PR records its contract, focused tests, compatibility results and exact
live integrations run/not run. Do not mark an unsupported adapter path complete
because a fake passed. A capability can be deliberately disabled, but its
implementation task remains deferred with the missing evidence named.

| Contract | Required independent evidence | Refusal/recovery behavior |
|---|---|---|
| Storage, capacity and leases | Real cross-process contention, PID reuse and crash/fsync cases | No unlocked writes or live/unknown takeover |
| Git snapshots/integration | Real repositories, hostile metadata/filter cases, ref race | Refuse incomplete snapshots; publish only owned ref |
| Configuration/authority | Golden resolver plans plus trust replacement races | Refuse widening, stale trust or ambiguous identity |
| Flow/approval/consult recovery | Kill at intent, dispatch and terminal persistence boundaries | Never auto-replay uncertain edits, deliveries or dispatch |
| External readonly/consult children | Real Docker writer and host sentinel | No host substitution if isolation is unproved |
| ACP capabilities/consult initiation | Exact schema/binary captures, structured identity and quiescence | Unsupported method/tool remains disabled |
| Accounting and limits | Independent source totals, replay epochs and account freshness | Unknown is neither zero nor fallback authority |
| Dashboard and secrets | Production-written records, live Chrome, log/export token scans | No UI authority bypass, leaked token or duplicated rollup |

## Set A — foundation and proof

### A.1 Rebase, relocate and audit the proposal

**Files:** this design and roadmap, `docs/design/index.md`, `docs/plans/index.md`,
`mkdocs.yml`.

**Change:**

- Rebase the documentation work onto current `origin/main`.
- Move the design to `docs/design/` and this roadmap to `docs/plans/`.
- Add both records to their indexes and to the "Design records" and
  "Implementation plans" sections of the `mkdocs.yml` navigation. CI runs
  `mkdocs build --strict`.
- Check every guide name this roadmap uses against the restructured site. As of
  `3456f25`, there is no `using-garuda.md`, everyday-use guide or feature map;
  the site has a quickstart, `how-garuda-works.md`, `command-builder.md`, the
  `use-cases/` levels and four specialist guides.
- Re-audit every source path, line reference, backlog claim and already-landed
  documentation fix.
- Remove completed work rather than retaining duplicate tasks. At the current
  base, README command syntax, command parsing checks and duplicate table-row
  detection have already landed.

**Acceptance:** documentation-contract checks and `mkdocs build --strict` pass,
and no maintained link points back to the draft location.

### A.2 Fix current-main correctness defects

These are separate small pull requests because they do not depend on the new
architecture:

Reproduce each defect against the refreshed main first. Already-fixed items
are removed from the work list, with the owning regression test referenced;
do not turn a stale audit into another implementation.

1. A non-editable install imports transport configuration without requiring the
   repository's `tests/` directory. Full transport admission remains a CI-owned
   contract.
2. Explicit `garuda run --runtime native` wins over routing and classification.
3. Native default runs do not execute ACP discovery probes. Discovery is lazy,
   bounded and cached without storing probe output.
4. `latest` resolves in the current project unless global lookup is explicit.
5. Contributor documentation describes `AgentRuntime` as shipped, not planned.

**Owner-boundary tests:** installed-package subprocess import; CLI selection
through the public parser/runner boundary; subprocess-probe observation;
two-repository session resolution. Do not use source greps as primary proof.

### A.3 ACP exercised-capability spike

**Files:** an opt-in capture script, versioned fixtures under
`tests/fixtures/acp/`, ACP protocol notes in the external-harness guide.

For Claude Code and Codex adapters that are installed locally, record:

- ACP schema commit and protocol version;
- adapter path digest and reported version;
- initialize and `session/new` responses;
- exact model option ids;
- effort or reasoning-level option ids, if any are advertised;
- exercised model-selection and effort-selection results;
- exercised load/resume and close result where possible;
- whether real usage was observed or remains unknown, separating stable
  `usage_update` context/cost snapshots from optional end-turn token usage;
- units, accumulation window, model attribution, replay behavior and reset
  semantics for every accounting source. Link the exact schema and adapter
  documentation; presence of a notification alone does not prove accounting.

Session creation and option capture send no prompt. Any check requiring a model
turn is separately opt-in, explicitly budgeted and runs the smallest meaningful
prompt. CI uses the fake server for deterministic protocol behavior but never
converts fake support into a vendor-support claim.

**Gate:** update the capability table and fallback behavior from observed facts.
An advertised but unexercised method is recorded `declared`, not `supported`.

### A.4 Durable queue and lock spike

**Files:** a prototype `QueueStore` and focused cross-process tests in temporary
directories. No CLI wiring.

Prove:

- mutually exclusive cross-process claim on every supported OS/filesystem;
- FIFO selection within one user and harness;
- stale-claim cleanup only after expiry and confirmed owner/descendant death;
  PID reuse, a live owner past TTL and unknown liveness never permit takeover;
- cancellation of a queued item;
- a waiting process eventually claims a released slot;
- unsupported locking fails closed, including the existing best-effort metadata
  lock path; a lock failure cannot fall through into an unlocked mutation;
- cross-entry capacity reservations obey one finite ceiling; unavailable
  workspace ownership releases reservations rather than causing lock inversion.

**Gate:** demonstrate the wake/contend mechanism with real subprocesses. A
single-process fake queue is insufficient.

### A.5 Snapshot and integration-publication spike

**Files:** a prototype under `garuda/workspace/` and tests using real temporary
Git repositories. No session CLI wiring.

Prove:

- stable, bounded snapshot capture includes tracked edits, deletions and
  untracked non-ignored files without changing the source index or stash,
  executing hooks/filters/signing, or following escaping symlinks; unsupported
  submodules/sparse/filter/non-Git cases refuse explicitly;
- a detached consult repository has independent Git metadata with no linked
  administrative paths, hardlinked objects or alternates;
- use NUL-delimited manifest entries, no-follow bounded reads,
  `hash-object --no-filters` and direct temporary-index entries, not `git add`
  (which may run clean filters). Sanitize Git environment/fsmonitor and refuse
  changes between pre/post capture manifests;
- concurrent name/branch allocation has one winner;
- an integration preview detects content conflicts;
- destination/source ref changes, mandatory hook/signing requirement refusal
  and failed or tree-mutating checks do not modify the destination checkout/ref;
- checks run in the existing Docker workspace boundary without caller checkout,
  shared Git metadata, host credentials or socket mounts. A deliberately writing
  checker cannot touch a host sentinel; unconfined/missing checks refuse;
- successful publication uses expected-old-id compare and swap on only the
  Garuda integration ref, returning a manual application command;
- live and unmerged worktrees are not cleaned accidentally.

**Gate:** destination HEAD, branch, index and checkout remain byte/ref-identical
on success and failure. Snapshot/publication uses hook-free/signing-free Git
plumbing; automatic checkout application, hooks and signing are not part of v1.
Publication's live Docker checker gate is separate from snapshot/worktree proof,
so unavailable Docker does not block ordinary worktrees.

### A.6 Usage and limit source spike

**Files:** an opt-in capture script, fixtures under `tests/fixtures/usage/`, and
notes in the external-harness guide.

For each installed harness (Claude Code and Codex first), find and record:

- whether the official CLI documents a status interface that reports plan usage
  windows (used fraction, window length, reset time), with its exact argv and
  output schema;
- whether the ACP adapter reports structured limit data or rate-limit errors
  during a session, and their exact shape;
- what a rate-limit or quota stop looks like on the wire: stop reason, error
  code and reset field;
- for each API provider used by native roles, which rate-limit response values
  LiteLLM exposes;
- whether a documented status interface supplies an opaque account/profile id
  suitable for binding limits without reading credentials;
- how login/profile/version changes invalidate observations, and whether a
  future reset time and fresh same-account status can be independently proved;
- the source policy that selects per-turn usage versus cumulative deltas so
  two reports of the same consumption are not summed.

**Rules for adopting a source:**

- It must be documented by the vendor or the adapter.
- It must be exercised against the exact installed version without sending a
  paid prompt. A limit error is captured from recorded fixtures, or from a
  separately approved minimal live test.
- Reading auth files, browser cookies or undocumented endpoints is out of scope.
- Fixtures preserve normalized counts, shapes and opaque identifiers only;
  redact account names, paths and provider payloads before committing captures.

**Gate:** each harness version's limit windows and accounting semantics are
recorded as `supported`, `declared` or `unknown`. F.2 renders only proved values.
Unknown/account-unbound limits cannot enable quota fallback. Captures are shared
with A.3 rather than issuing another prompt to reproduce the same evidence.

## Set B — session kernel

### B.0 Strict storage and shared capacity primitives

**Dependencies:** A.4. **Files:** focused storage/ownership modules and real
subprocess tests. Do not embed a second scheduler in each interface.

Promote the proved owner-only, no-follow, locked, atomic/fsynced storage contract.
Add versioned owner epochs, PID/start identity, descendant liveness and finite
capacity reservations shared by every runtime launch. Historical reads remain
compatible; new mutations refuse unsupported filesystems, lock failures,
partial/corrupt/future records and ambiguous owners. Recovery quarantines rather
than stealing live/unknown ownership. Keep critical sections short; release a
slot when a workspace cannot be acquired instead of waiting with it held.
Canonical runtime/provider keys do not split by role alias or selected model.
Journal launch intents and supervised process-group/container identity before
dispatch so worker death cannot orphan an uncounted live runtime.

**Acceptance:** real foreground/background/SDK/flow/consult-style subprocess
contenders never exceed a configured ceiling. Expired-but-live, PID-reused,
unknown-descendant and late-owner writes cannot reclaim or alter another owner.
Crash at each persistence boundary leaves one recoverable committed version.

### B.1 Project identity and atomic session names

**Files:** `garuda/core/sessions.py`, a focused project-identity module, session
migration code and storage tests.

Add meta v2 fields from the design. Normalize linked worktrees to one project
identity while preserving the visible workspace path only in local metadata.
Generate the versioned, domain-separated HMAC project id using the atomic,
user-local `0600` key specified in the design. Handle bare repositories, non-Git
workspaces and symlink aliases explicitly. Allocate names with an atomic
directory operation under the session-store lock.
Store canonical path and filesystem identity locally at first allocation, so
recovery cannot treat a replacement repository at the same path as the original.

Legacy sessions load without rewriting until a mutation requires migration.
Unknown future schema versions fail actionably.

**Acceptance:** concurrent subprocesses cannot receive the same project/name or
create different identity keys; worktrees and symlink aliases share a project;
project-scoped `latest` cannot cross repositories; ids and aggregate exports
contain no source path or key. A missing key with existing identities refuses
allocation and offers recovery instead of silently changing project scope.
`garuda doctor --recover-project-ids` uses the design's offline, journaled key
epoch/alias migration. Test crashes before and after manifest publication,
missing/replaced paths, name collisions and live/unknown owners. Repeated
recovery is idempotent; ledger bytes and totals remain unchanged, verified alias
grouping is restored and unverifiable projects are explicitly unmapped.

### B.2 Separate process, work, outcome and verification state

**Files:** `garuda/runtime/session.py`, `garuda/core/sessions.py`, migration and
read-model tests.

Add the four orthogonal fields defined by the design, plus the self-check
result. Migrate existing `running`, `success`, `failed`, `completed` and
`verified` data using the design's legacy-record table. Do not erase the
original raw status during migration. `crashed` is computed by the read model;
it is never stored.

Preserve legacy result fields, exit codes and `success`/`verified` semantics
through a compatibility projection. Richer fields have an explicit v2 schema;
the old projection remains for the whole epic. Preserve completion-gate receipts
and authoritative grader provenance, not just the legacy boolean.

**Acceptance:** state transitions reject impossible combinations, and CLI/SDK
serialization round-trips every state without flattening it. Golden legacy
records produce the same old CLI/SDK/JSON-RPC results and exit codes while
exposing the separate v2 evidence view. Active sessions have no terminal outcome.

### B.3 Move generated context into the session store

**Files:** `garuda/context/pack.py`, runner/session wiring and context tests.

Make the session directory the only generated-pack target. Preserve committed
durable project context as read-only input. Migration reads an old workspace
pack only when it is explicitly associated with the resumed session; it never
deletes it.

**Acceptance:** two sessions in one repository write distinct packs, and no run
creates or updates generated `.context/current-task.md` or `handoff.md` in the
workspace.

### B.4 Apply leases and baselines to every mutating entry point

Recovery-facing global/session lease inspection includes every retained holder,
including an expired lease with a confirmed dead parent. Ordinary records have
no complete descendant-cleanup receipt. Runtime recovery refuses before session
changes, and project-key recovery refuses before staging or publishing a new
key. `garuda doctor` continues to show the retained stale ownership. Parent death
or waiting out the TTL does not remove this refusal; complete supervised cleanup
and recovery receipts remain separate work.

Borrowed delegation/guard creation and cached acquisition/start/race revalidate
the parent's current issued binding under the lease lock without renewal.
Exercise release followed by a replacement editor, missing/changed records,
unknown identity and real fork inheritance, with an issuer-positive control
that leaves parent lease bytes unchanged. This pre-use admission check adds no
already-running descendant supervision or cleanup/recovery receipts.

Ordinary workspace holders remain recorded after parent death/TTL expiry: the
lease has no complete descendant-cleanup receipt. Read-only registration must
preserve prior bindings. Prove this with a real surviving writer in another
process group; new mutators refuse and read-only registration cannot enable a
later takeover. Keep historical reads and legitimate issuing-owner release/new
acquisition, with no automatic stale takeover or invented recovery override.

Workspace ownership also requires the successful issuing store/process's full
PID/start identity/group/epoch, workspace and mode. Prove the current creator
before publication; refuse existing-session acquisition, copied epochs, unowned
instances, fork inheritance, mismatched bindings and duplicate holders without
changing bytes. Use real live writers and real fork inheritance, plus successful
issuer renewal/release and release-before-reacquisition controls. These checks
add no descendant-cleanup receipt or automatic recovery protocol.

**Files:** CLI chat, web live chat, SDK conversation, recipe runner and shared
run guard.

Every persisted mutating session acquires one lease, starts a heartbeat,
captures its baseline before a prompt, and releases on success, refusal,
cancellation and exception. Read-only native work receives a read-only lease.
ACP permission labels do not waive the mutating lease because they are not an
enforcement boundary.

**Acceptance:** public boundary tests launch competing CLI/SDK/web operations
against one temporary workspace and observe the same conflict and cleanup
semantics. TTL cannot evict a live owner. No terminal path releases ownership
before descendants are reaped; uncertain liveness quarantines the owner.
Do not assert which private helper each interface called.

### B.5 Production worktree and integration service

Promote the proven A.5 mechanisms into `garuda/workspace/worktrees.py`.
Implement `auto`, `worktree` and `shared`, setup-command trust, integration
preview/publication, project integration locking and cleanup. Never update the
user's destination branch/index/checkout; emit the checked commit and manual Git
application command. Consult snapshots have independent repositories, not
linked worktrees sharing refs/configuration.
Publication reuses the existing Docker workspace boundary for required checks;
it does not depend on the later ACP-specific C.8a service. No trusted check,
usable confined checker or unsupported mandatory hooks/signing means no
publication. Do not run a host check as a substitute.

**Acceptance:** real Git behavior matches A.5 through the session-facing API;
baseline and delta bind to the chosen worktree; worktree output never implies
host confinement.

### B.6 Shared session service

Quarantine must retain both workspace and runtime reservations through later
release/finalizer calls. Latch it before stopping renewal; propagate borrowed
step quarantine to the parent and retain child slots on parent quarantine,
including cleanup after revocation. Prove retention with real separately grouped
writers and capacity/workspace contenders, plus ordinary reaped completion.
Supervised cleanup/recovery receipts and parent-death workspace expiry remain
separate work.

**Files:** new `garuda/interfaces/session_service.py`, existing entry points and
contract tests.

One service owns this ordered lifecycle:

1. resolve effective configuration and role;
2. allocate session identity;
3. select or create workspace;
4. reserve shared capacity, acquire lease and baseline; release the slot if
   workspace acquisition cannot succeed immediately;
5. install the approval broker;
6. launch native or ACP runtime;
7. capture terminal outcome, delta and evidence;
8. close/reap runtime and descendants, then release ownership/capacity; quarantine
   an owner whose death cannot be established.

Move `run`, chat, SDK and web paths onto it incrementally. A route is complete
only when its externally visible lifecycle matches; a monkeypatch spy on the
service is not acceptance evidence.

### B.7 Resume, linked continuation, session tags and bounded briefs

**Files:** new `garuda/context/brief.py`, session service, CLI resume surface,
tag resolution, and resume and tag tests.

Briefs include only redacted task/state/artifact/diff/check references and last
bounded final output. Every check records its code fingerprint and renders stale
when the workspace differs. ACP sessions have no state card; their brief is
built from the task, final output, delta, checks and artifacts.

Implement native resume, exercised ACP resume, brief fallback and `--as` linked
continuation. Persist `resume_mode` and exact runtime identity.

**Session tags** replace any automatic cross-session index:

- `--with <name>` can be repeated on `run`, `chat` and `resume`.
- `@name` in a task or chat message is a tag only on an exact match with a
  session name in the current project, at a token boundary. A full id in message
  text never authorizes another project's lookup.
- Cross-project sharing requires `--with-id <full-id>` and an interactive
  destination/field preview confirmation, or additionally
  `--allow-cross-project-context` for headless use. Project configuration and
  model/tool output cannot grant this permission.
- Resolved tags are echoed before sending. An unknown `--with` name refuses with
  `session.tag_unknown`.
- Each tag's brief is attached as escaped, source-labelled data within one
  shared budget, and trimming is reported.
- The receiving session records its tags. The tagged session's read model lists
  who tagged it. Cross-project sharing writes the design's immutable audit
  receipt without copying brief text or dereferencing another workspace's files.

**Acceptance:**

- A new process resumes a capable fake ACP session.
- Adapter drift selects the brief fallback.
- A live owner refuses.
- Inherited checks never become current verification evidence.
- A Codex-session brief tagged into a Claude Code session (fake runtimes)
  arrives as labelled data.
- `@pytest.fixture` and an `@` token that names no session are left as text.
- An adversarial final output cannot break out of the data envelope.
- Several tags stay within the budget, and trimming is reported.
- Another project's full id in `@` text stays plain text; ordinary `--with`
  refuses it, and headless `--with-id` without the explicit grant refuses before
  any prompt. Confirmed/explicit cross-project sharing records the destination
  identity and field names. A forged project setting cannot authorize sharing.

### B.8 Secure file-backed approval channel

**Files:** `garuda/acp/broker.py`, session run guard and approval security tests.

Implement the request/response binding, permissions, no-follow creation,
exclusive decision, durability, expiry, replay protection and ceiling
revalidation from the design. TTY and file answers meet at one broker
transaction.

**Acceptance:** adversarial tests cover replay, foreign session/request,
modified request, symlink target, expired answer, two writers, ceiling change,
partial file and crash before directory sync. The runtime receives at most one
valid answer. Cover crash between durable delivery reservation and runtime
acknowledgement: without proved idempotent acknowledgement, recovery cannot
resend. A single durable decision is not exactly-once runtime execution.

## Set C — additive configuration and flows

### C.1 Additive `garuda.yaml` schema and round-trip

**Files:** a focused parser/model module and configuration tests.

Implement versioned roles, ordered flows, typed step artifacts, checks, the
user-only `harnesses` block (`allowed_models`, `max_parallel`), session settings,
role consult grants and bounded `consults` limits. Model `write_policy: no-edits`
separately from the existing permission enum. Unknown/duplicate keys, invalid
limits and unsupported combinations fail with their full path.
Use safe YAML loading; no object tags. Enforce the design's hard v1 bounds:
32 steps, 16 parallel reviewers, 10 coder retries and 100 root-task consults.
Reject nested flows/recursive role expansion rather than creating a hidden DAG.

An `authority` key anywhere is rejected. Authority is assigned from the file
the check came from. The user file is `garuda.yaml` next to the resolved global
settings file, so `GARUDA_GLOBAL_SETTINGS` moves both. User-file checks run in
every project; `config show` lists them separately from project checks. Implement the design's fixed matrix: legacy fields retain
their existing resolver; new selection is CLI > trusted project > user > package
default; named roles/flows replace whole definitions; ceilings intersect;
acceptance checks accumulate with provenance-preserving command deduplication.
Deduplication keys include exact argv/shell text, cwd, environment and execution
mode; no lossy whitespace/quoting normalization of shell commands.
`harnesses` and `sessions.keep_days` are user-only. Conflicting legacy top-level
keys or incompatible explicit role/runtime/model selections give
`config.conflict` rather than a hybrid runtime identity.
Legacy runtime/model flags bypass implicit `defaults.role`; they do not silently
rewrite an explicitly selected role/flow. A project fallback chain must be an
ordered subset of the user-authorized chain; omission removes it.
Project consult targets must be a subset of user grants; omission removes them.
Project consult limits may only decrease user ceilings. User-file resolution
delegates to `global_settings_path()`, including existing home compatibility.

`config migrate` renders additive suggestions and a semantic diff. `--write`
backs up changed files, adds only supported definitions, refuses conflicts and
never deletes the source. Repeating the migration is a no-op.

**Acceptance:** every maintained configuration example round-trips; old config
and a migration without opting into a role/flow produce the same native run
plan. A table at the public resolver boundary covers every overlapping layer,
whole-definition replacement, explicit native selection, conflicting selectors,
check accumulation and ceiling intersection. Preview and repeated migration do
not alter source bytes; a conflict refuses before any write or runtime launch.

### C.2 Provenance and project trust

**Files:** effective-config and trust modules, configuration CLI and security
tests.

Carry provenance on every resolved leaf. Enforce the project trust table:
commands, native models and prompt templates require an interactive hash-bound
record; runtime ids/models/permissions can only narrow user authority.

**Acceptance:** property-based inputs cannot widen executable, provider,
endpoint, credential, prompt, permission, model-allowlist or confinement
authority. Changing any trusted file byte invalidates its record. Race a file
replacement/symlink swap against launch: only the exact trusted parsed bytes
may execute, never a reopened replacement.

### C.3 Exact role and ACP model resolution

**Files:** shared setup, ACP client/adapter/fake agent and conformance tests.

Resolve a role to runtime id, exact model option id, permission ceiling,
profile, configuration digest and provenance. Friendly matching exists only in
interactive setup and is persisted as an exact id. Set ACP options before the
first prompt only when A.3 proved support for that adapter identity.

`effort` maps to the native reasoning-effort setting. For ACP it maps to an
effort option only when A.3 proved one for that adapter identity; otherwise
setting it refuses before a prompt.

**Acceptance:** missing or changed ids refuse before a prompt; the chosen id and
adapter identity persist in the session and step receipt.

### C.4 `init`, `doctor` and `config show`

**Files:** onboarding modules, CLI and guides.

`init` shows proposed roles, exact/unknown model choices, default single-role
behavior, optional worst-case flow invocation counts (retries plus consults)
and known/unknown cost. It writes only
after confirmation. `doctor` reports executable/version/login/capability,
configuration provenance, leases, queues, worktrees and actionable diagnostics.

Login probes interpret documented success, documented logged-out status and
unexpected failure separately, with exact-version manifest argv, closed stdin,
minimal environment, timeout, bounded output and a 60-second cache. Refresh
before login-based fallback. Unsupported status behavior is `unknown`. Record
the explicit durable decision superseding builtin unknown-until-run status;
output, account names and credential values are never stored.

`init` proposes the four roles the example flows use: `scout`, `planner`,
`coder` and `reviewer`.

`init --project` proposes acceptance checks from marker files, as in the
design's table. It reuses the bounded trait detection in
`garuda/runtime/selection/`. On confirmation it writes the project
`garuda.yaml` and this user's trust record in one step.

Add `garuda/diagnostics.py` with one `Diagnostic(code, message, fix)` type and a
registry of stable codes. It starts with the codes the design lists. Doctor,
init, selection explanations and the session service render diagnostics, and
later tasks register their codes there.

**Acceptance:**

- First use remains bounded and safe; no provider or adapter is installed or
  probed merely because another provider is selected.
- Each marker fixture proposes its expected check, and nothing runs before
  confirmation.
- Tests assert diagnostic codes, not message text.
- Every registered code has a fix template.
- Exercised documented logged-out status is distinguishable from a timeout,
  malformed response or unknown version; only the former can enable fallback.
  The public doctor result and filesystem show that raw output is not retained.

### C.5 Attributed verification receipts

**Files:** verifier/evidence integration, session result migration, new receipt
model and verification tests.

Record command authority and exact code fingerprint. The authorities are:

- `user-config`, `trusted-project` and `user-request` (from `--check`), which
  are acceptance checks and may produce `passed`;
- `agent-suggested`, which covers the native completion gate's commands and an
  ACP harness's listed commands, and only sets the self-check result.

The native gate itself is unchanged and retains its separate receipt, task
acceptance-contract evidence and user-configured authoritative grader
provenance. Agent-chosen commands do not inherit a grader's authority. With no
acceptance check, verification is `unavailable` and carries
`verification.no_trusted_check`. Determine whether
test-infrastructure changes affect a particular check, instead of applying a
repository-wide automatic downgrade.

**Acceptance:** the matrix covers trusted pass/fail, suggestion-only pass,
missing checks with the diagnostic, `--check`, stale code, relevant and
irrelevant test-infrastructure edits, permission refusal and runtime failure.
Outcome, verification and self-check remain independent in every row.
Include a native authoritative-grader case and legacy projection assertions in
the existing boundary matrix rather than duplicating it in every interface.
No-edits steps cannot bypass their command ceiling to run checks; parent/coder
phases own separately authorized execution. Evidence reuse requires the exact
workspace fingerprint and original authority, not a reviewer assertion.

### C.6a Sequential flow engine and step receipts

**Files:** new `garuda/flows/` package, session service and flow contract tests.

**Dependencies:** B.6, C.3, C.5 and C.10. C.8a lands before any external
`readonly` role is enabled. Native-only flows may proceed independently.

The parent selects one workspace and holds a scoped internal lease capability.
Children cannot construct or reuse that capability independently. Before a step
starts, validate its typed input artifacts and workspace version. After it ends,
persist an immutable receipt and output artifact references. Validate typed
artifact digest, producer/attempt, bounded size, workspace version and no-follow
session-relative path. Journal intent before launching; a crash with no terminal
receipt quarantines the attempt rather than replaying a possible mutation.
Garuda writes artifacts from the versioned, bounded structured-output envelope
for both native and ACP runtimes. Text alone or an arbitrary model-supplied path
cannot satisfy a required artifact; planner/reviewer source writes are unnecessary.

**Acceptance:** missing, forged, escaping, symlinked or stale inputs refuse;
distinct attempts produce distinct baselines/deltas and receipts. Competing
entries cannot acquire the parent lease between steps. Parent cancellation
reaps children. Kill the worker before launch, during a mutation and after
receipt publication: resume cannot repeat a completed or ambiguous mutation.

### C.6b Packaged flows and onboarding

**Dependencies:** C.6a and C.7. **Files:** flow package data and user guides.

Ship the three example flows from the design (`pair`, `plan-build-review`,
`plan-only`) as package data, with their typed artifacts filled in. Their
scout, planner and reviewer steps use `no-edits`, so they run with Claude Code
and Codex without Docker.

- They can be run by name, and `garuda config show --flow <name>` prints one to
  copy.
- A user flow with the same name replaces the example.
- A flow naming a missing role refuses before launch and lists the roles it
  needs.
- The examples also appear in `guides/sessions-and-flows.md`.

**Acceptance:**

- A three-step flow refuses missing or wrong-version artifacts.
- It records distinct baselines and deltas.
- It prevents external lease acquisition between steps.
- It cancels the active child with the parent.
- It recognizes interrupted intents and requires explicit retry rather than
  replaying completed or ambiguous mutations.
- Each example flow validates and runs against fake runtimes.
- A same-name user flow takes precedence.
- A missing role refuses with the diagnostic.

### C.7 Review mechanics and independence policy

Parse a bounded review block for `approve` or `changes` and structured findings.
Invalid output stops the flow; valid major/blocker findings request changes. Retry only the named
role and stop after the configured bound. `review.by` must name the declared
terminal reviewer, not create a duplicate reviewer. `max_rounds` counts coder
retries after the initial attempt: two means at most three coder/reviewer pairs.
Regenerate workspace-bound artifacts on retry; never reuse old deltas/checks.

Record requested and actual reviewer independence. Display
`review_approved`/`review_changes_requested`; never map review approval to
verification success.
Apply the chosen independence policy to actual runtime/model/fallback identities
and earlier consults before dispatch. Packaged flows require a second opinion,
not an unproved vendor/model-family independence guarantee.

**Acceptance:** parsing, retry and exhaustion tests prove mechanics. They do not
claim semantic review correctness. Assert exact public launch/receipt sequence,
no duplicate final reviewer, regenerated artifacts and rejection of a consulted
or fallback-aliased reviewer when the requested independence policy forbids it.

### C.8a Read-only ACP confinement

**Dependencies:** A.3, B.5, B.6 and C.3. Enforce Docker-class confinement for every
external ACP role marked `readonly`, including standalone runs, sequential
scouts/planners/reviewers and fallbacks. Read-only source, bounded scratch,
excluded host credential stores/other workspaces/Docker socket, unprivileged
containers and a usable user-authorized in-container runtime are preflight
requirements. Otherwise refuse with
`workspace.readonly_unenforced` before launch. No host runtime substitution.

**Acceptance:** standalone and sequential entry points share the same preflight
refusal contract. A real deliberately writing Docker runtime cannot alter the
source, repository metadata or host sentinel; bounded scratch remains usable.
Worktree-only execution refuses. No live Docker evidence means this capability
remains disabled, not that fake protocol tests establish confinement.

### C.8b Parallel review snapshots

**Dependencies:** C.6a, C.7 and C.8a. Add parallel review groups using one immutable Git snapshot from
A.5. Native reviewers use existing read-only tools; external reviewers reuse
the already-proved confinement service rather than a second permission path.

**Acceptance:** standalone, sequential and parallel entry points share the same
preflight refusal contract. In real Docker, a deliberately writing external
runtime cannot change the source, destination repository or an out-of-workspace
sentinel, while its permitted scratch succeeds. Worktree-only execution refuses.
If Docker proof is unavailable, the capability stays disabled; fake protocol
tests do not establish confinement.

### C.9 Start-time role fallbacks

**Files:** effective-config role model, shared setup, session service and
fallback tests.

A role may list ordered fallbacks in the user file, each naming an exact harness
and model id. Before a session starts, the service walks candidates once. It
skips a candidate only for that candidate's recorded eligible reason, using
codes from the C.4 diagnostic registry:

- `harness.cli_missing` or `harness.adapter_missing`;
- `harness.logged_out`;
- `harness.limit_reached`, only after E.2 proves fresh same-account status, an
  explicit future reset and a current exhausted window/event. Enable this
  reason in a later integration PR; C.9's other reasons ship independently.

**Rules:**

- An unknown limit is not a reason.
- Unbound, stale, expired, version-changed or reset-unknown limit evidence is not
  a reason. Login-based fallback uses the freshly checked documented status.
- Every candidate still meets the authorized model, permission, confinement and
  capability contract. Trust/configuration/confinement errors refuse; they
  cannot select another provider.
- Fallback never happens after a prompt has been sent, and never mid-run.
- The preflight, session record, step receipt and ledger show the primary, the
  entry taken and the reason.
- A project may remove fallbacks but not add them.
- If E.1 has not landed, the session/step selection receipt is authoritative;
  ingestion adds the same identified `fallback_start` record when the ledger
  becomes available. This does not delay missing/logged-out fallbacks.

**Acceptance:**

- Each reason selects the next entry.
- An unknown limit keeps the primary.
- Unbound/stale/reset-unknown/account-changed evidence keeps the primary. A
  candidate rejected for confinement or trust sends no prompt to any fallback.
- When every entry fails, the run refuses and lists every reason.
- No prompt reaches a runtime before the decision is recorded.

### C.10 `no-edits` steps

**Files:** flow models, the session service, `garuda/acp/broker.py` ceilings,
workspace delta comparison, and no-edits tests.

**Dependencies:** B.5, B.6 and C.1–C.3. `write_policy: no-edits` is accepted as
a role or step policy without extending/reinterpreting the permission enum.
It works in standalone runs and sequential steps, never parallel groups.

- The broker denies file-edit and command-execution permission requests for the
  step.
- After all runtime descendants exit, compare the design's bounded no-follow
  workspace/metadata manifest, including ignored files, modes and symlinks.
  Changes or incomplete/unknown attribution withhold outputs and stop the flow
  before its next step; record evidence and revert nothing. Detect observed
  final state, not writes outside the workspace or transient reverted writes.
- The CLI, receipts and dashboard label the mode "guardrail, not confinement".
  An unchanged result reads "no changes detected".
- `no-edits` in a parallel group refuses validation; parallel review keeps
  C.8b's `readonly` requirement.

**Acceptance:**

- A fake ACP reviewer that writes despite a denied request stops the flow, the
  next step never starts, and the written bytes are still present.
- A non-writing reviewer lets the flow continue.
- Denied permission requests are recorded.
- Validation refuses a parallel `no-edits` group.
- No surface describes the mode as read-only or confined.
- An ignored-file write, mode/symlink change, repository ref change or failed
  baseline cannot be reported as an unchanged workspace. Standalone and flow
  paths enforce the same contract without erasing user edits.

## Set D — background control and session views

### D.1 Production queue store

Promote A.4 into a versioned durable store. Bind entries and claims to user,
harness, session, worker identity and configuration digest. Preserve FIFO within
that scope, and expose read-only queue inspection. Capacity per harness comes
from `harnesses.<id>.max_parallel` in the user file and uses B.0's shared
capacity store; D.1 must not introduce queue-only capacity or TTL-only takeover.
The shared store must also refuse same-holder replacement of live/unknown
owners (#213); an exact-owner retry is idempotent and confirmed-dead takeover
retains stale-release protection. Audit this prerequisite before queue wiring.

Enqueue retries must preserve the exact scope/user/harness/session/configuration
binding across waiting entries and claims, and preserve durable FIFO sequence.
Conflicting retries refuse before writing. Cover exact and conflicting retries
from real subprocess contenders and leave preexisting duplicate records intact.

Background admission resolves effective `garuda.yaml` roles through shared
agent setup before creating launch state, retaining the selected role and
original harness reference. The worker compares the effective configuration
digest before selection. Dynamic role fallback refuses until a persisted
selection decision is available. Referenced runtime/agent/MCP files and later concurrent
configuration edits remain outside this pre-selection check.

Ordinary reservations lack descendant cleanup receipts, so both ordinary and
queue admission retain them after parent death. Prove this with a real parent
that reserves capacity, spawns a writer in another group and exits: same-holder,
other-holder, matching-dead-owner retry and queue callers must refuse without
changing capacity bytes. Explicit matched release remains a coordinator cleanup
assertion; do not invent automatic recovery from a pid/TTL/group observation.

Check complete committed adoption at direct dispatch, and recheck ordinary
capacity activation under its file lock and after publication before returning
launch authority. Exercise failed release intent from the claimant and another
store instance, workspace refusal, and real threads that capture a ticket before
the lock or revoke it during activation publication. Retain protected capacity
and source records on refusal; partial activation remains quarantined.

Enforce user/harness scope consistency for dispatch-ready admissions at both
enqueue and ticket creation. Exercise a fully bound second job that tries to
jump an older job's lane with a different scope label, and a preexisting record
that bypasses enqueue. Refuse before intent/capacity publication without
rewriting its bindings or manufacturing missing dispatch evidence.

Check complete owner identity on heartbeat, release and workspace requeue.
Implicit calls must use this instance's retained successful claim in the
claiming process, never owner data read from the store. Test unowned and stale
instances, inherited fork authority, identity mismatches sharing an epoch, and
a replacement claim during both refused and exceptional workspace acquisition.

Make inspection pure from construction onward: no directory initialization,
chmod, lock acquisition, migration publication or backups on `entries()` or
`snapshot()`. Verify absent stores and existing current/legacy/corrupt/future
records with independent byte, permission, timestamp and inventory checks,
plus an inspecting subprocess while a real writer holds the lock.

Refuse mutation of any nonempty legacy record because binding evidence is
missing, including live/dead/unknown owners. Validate empty legacy schemas,
archive exact bytes durably before publication, reuse only verified matching
private regular archives, and never import a legacy capacity ceiling. Test
process death at each flush/publication boundary and an exact retry, backup
ambiguity, replaced directories and nonregular files. These tests do not
substitute for the cross-store queue/capacity crash journal.

The production journal now uses queue version 3 and capacity version 2:
publish intent, reserve a protected slot, publish claim, commit selection, clear
intent, then retain local activation authority. Release publishes intent, fences
captured adoption tickets in capacity while retaining activation history, then
publishes claim removal before returning capacity. Ordinary callers cannot reclaim queue
slots. Recover confirmed-dead pre-activation operations without launch; quarantine
activated or ambiguous dispatch. Verify actual process death at every publication
boundary and foreground admission through the ordinary launch guard. Requeue
workspace-refused work by original durable sequence even when callbacks finish
out of order. Keep unresolved transactions visible through pure inspection.

Version 2 waiters may migrate only with full bindings and ordered sequences;
version 2 claims refuse without activation evidence. Nonempty capacity version 1
records refuse mutation without reservation origin evidence; empty ones archive
exact bytes before upgrading. Preserve unrelated archives in every case.

**Acceptance:** repeat A.4 against the production API plus migration, corrupt
record and clock-skew cases.

### D.2 Detached workers and lifecycle

`--bg` resolves the runtime reference through the shared trusted catalog before
session or queue creation, using the canonical capacity key and retaining the
original alias reference for downstream capability resolution. Before worker identity publication or
selection, match its resolved lane/session and serialized argument receipt to
the admitted queue record. Refuse retargeted aliases and changed argument/receipt
records without rewriting them; an absent admission starts no work. This receipt
covers arguments, not every referenced configuration file or cleanup evidence.

`--bg` creates the session and queue entry, then re-executes a hidden worker with
an explicit session id. The worker either claims capacity or waits without a
workspace lease. Record PID/start-time/command identity before it can mutate.

Implement queued cancellation, active cancellation, crash classification,
bounded logs and terminal cleanup. `garuda sessions` remains a one-shot script-
friendly view; background execution does not require the dashboard to run.

The worker now persists activation before the runner. Worker death after that
point quarantines its queue slot, including when a runtime survives in a separate
process group. This fence does not complete descendant supervision or cleanup
receipts; those remain required before every terminal path can safely release.

**Acceptance:** real subprocess tests cover queue-to-run promotion, cancellation
before launch, crash, stale PID reuse defense and slot release on every terminal
path.

### D.3 Shared read model and live stream

Build one pure read model over session, queue, approval and flow records. CLI and
web render that same model. Add server-sent events only after snapshot responses
are correct; polling remains a fallback.

**Acceptance:** given the same fixtures, CLI JSON and web API expose equivalent
state/outcome/verification and provenance. Stream reconnect resumes without
duplicating or skipping complete events.

### D.4 Sessions and flow views

Show project/name, role/runtime/model identity, process/work/outcome/verification,
verification authority, workspace/branch, queue position, usage/cost and fixes.
Flow details show steps, attempts, artifact edges, deltas and review outcomes.

Unknown cost and capability remain visibly unknown. A review badge is never
rendered as verification. Usage and cost show `unknown` until E.1 lands; this
view never computes accounting itself.

### D.5 Approval inbox through the broker

The inbox reads pending bound requests and submits a session-bound response
to the file channel. It never answers a runtime directly. Keep existing Host,
Origin and capability-token gates.

**Acceptance:** browser and HTTP-boundary tests prove request binding, one winner,
timeout behavior, stale page rejection and ceiling revalidation.

### D.6 Background-control browser acceptance

Seed through production session, flow, queue and approval writers. Exercise
queued/working/waiting/crashed/stopped states, independent outcome and
verification, a pending/stale approval, stop and stream reconnect. Compare the
same records with CLI JSON and web responses. This gate does not wait for usage
analytics. Use the documented live-Chrome runner and report whether it ran.

## Set E — usage accounting and provider limits

### E.1 Idempotent usage ledger

**Dependencies:** A.3, A.6 and B.6 (including B.1/B.2). Land alongside C/D to
build history before analytics. No flow, worker or dashboard dependency.

**Files:** a focused usage-ledger module, `garuda/core/metrics.py`,
`garuda/model/litellm_model.py`, `garuda/context/summarizer.py`, ACP
normalization and ledger tests.

**Change:**

- Append one record per native model call and one per ACP usage-report delta,
  only where that delta's semantics are proved. Preserve other ACP reports as
  snapshots. Implement the design's versioned record kinds, idempotency keys,
  attribution, accumulation windows and coverage markers.
- Deduplicate by stable call/turn/event identity under the store lock. Track
  cumulative epochs/high-water marks, refusing fabricated deltas after a gap,
  reset, drift or unexplained decrease. Do not count ACP turns/snapshots as
  model calls, or context occupancy as billable tokens.
- Preserve one accounting source for a given consumption window; simultaneous
  cumulative and per-turn reporting cannot contribute twice.
- Sources without stable report/turn identity remain snapshots. Range totals
  exclude snapshots without a proved baseline and label coverage incomplete.
  Price-source revisions are fixed on ingestion, not recalculated on rendering.
- Tag the calls that are untagged today, including summarizer calls, and delete
  the backlog item "Summarizer model calls are invisible in token accounting".
- Emit deduplicated `fallback_start` records from session selection, separately
  from inference attempts. Incomplete native accounting after a crash is marked
  incomplete until reconciled against persisted inference events.
- Rotate the ledger monthly, and prune past the retention setting.
- Persist role, origin and original call purpose as separate dimensions. Parent
  rollups reference unique child events rather than appending duplicate charges.

**Acceptance:**

- For native fixture sessions, ledger totals equal each session's own
  `aggregate_model_metrics` totals.
- ACP fixtures have fixed hand-calculated expectations for per-turn versus
  cumulative reporting, repeated/replayed/out-of-order snapshots, reset epochs,
  missing baselines, reconnect gaps and simultaneous reporting. Expected totals
  are not produced by the normalizer or a second copy of its algorithm.
- The writer refuses a record with any field outside the schema.
- Identifiers and exports contain no task, path, raw provider data or identity
  key. Unknown model attribution/count/cost remains unknown.
- Concurrent writers in separate processes never interleave records.
- A process crash around append/cursor publication followed by replay cannot
  produce duplicate charges; truncated records are classified before recovery.
- Pruning sessions leaves the ledger totals unchanged.

### E.2 Account-bound limit observations and quota fallback

**Dependencies:** E.1 and the exercised C.4 probes. Limit-fallback integration
also requires C.9; keep its enabling change in a focused follow-up PR.

Normalize only A.6-proved structured limit errors and status sources. Append
deduplicated `limit_event` records and atomically replace per-harness/profile
snapshots. Store a user-local digest of an officially supplied account/profile
id, exact version, observed time, at-most-60-second fallback TTL and explicit
reset time. Never persist an account name or inspect credential material.

Refresh before quota selection. Account/profile/login/version changes invalidate
eligibility. Without a fresh same-account observation and future reset, an event
can be displayed historically but cannot trigger fallback. Unknown reset time
does not make the provider unavailable indefinitely.

**Acceptance:** use public doctor/selection boundaries and recorded source
fixtures for known, unknown, stale, expired, changed-account/version and missing
reset cases. Only a fresh account-bound exhausted observation enables C.9's quota
reason; no prompt is issued by refresh. Race tests prove snapshots are complete
and ledger events are deduplicated. Without a proved account identifier, the
provider remains eligible and its quota-fallback capability is disabled.

## Set F — dashboard observability and exports

**Dependencies:** C's effective config/diagnostics, D.3's shared read model and
E's accounting contracts. Views consume those public records; they cannot add
an accounting source or execution authority. Ship each view as a separate PR.

### F.1 Conversations view

**Files:** `garuda/interfaces/web/reads.py`, `garuda/observability/lanes.py` and
`trajectory.py`, `static/views_trajectory.js`, and browser fixtures.

**Change:**

- Extend the existing trajectory view into the conversation page rather than
  adding a second view.
- Render native and ACP lanes from persisted records, with a model badge on each
  call or turn.
- Build the "Models used" table from the session's ledger records, grouped by
  work type, harness and model. For native sessions recorded before E.1 landed,
  fall back to the session's own `aggregate_model_metrics`, and label the table
  "from session metrics".
- Show the sessions this conversation tagged, and the sessions that tagged it,
  with harness, model and links.
- Show linked and fallback sessions as one story, each with its own lane.
- Show ACP calls on other models that the harness does not report as
  `not reported`.
- Separate selected model from reported attribution, context snapshots from
  billable usage, and ACP reported turns from native call counts.

**Acceptance:**

- A native session with collector, classifier and summarizer calls shows one row
  per work type, with totals equal to its ledger records.
- A flow shows its step roles as work types.
- An ACP session shows its selected model, and `not reported` for internal
  calls/counts. Unattributed reports are not assigned to the selected model.
- A pre-ledger native session shows its session-metrics totals with the label.
- Tag links resolve in both directions.
- Message content is rendered escaped.

### F.2 Providers and limits view

**Files:** `garuda/interfaces/web/runtimes.py` and `static/views_runtimes.js`
(both extended), the doctor JSON, the ledger and the limit snapshot.

**Change:**

- Show one card per harness and API provider, with the design's fields.
- Every limit value shows its source and observed time. A source A.6 did not
  prove renders as `unknown`.
- Label observed use "through Garuda only".
- Refresh re-runs the cached doctor probes and never sends a prompt.

**Acceptance:** each of these fixtures renders as specified:

- known windows;
- unknown windows;
- a stale observation;
- an unexpired limit event, an expired one, and one with no reported reset
  time, which is shown as historical;
- a version that differs from the captured capability matrix;
- a changed account/profile and a source with no account identifier;
- a logged-out harness.

### F.3 Usage statistics and aggregate exports

**Files:** `garuda/interfaces/web/reads.py` (ledger aggregates reusing the
`aggregate_model_metrics` record shape), `static/views_runs.js` and `charts.js`
(both extended), and browser fixtures.

**Change:**

- Ranges of 24 hours, 7 days and 30 days.
- As designed: work-type share, a harness/model table, a daily trend, role and
  opaque-project tables, and CSV or JSON aggregate export. Outcome, verification
  and approval detail stays in session views; it is not inferred from this
  accounting ledger.
- Keep unknown cost separate from known cost everywhere.
- Show native model calls, ACP reported turns and context/cost snapshots as
  different measures; percentages use only records with comparable units and
  display excluded/unknown coverage.
- State on the view that statistics are never a routing input.

**Acceptance:**

- A small fixed ledger spanning range boundaries has hand-calculated golden
  totals for every table, median and export. Do not generate expected values
  with the production aggregate helper or a duplicate aggregation algorithm.
- UTC instants define rolling ranges; timezone labels affect display only.
- Duplicate/replayed reports do not alter totals, and incompatible ACP/native
  units cannot enter a shared call or token denominator.
- Exports contain schema fields only.
- A range with only unpriced calls shows unknown cost, not zero.

### F.4 Setup view

Show the doctor results and effective-config provenance, with copyable fix
commands. Show the role table: each role's harness, exact model id, effort and
fallback chain. List the available flows, marking the example flows and naming
any missing roles. The view is read-only.

### F.5 Observability browser acceptance

Seed records through production writers rather than hand-authoring fields that
the reader is supposed to validate. Reuse D.6's fixtures and runner; worker,
broker and state-transition assertions stay with their existing owner tests.
This gate adds rendering and navigation checks for:

- native, ACP and brief-fallback sessions;
- a multi-round flow with typed artifacts, and an example flow;
- a session that tagged a session from another harness;
- explicitly authorized cross-project sharing and its receipt;
- known and unknown usage/cost;
- ACP cumulative/per-turn fixtures, duplicate reports and unknown internal calls;
- a conversation with several work types, an ACP lane and a fallback start;
- provider cards with known and unknown limits and a limit event;
- usage statistics over each range, and their export;
- setup diagnostics and the role table.

Use the documented live-Chrome gate and state explicitly when it was not run.

## Set G — consults between roles

### G.1 Consult transport spike
**Files:** opt-in capture script, redacted ACP fixtures and the external-harness
guide. Reuse A.3 captures; do not buy a second run for the same evidence.

For exact installed Claude Code/Codex adapter identities, exercise MCP stdio
forwarding from `session/new`, listing/calling the tool, structured server/tool
permission identity, request/response errors and cancellation. Separately prove
a documented quiescence handshake that stops all caller workspace operations
during snapshot capture, including overlapping tool calls. An MCP callback
alone does not prove a paused workspace. Record method, version, timeout and
failure behavior, not just a boolean.

Discovery uses no model prompt where supported. Actual tool invocation is a
separately authorized smallest live test with a hard run budget; CI uses a fake
server. No live calls are authorized by this documentation change.

**Gate:** record independent `supported`/`declared`/`unknown` outcomes for
forwarding, permission provenance and quiescence. G.3 requires all three for
that exact binary/schema identity. Unsupported ACP initiation remains disabled;
it does not block native askers or the rest of the roadmap.

### G.2 Consult engine for native askers

**Dependencies:** B.0, B.5, B.6, B.7, C.1–C.3 and C.9. External consulted
children additionally require C.8a. Native-only delivery does not wait for G.1
or Docker. C.10 is not a substitute for consult child isolation.

**Files:** focused `garuda/consult/` admission/runner/receipt modules, session
service, native tools and owner-boundary tests. Configuration was added in C.1;
do not introduce a second parser here.

Implement the design's contracts in this order:

1. Authorize asker ancestry, target and narrowed ceilings; bound request/brief.
2. Atomically reserve the root-task count and durable request id. Requests bind
   payload digests; replays cannot launch another child. One active consult per
   root; concurrent calls return `consult.busy`.
3. Resolve the exact trusted target/fallback, isolation and shared capacity.
   Refuse unavailable capacity immediately, without waiting on the parent.
4. Quiesce native workspace operations and capture the bounded stable snapshot.
   Create an independent repository with no shared Git metadata/alternates.
5. Launch the dedicated native scoped read-tools profile, or the C.8a confined
   external runtime. Disable shell, custom hooks/tools, network, approvals,
   tags and recursive consults; external runtimes never run on the host.
6. Enforce deadline and native turn/output caps. On cancellation/crash, close
   and reap descendants, retaining ownership if liveness is unknown.
7. Validate terminal state and snapshot/metadata delta; withhold answers on
   changed/unknown evidence. Persist receipt and cached result, then clean up
   only owned scratch and release capacity. Return bounded labelled advice.

**Acceptance:**

- An authorized native caller gets a scoped native child's labelled answer.
  Forged target, child ancestry or project-expanded limits launch nothing.
- Concurrent root/flow requests cannot exceed count/capacity; resume does not
  reset limits. Repeated request ids return one result; altered payloads refuse.
- Child writes to Git refs cannot reach caller metadata. A denied native shell,
  escaping file path or custom tool cannot execute. Host ACP children refuse;
  real Docker proof for an external child remains the C.8a gate.
- Snapshot capture blocks concurrent native tool writes; the child subsequently
  sees the snapshot even as its caller proceeds. Changed or uninspectable child
  evidence yields no answer.
- Capacity one on the parent's harness returns
  `consult.capacity_unavailable` promptly. Concurrent calls return busy.
- Crash before dispatch, during a child and after receipt publication cannot
  duplicate a launch. Timeout/cancellation reap before releasing capacity;
  failed reaping quarantines, rather than claiming recovery succeeded.
- Question/brief/answer limits are finite. Envelope-breakout payloads remain
  labelled data; no test asserts semantic prompt-injection immunity.
- Receipts contain identities/digests/evidence references, not question/answer
  text. Native denied-operation counts consume structured permission refusals
  and file-access denial results, including repeated/sequential/parallel attempts;
  an ordinary missing file with “denied” in its name contributes zero.
  Consultation never sets task verification to passed.

### G.3 MCP consult tool for ACP askers

**Dependencies:** G.1 and G.2, with all exact-version transport gates met.

**Files:** hidden `garuda _consult-mcp` stdio entry point, trusted ACP
handshake construction, broker, secret-redaction and transport tests.

Expose exactly `consult` through a trusted absolute executable and narrowly
scoped environment. A session-local Unix socket in a `0700` directory binds
asker id, process identity and epoch to a random 256-bit token. Register
redaction before constructing/logging any `mcpServers.env` content. Never
persist or pass the token/endpoint to the consulted child. Requests use the
G.2 replay-safe id/payload contract; clients cannot select another session.

Auto-allow only exercised structured MCP server/tool identity plus active
session provenance; display titles are untrusted. Other permissions are
unchanged. Resume revokes/rotates old endpoints and tokens. The caller must
complete its proved quiescence handshake before capture, or the call refuses.
Missing forwarding, identity or quiescence support means no tool is exposed.

**Acceptance:** exercise an ACP asker through a real stdio/socket process plus
fake ACP server. Wrong tokens, foreign session selection, ended/old epochs,
forged display titles and recursive clients refuse. Duplicate requests produce
one dispatch. Capture verbose logs, failures and support exports: the token
must be absent everywhere, including the initial handshake. Verify owner-only
socket permissions; do not claim protection from hostile same-UID processes.
Removed targets/unsupported versions expose no MCP tool. A live adapter claim
requires G.1 evidence with exact versions and separately authorized spend.

### G.4 Consult visibility

**Dependencies:** G.2, D.3, E.1 and F.1.

**Files:** CLI summary, shared read model, conversation/usage views and browser
fixtures written through production persistence APIs.

Show nested caller/child lanes with admission, outcome, duration, denied
operations and observed changes. Expose role targets, ceilings and transport
status in setup. Flows record actual consulted identities and the independence
decision, not merely the target alias.

Use `origin: consult` as a grouping dimension alongside original
`call_purpose`. Parent views roll up unique descendant events once; global
aggregates sum original events, never parent rollups plus children.

**Acceptance:** production-written fixtures render success, refusal, timeout,
quarantine and changed/unknown evidence. Independent hand-calculated totals
agree between child, parent and global views with no double-counting or loss
of summarization purpose. Reviewer annotations use actual fallback/consult
identity and policy. Live Chrome remains the browser acceptance gate.

## Documentation work by set

- Set A corrects current-main facts, records measured ACP support, and updates
  the `mkdocs.yml` navigation for these records.
- Set B updates architecture, modules, `safety-and-workspaces.md` (worktrees and
  leases) and the CLI reference (resume, tags).
- Set C:
  - adds `guides/sessions-and-flows.md`, covering sessions, tags, worktrees, the
    example flows and checks, to the `mkdocs.yml` navigation;
  - links it from the "Level 4 · Automate" and "Level 5 · Advanced" use-case
    pages;
  - updates `configuration.md`, the quickstart (`getting-started.md`),
    `how-garuda-works.md`, `external-harnesses.md`, `command-builder.md`, the
    cheat sheet and the CLI reference.
- Set D updates `web-dashboard.md` for background sessions, flow state and
  approvals, and `troubleshooting.md` for worker/approval diagnostics.
- Set E documents ledger retention, usage coverage, account-bound limit
  freshness and quota-fallback requirements in the existing harness and
  configuration guides.
- Set F updates `web-dashboard.md` for conversations, providers, usage/export
  and setup; it links to E's accounting contract instead of redefining it.
- Set G adds a "Consults" section to `guides/sessions-and-flows.md` (setup,
  limits, what a consult can and cannot do), notes consult transport support in
  `external-harnesses.md`, and adds the consult codes to `troubleshooting.md`.

Keep `safety-and-workspaces.md` and `external-harnesses.md` as dedicated guides.
Do not collapse them into onboarding or configuration.

## Explicitly deferred

- Removing the P2 router, recipes, rigorous mode, runtime CLI or eval CLI.
- Changing the canonical configuration home or deleting environment variables.
- Automatic task-text flow routing.
- A general DAG/workflow language.
- A resident scheduler/daemon.
- ACP v2 migration.
- Quality-history-based routing.
- Editing configuration from the dashboard.
- Polling vendor billing, credit or account endpoints.
- Automatic application of integration commits to the user's live checkout.
- Automatic integration hooks/signing; v1 refuses mandatory requirements.
- Non-Git/submodule/sparse/custom-filter snapshots and ACP consult initiation
  without an exercised quiescence/permission-identity contract.

Each removal or protocol migration needs its own design, named release targets,
compatibility window and rollback story after replacements have shipped.

## Milestones and acceptance

| Milestone | User-visible result | Release gate |
|---|---|---|
| A | Current defects fixed; capability, scheduler, Git and usage-source assumptions measured | Current-main audit plus all four proof spikes |
| B | Opaque project-scoped sessions, safe worktrees, one lifecycle, resume, explicit context sharing and secure approvals | Cross-process identity/lease/Git/approval suites, legacy projection and sharing authorization |
| C | Additive config, exact roles/models, example flows, checks, missing/logged-out fallbacks and receipts | Precedence/trust matrix, flow restart, attributed verification and live Docker confinement for every ACP read-only role |
| D | Durable background work, shared session/flow views and broker approvals | Real worker races, HTTP/browser checks and CLI/web equivalence |
| E | Idempotent accounting, sourced limits and gated quota fallback | Native ledger-to-metrics equality, ACP golden fixtures, crash/replay recovery and fresh account-bound selection |
| F | Conversations, provider status, usage aggregates/exports and setup views | Hand-calculated aggregate/export fixtures and browser checks over production-written records |
| G | Roles can consult other roles; answers return as data with full receipts | Authorization, snapshot-isolation, no-deadlock, timeout/crash, token-binding and envelope suites; real-adapter transport only behind G.1's gate |

Full `pytest -q` and `ruff check garuda tests` remain implementation merge gates
after focused owner tests; docs-only PRs run docs-contract and strict site checks.
Live integrations run only for the capability being enabled and report exact
versions. Unavailable adapter/Docker proof leaves that capability disabled;
browser views require the live-Chrome gate before their acceptance claim.

## Risk register

| Risk | Required mitigation |
|---|---|
| Adapter declares a method it cannot complete | Exercise and pin adapter identity; otherwise fallback/refuse |
| Queue loses its scheduler | Every queued request owns a waiting worker; cross-process claim and stale recovery are tested |
| Worktree presented as confinement | Docker gate for every external ACP role marked read-only, including standalone and sequential work |
| Docker requirement blocks everyday flows | `no-edits` steps for sequential work: broker denials plus change detection that stops the flow, labelled as a guardrail |
| A `no-edits` step writes anyway | Bounded manifest and repository metadata comparison; changed/unknown evidence withholds output and stops; nothing is reverted |
| Merge races with user or another session | Prepare/check in owned workspace; CAS-publish only a Garuda ref; no automatic live-checkout update |
| Project config injects instructions | Prompt authority excluded by default; hash-based interactive trust |
| Tagged-session content injects prompt instructions | Explicit local tags, structured escaping, provenance labels and adversarial envelope tests |
| A full id exports another project's context | Distinct cross-project option, interactive confirmation or explicit headless grant, destination-bound receipt |
| Approval response is forged or replayed | Digest/nonce/session binding, permissions, no-follow, expiry and ceiling recheck |
| Agent chooses an irrelevant passing check | Attribute as a self-check; only acceptance authority can produce `passed` |
| Users never configure acceptance checks | `init --project` proposes them from marker files and trusts them in one step; `--check` for one run; the `verification.no_trusted_check` fix is shown on every such session |
| A config file claims its own authority | `authority` keys are rejected; authority is assigned from the source file |
| Flow multiplies spend unexpectedly | Single-role default, flow preflight, explicit headless selection and unknown-cost preservation |
| Tests prove mocks or private wiring | Owner-boundary authoring gate and real subprocess/Git/HTTP/protocol tests |
| Compatibility work grows into a rewrite | Additive sets only; removals deferred to separate proposals |
| New configuration silently overrides legacy settings | Disjoint namespace, fixed precedence, whole named definitions, ceiling intersection and actionable conflict refusal |
| Richer verification breaks old consumers | Versioned additive fields, preserved raw/gate evidence and unchanged legacy result projection |
| Partial limit data presented as authoritative | Source and observed time on every value; `unknown` without a proved source; observed use labelled Garuda-only |
| Stale quota data selects another provider/account | Fresh account/version-bound evidence and future reset; otherwise display-only |
| Usage ledger leaks prompts or paths | Opaque project/account digests, schema-only records and aggregate exports; no path, account name or raw provider data |
| ACP cumulative reports double-count consumption | Typed snapshots/deltas, one source per accounting window, deduplication/epoch rules and independent golden totals |
| A crash or replay duplicates charges | Stable attempt/event ids, locked idempotent ingestion and real crash/cursor recovery tests |
| Fallback switches vendor or spend unexpectedly | User-file only, start-time only, recorded reason, shown in preflight |
| Agents loop or chain consults | Depth/ancestry enforced in admission, independent of tool visibility |
| Consults multiply spend | Durable root-task count across resume/flows, finite request/turn/deadline bounds; unknown ACP cost is not a dollar guarantee |
| A consult deadlocks on harness capacity | Never wait on capacity held by the asker or its flow; refuse immediately with `consult.capacity_unavailable` |
| A consultant edits the asker's work | Independent Git metadata, scoped native read tools or mandatory Docker for external children; no answer on changed/unknown evidence |
| Another process drives the consult endpoint | Owner-only socket, token/epoch/asker binding, request replay protection and rotation; hostile same-UID processes excluded |
| A consult answer injects instructions | Escaped, bounded, source-labelled data envelope with adversarial tests |
| Consults erode review independence | Actual fallback/consult identities checked by the review policy before launch |
| Queue/lease expiry steals a live owner | Strict shared capacity, process/descendant proof and owner epochs; TTL alone never permits takeover |
| Crash repeats an edit, approval or consult | Durable intents/delivery reservations; ambiguous dispatch quarantined, never automatically replayed |
| MCP handshake logs leak its token | Register redaction before handshake serialization; test verbose logs, failures and support exports |

## Completion definition

This roadmap is complete only when Sets A–G have independently met their gates,
current documentation describes shipped behavior, durable decisions are
recorded, and no acceptance claim relies solely on a fake runtime, declared
capability, model verdict, worktree guardrail or implementation-coupled spy.
An explicitly disabled capability is safe to release without, but is not a
completed implementation task. Record its deferred status and missing proof;
never label the entire epic complete while claiming that capability ships.
