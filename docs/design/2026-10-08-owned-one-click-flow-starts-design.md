# Owned one-click flow starts

**Status: proposed; awaiting separate P3 approval.** This is the design
deliverable for [#315](https://github.com/Darshan2104/Garuda-openagent/issues/315).
It enables no route, Start button, runtime capability or adopted decision.
The issue requires P2 acceptance to land and a separate P3 go-ahead before
the write surface is enabled. Approval of this proposal does not waive any
implementation or runtime conformance gate below.

## Baseline and scope

Inspected main: `2414886d1aa6c969ead753221024c6699798cbd3` (2026-10-08).
P0/P1a and P2 have landed through PRs #318–#325 and #327; #314 is closed.
P2's exact tested tree matches main and its required and optional CI passed.
The accepted revision-5 [plan](../plans/2026-10-06-ready-to-use-workflow-scenarios.md)
has SHA-256 `d39a83cb30a2149b273194fcbeab0bed6652be6d8697819fd65c9d886d06dca9`.

This proposal covers flow-kind Starters, their durable operation admission,
shared authority context, server lifecycle and browser follow-up. Run-kind
Starters remain preview/copy until [#316](https://github.com/Darshan2104/Garuda-openagent/issues/316)
lands its native/ACP facade and conformance gate. There is no background-flow
queue, automatic resume, new event bus, provider transport or OAuth integration.
P1b flow verification remains unavailable until its actual mechanism lands.

The [P1b ownership proposal](https://github.com/Darshan2104/Garuda-openagent/pull/326)
is still unapproved. Its shared native/ACP cleanup and strict metadata prerequisites
also affect P3. They may land before verification itself, after their own review;
this proposal cannot turn existing best-effort cleanup into closure evidence.

## Current owners and prerequisites

| Owner | Inspected behavior | Required change before enabling Start |
|---|---|---|
| `interfaces/web/scenarios.py` | Pure installed library, detail, preview and selected results. | Keep these reads pure; add a thin authorized start adapter to one gateway. |
| `scenarios/compile.py::compile_with_context` | Returns a plan and context; hashes config before/after resolution. | Freeze the exact bytes used for parsing and trust, including profiles, manifests and selected source/artifact bytes; endpoint hashes alone cannot exclude an ABA change. |
| `scenarios/service.py::StarterService.start` | Recompiles, then calls the flow facade, which reloads config. | Share validated dispatch with CLI/HTTP; carry the approved context into execution instead of resolving again after the comparison. |
| `flows/service.py::FlowExecutionService` | Resolves again; returns only after completion. A supplied flow session currently selects resume. | Add explicit new-run admission and an admission notification, separate from resume; run the same engine with a reserved parent ID and frozen context. |
| `flows/engine.py::FlowRunner.run` | Calls `_begin()` before lease acquisition; resolves role plans per attempt; releases in `finally`. | Acquire its own lease before creating the session; refuse ID collisions; use frozen authority for every branch, fallback and consult; release only after proven teardown/publication. |
| `flows/launch.py` / `StepLaunch` | No dashboard approval/ceiling context. Native setup inherits role/profile posture; ACP uses its current broker path. `StepResult` has no closure proof. | Pass a typed shared execution context through real launch owners, with bounded authority and native/ACP approval adapters; require cleanup evidence, including snapshot helpers and parallel members. |
| `core/sessions.py::SessionStore.begin` / metadata writers | Existing session metadata may be reused; publication and locking have legacy fallback behavior. | New admitted flow records need exclusive creation and strict durable publication using the common writer/lock policy; uncertainty cannot be rebuilt from partial metadata. |
| `runtime/strict_store.py` | No-follow leaf opens, cross-process lock and fsynced replacement conventions. | Use descriptor-bound containment/ownership, strict schema and size checks for the intent store; do not assume helpers prove every ancestor or bound every read. |
| `interfaces/web/approvals.py`, `run_guard.py::broker_approval_handler` | Native parked asks and ACP durable asks already have owners, timeout and audit paths. | Bridge both through their existing owners; do not substitute a permissive callback, stdin prompt or silently denied mutating ACP flow. |
| `interfaces/web/__init__.py::serve`, `live.py::LiveRuns.aclose` | Server shutdown closes existing chat/job owners. | Register and drain the additional flow gateway explicitly; HTTP-thread cancellation must never own the flow task. |

## Approaches and recommendation

1. **Shared loop-owned gateway (recommended).** A dedicated gateway admits
   durable operations and owns flow tasks on the server loop. It calls
   `FlowExecutionService` in process; that facade and the engine keep runtime
   and lease ownership. This limits changes to admission, typed authority and
   the common cleanup prerequisites, while using existing event/result views.
2. **Supervisor per operation.** A worker could import the flow service under
   a separate supervisor. This isolates process trees but also needs reviewed
   cross-process borrowing, approvals, config transfer and publication. Moving
   the engine into a worker alone does not prove safe lease release.
3. **Extend JobManager with a flow kind.** This could reuse its task registry,
   but requires flow-aware admission, journals, completion and recovery across
   existing job consumers. The launch intent is still necessary; native chat
   or native model-job assumptions cannot stand in for an ACP flow.

The gateway exposes a small `admit/status/close` contract. The intent store
owns operation identity and transitions. The compiler owns the validated plan;
the flow service/engine own execution. New shared wiring belongs in a dedicated
service or `agents/setup.py`, not copied into each HTTP, CLI or runtime interface.

## HTTP contract and authority

After approval and conformance, `POST /api/scenarios/start` accepts only:
`operation_id`, installed `starter_id`, bounded `inputs`, allowlisted integer
`workspace`, `preview_digest`, and `authority_digest`. The operation ID is a
canonical UUIDv4, and both digests are lowercase SHA-256 hex strings. Enforce
the existing HTTP body limit and installed field bounds. Use unique-key JSON;
reject unknown fields, booleans as indices, raw paths, definitions, executables,
cross-project grants and unsupported run options before execution.

P3 preview adds a separate server-authority envelope outside `LaunchPlan`:
canonical workspace identity, operator ceiling and effective step permissions,
allowed role/runtime scope, and available ownership/approval capabilities.
Its digest supplements the existing CLI/HTTP-identical plan digest. Neither
digest is a grant or cryptographic evidence of a human gesture. Authorization
comes from the protected write request and the operator's actual policy.

Request admission validates token/Host/Origin. Launch revalidates its captured
authorization scope against the current server epoch/policy without persisting
or forwarding raw credentials. Both enforce write enablement,
the current canonical allowlist entry and device/inode, private-store access,
installed schema, effective trusted sources, role bindings and policy. Run-kind
requests refuse until #316; flow options absent from its supported contract
refuse. Unknown ceiling modes or unavailable approval/cleanup capabilities refuse.
The UI capability selector cannot bypass the same server enforcers.

For a new operation, compile from the exact frozen source bytes and compare both
digests before persisting admission. The same resolved flow, profiles, registry,
role authority and selected input bytes feed execution. Do not call the current
reload path after comparison or silently accept a different fallback identity.
The common launch owner applies the ceiling to effective prepared permissions,
including inherited profile settings. A no-edits step stays readonly/deny-all;
an unsupported ACP authority posture refuses rather than broadening it.

Configured preview remains non-executing. Any actual executable/version/login
probe at launch is owned execution: it occurs only after durable authorization
and engine admission, under the admitted operation's supervision. Use official user-authenticated
CLIs/public APIs; do not read, persist, copy or proxy vendor OAuth tokens.

## Durable admission and at-most-once dispatch

`scenarios/launch_intents.py` uses a private store under the selected session
store. Validate every path component through owned directory descriptors;
files/locks are owner-only regular files, with no symlink following or unlocked
fallback. Read at most 64 KiB plus one overflow byte from the validated descriptor;
reject oversized version-1 records before parsing, reject duplicate
keys/unknown fields/future versions and validate legal transitions. Bound lock
and filesystem work through owned workers so cancellation cannot abandon it.

An intent contains operation ID, canonical request/plan/authority digests,
workspace and store identity, compiler/catalog version, reserved random parent
UUID, server epoch/process identity, transition sequence and typed lifecycle
references. Persist no credentials, raw config, task, source contents, transcript
or large output. The ordinary session retains its existing authorized evidence.
An allocated UUID is not a created session or a workspace lease.

Serialize read-check-write under the cross-process lock. An existing operation
with the same immutable request identity returns its recorded run or explicit
admission/uncertainty state; it never schedules a second task. Different digest,
inputs, workspace or authority reuse refuses. Authenticate and check the current
operator scope before returning an existing record. Do not recompile a completed
operation into a different run because configuration later changed.

| Phase | Meaning | Duplicate or restart behavior |
|---|---|---|
| `prepared` | Durable identity, no launch authority yet. | Live original owner may continue; duplicate never schedules. A dead owner becomes abandoned/refused; restart never dispatches it. |
| `launch-authorized` | Durable permission to enter owned execution; work may have begun. | Reconcile existing identity/evidence or uncertainty. Missing PID/session does not prove no launch. |
| `admitted` | Engine owns its lease and durably created the bound parent session. | Return that session; no second admission. |
| `terminal` | Existing runtime terminal evidence and proven cleanup/publication are bound. | Return that evidence, keeping review and verification separate. |
| `refused` / `abandoned` | This operation cannot execute further. | Return its refusal; a new explicit preview/operation is needed for a new attempt. |
| `uncertain` / `quarantined` | Launch, cleanup, identity or publication cannot be proven. | Read-only projection; retain ownership/quarantine where applicable, never replay. |

The original server owner persists `prepared`, registers one owned task, then
persists and fsyncs `launch-authorized` before any execution or probe. A failure
to confirm that write authorizes nothing. Post-rename/fsync failure preserves
potentially published records for reconciliation; it is never assumed rolled back.
Holding the store lock never waits for a runtime or browser approval.

The flow service enters explicit new-run mode with the reserved UUID. The engine
acquires the sole source-workspace lease, creates the parent exclusively and
publishes a durable operation/session/lease-epoch binding before any step starts.
Collision, busy workspace or publication failure starts no step and cannot
overwrite another session. Snapshot member leases/capacity remain with their
existing isolated owners; the gateway opens no chat or competing source lease.

An admission notification allows a response before flow completion. Return the
session only after its durable binding is validated. A bounded response wait
may instead return an admission-pending or uncertain operation, explicitly
without claiming a session exists. A transport timeout or lost response never
cancels the owned task; a retry with the same operation returns its existing
state. Keep the operation ID until that result is resolved. There is no exactly-once
success promise: refusal/uncertainty may mean no completed run, never a silent retry.

## Process ownership, approvals and shutdown

The gateway owns the operation task; the engine owns the workspace lease;
native/ACP/snapshot services own their admitted resources and reservations.
Pass a typed execution context with ceiling, frozen authority, owner/session
identity and approval-handler references through sequential, review, retry,
fallback, consult and parallel paths. It is not a caller-supplied definition.
Native asks use the existing parked broker; ACP asks retain their durable
broker/audit path and bridge to the dashboard answerer. Timeout, loss of browser
heartbeat and server cancellation deny outstanding asks. Restart never revives
an approval or treats an old answer as a new grant.

A disconnected tab leaves server-owned work running; a denied unanswered tool
may still cause the runtime to stop. Server shutdown first stops admission and
HTTP intake, denies outstanding asks, cancels owned flows and drains their real
runtime/helper owners. Metadata publication and bounded filesystem workers must
finish or yield explicit uncertainty before the source lease can release.

Leader exit, a quiet process group, successful `StepResult`, cancelled Future,
or absence of an observed PID is insufficient closure. Require the common
native/ACP descendant evidence proposed in #326, including no-edits confined
steps, detached children, MCP/background tools, parallel members and snapshot/Git
workers. The engine retains quarantine when any owner cannot prove closure;
ordinary `finally` release cannot undo it. No broad process-name matching or
foreign lease/PID killing is a recovery path.

The recommendation is Linux supervision first, with actual local descendant
conformance; other platforms expose Start only after an equivalent owned-closure
backend passes. This platform choice requires approval. Until the mechanism is
implemented and proven for the selected flow's runtimes, Start remains unavailable
on that platform. Guardrails are not a sandbox boundary. Supported Docker or
other isolation keeps its existing meaning; there is no automatic host fallback.

On restart, inspect intents, immutable operation binding, actual engine journal,
strict metadata and issuer/process/lease identities through bounded read-only
reconciliation. Valid matches project an existing session; disagreement, corrupt
records, vanished publication or unproven cleanup project uncertainty. Respect
a live original owner and sticky quarantine. Never invoke `run`, `resume`, a
launcher, an approval answer or lease recovery merely to repair an HTTP response.
An intact prepared record permits an abandoned classification only with a
verified dead owner and no contradictory execution/binding evidence; disagreement
is uncertain. Missing records or PIDs never prove that execution did not begin.
Prepared and launch-authorized records are not garbage-collected into permission
to reuse an operation ID. Expiry alone proves neither no launch nor safe cleanup.

## Browser behavior and evidence

Flow Start is available only for a fresh preview with a supported server
capability. Display the operator ceiling and effective step posture. One explicit
Start creates and retains an operation ID; concurrent clicks/retries reuse it.
Editing inputs invalidates the preview. Changed evidence refuses with a fresh
preview action; an unresolved previous operation remains visibly unresolved.

Follow the existing parent session/detail and byte-offset event/journal views
after admission. Pending admission is distinct from queued/running/completed work.
The intent projects boundary status only; it is not a second progress store.
Run-kind cards retain working copy-command. Results keep configured/actual
identities, review/waiver, process/work/outcome, historical verification,
supplied sources and selected-session coverage distinct. No P1b check or cost
claim is fabricated. Implement-plan remains an explicit new preview and action.

## Validation and release gate

Implementation requires observable owner-boundary evidence, not JS snapshots or
fixtures that manufacture publication/closure receipts:

- Concurrent same-operation HTTP requests across threads/processes observe one
  real local flow/child counter; mismatched reuse launches nothing.
- Production transition interruptions cover prepare, authorization, engine
  lease admission, session binding, first child, lost response and final publication.
  Each duplicate/restart yields the same run or uncertainty, never another launch.
- Token/Origin/Host, write mode, empty/remapped/symlinked workspaces, changed
  source/config/profile/manifest/artifact, unknown schemas, lock/storage failures,
  permission escalation and unsupported kind/options refuse at the enforcer.
- A competing lease produces no parent session; runtime/snapshot branches retain
  their real capacity/borrowing/approval semantics. Native and ACP ceilings and
  readonly refusals are exercised through the shared prepared launch owner.
- Actual detached writers and busy/helper workers prove teardown or quarantine
  before release, including cancellation and server shutdown. PID reuse and
  foreign/stale ownership never authorize a signal or replay.
- Live Chrome against the real server covers preview/Start, duplicate action,
  stale evidence, approval, result/progress, tab disconnect and shutdown/restart.
  Unsupported platforms exercise explicit unavailable behavior; positive lifecycle
  evidence must come from the platform whose closure backend is claimed.
- Run narrow affected owner tests, full pytest, Ruff and documentation checks;
  report live vendor/Docker/tmux/Harbor/OS coverage accurately. Update adopted
  architecture/reference/roadmap and durable decisions only when behavior lands.

Approval must select the gateway approach and platform rollout, and accept the
shared strict-admission/authority/cleanup prerequisites. P3 approval alone is
not evidence that those prerequisites exist. #315 stays open until its actual
flow-start acceptance passes; #316 remains a separate run-kind gate.
