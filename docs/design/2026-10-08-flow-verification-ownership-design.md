# Flow verification ownership, evidence, and recovery

**Status: proposed; awaiting design review.** This record implements the design
deliverable for [#311](https://github.com/Darshan2104/Garuda-openagent/issues/311),
not the runtime behavior in #312 or #313. Neither `flow run --check` nor automatic
`build-review` checks exist on the inspected main. Approval must resolve the
review decisions below before P1b code begins.

## Baseline and scope

Inspected main: `2414886d1aa6c969ead753221024c6699798cbd3` (2026-10-08), after
PRs #318–#325 and #327 merged. P0, P1a and P2, including the reconnect example
and read-only dashboard, are landed; #303–#310, #314 and #317 are closed.
The flow, acceptance, lease, runtime and metadata owners below are unchanged
from the original `b9a60c12a8d4df291dfe94c22f1fc8978080b189` inspection.
The accepted source is revision 5 of
[ready-to-use workflow scenarios](../plans/2026-10-06-ready-to-use-workflow-scenarios.md),
especially §5.3 and §6.3. Its SHA-256 is
`d39a83cb30a2149b273194fcbeab0bed6652be6d8697819fd65c9d886d06dca9`.

This design preserves those contracts and identifies the additional mechanisms
needed to satisfy them. It proposes no second flow engine, automatic repair,
background-flow support, new check configuration keys, or Docker-check host
fallback. P2 read-only starter browsing landed independently of P1b; its UI
continues to show flow verification as unavailable. P3 launch remains separately gated.

## Confirmed owners and gaps

Paths below refer to the inspected main, not implemented P1b APIs.

| Current owner | Confirmed behavior | P1b change or constraint |
|---|---|---|
| `flows/service.py::FlowExecutionService` | Resolves effective configured/packaged flow and missing roles; constructs the runner; reloads recorded review/state afterward. No checks parameter. | Freeze check request and authority before admission; configure engine finalization once. Never call acceptance after the runner returns. |
| `flows/engine.py::FlowRunner.run` | Acquires one parent lease, starts heartbeat, delegates/revokes sequential step capabilities, writes terminal state, releases in `finally`. Lease mode follows steps' no-edits posture. | Explicit checks require a mutating parent lease. Capture evidence under it; race the owning execution against heartbeat; finalize verification before release. |
| `flows/engine.py::_run_step`, `_review_loop`, `_run_group` | Durable step intent/immutable receipt; retries retain attempts; parallel members use a detached snapshot. | Account for every attempted step and member. A successful receipt alone cannot establish cleanup or full changed-path evidence. |
| `flows/launch.py::launch_step` | Native and ACP launches borrow the parent owner; `StepResult` contains only session ID, success, and output. | Carry structured cleanup/evidence references from the real launch owner. Missing evidence cannot default to clean. |
| `interfaces/run_guard.py::WorkspaceLeaseGuard` | Issuing-owner validation, heartbeat/race, revocable borrowing, sticky quarantine. `stop_heartbeat()` retains ownership; later release cannot undo it. | Preserve this owner. Verification failure is a verdict; uncertain cleanup or publication retains quarantine. |
| `workspace/evidence.py` and `workspace/diff.py` | Persisted baseline; readable Git-derived delta; unsupported attribution stays explicit. Child `finish_session_evidence()` clips metadata path lists at 200. | Reuse full delta objects for cumulative accounting, not the clipped metadata. Persist completeness and attempt coverage separately. |
| `core/acceptance.py` | Authority selection, candidate fingerprint, per-command infrastructure guard, tree-changing-check void receipts, aggregate verdict. `run_check()` uses synchronous `subprocess.run`. | Reuse policy through an async owner; preserve synchronous entry points and their existing callers. |
| `config/garuda_yaml.py`, `config/project_trust.py` | Effective check provenance; exact-byte project trust; untrusted checks withheld. `Resolved` retains provenance labels, not source-byte snapshots. | Resolve and freeze from the same bytes used for trust/parsing. Add an internal source snapshot boundary rather than hash a second read afterward. |
| `runtime/session_state.py::finished` / `interrupted` | Build fresh state with unavailable verification by default. | Flow publication must explicitly retain the validated acceptance verdict; ordinary synchronous callers remain compatible. |
| `core/sessions.py::merge_meta`, `SessionStore.mutate_meta` | Metadata replacement is atomic; the sidecar lock can fall back to unlocked writes, and `_atomic_write_text()` does not fsync file or directory. Corrupt metadata may be rebuilt from partial updates. | New P1b flow metadata needs strict locking, durable publication and corruption refusal at this same shared writer boundary; atomic replacement alone is insufficient. |
| `flows/engine.py::recover` | Missing step receipt becomes quarantine; receipted steps skip on resume. No check-intent reconciliation exists. | Reconcile verification independently; completed steps do not authorize a missing check phase. |

The CLI's current synchronous acceptance owner is
`interfaces/main.py::_accept_session`, which calls `accept()` for role runs.
`garuda run --check` remains on that path. This proposal does not claim that the
synchronous path already has the new ownership or recovery guarantees.

Cleanup is a material gap. Native teardown attempts to reap registered
background tasks, but `runner.py` currently treats a reap exception as an empty
survivor list and logs some teardown failures. ACP `AcpProcess.close()` waits for
the leader and signals its group when the leader is still running; it supplies
no durable whole-subtree proof. `runtime/recovery.py` explicitly excludes
descendants that left the group. P1b cannot translate these facts into
`descendants_stopped: true`.

## Approaches and recommendation

| Approach | Benefit | Failure or cost |
|---|---|---|
| **Owned async acceptance adapter inside `FlowRunner` (recommended)** | Heartbeat remains responsive; one parent owns steps, checks, receipts, and release; async execution shares existing verdict policy. | Requires durable intent, process supervision, complete evidence, and recovery decisions. |
| Run synchronous acceptance in a thread while holding the lease | Small initial wiring change. | Cancelling the future does not terminate the check or descendants; no reliable launch/cleanup reconciliation. Does not meet §5.3. |
| Run checks in the outer service after the flow returns | Keeps the engine unchanged. | Ownership has already been released; a competitor can change the candidate. Does not meet §5.3. |

Use a focused `core/acceptance_runner.py::AcceptanceRunner` for async check
execution. Extract only pure receipt/verdict policy needed for reuse from
`acceptance.py`; preserve `accept()`, `run_check()`, and synchronous command
behavior. Put process supervision behind a shared internal protocol, so native
and ACP flow launches can provide compatible cleanup evidence without each
interface inventing a new finalizer.

## Request, authority, and admission

The proposed internal request consists of immutable check definitions, their
ordered IDs and authority, source snapshots/digests, and the effective check
permission ceiling. CLI check strings are `user-request`; configuration checks
use existing `user-config` / `trusted-project` provenance. Deduplication and
ordering follow the existing resolver. Agent-suggested checks are excluded from
acceptance execution and can only contribute self-check evidence.

Snapshot configuration bytes while resolving them. Project checks must be parsed
from the exact bytes accepted by `project_trust`, with the trust key retained;
user configuration must be frozen from its corresponding parsed bytes. Persist
digests and redacted definition summaries, not unrelated configuration or
secret environment values. Keep exact frozen definitions in the live request;
reconstruct configured definitions only from matching authorized source bytes.
An explicit CLI definition that cannot be safely recovered requires the user
to supply the matching request again for a never-launched attempt. Check identity includes normalized command/argv,
cwd, environment-definition digest, mode, authority, and source identity.

Freeze before acquiring the parent lease. Immediately after admission and before
each launch, revalidate sources, trust grant, effective permission ceiling, and
workspace/cwd binding. Any mismatch refuses execution or invalidates an already
started verification phase; do not quietly replace definitions. Trust cannot be
created by a headless flow. Symlink/containment or unreadable-source ambiguity
refuses. A check cannot acquire extra permissions from its environment, task
text, or an agent recommendation.

The check phase is a separate user-authorized phase: a planning step remains
no-edits even when the user explicitly requests checks afterward. That request
requires a **mutating parent lease** because host checks can write. Planning
starters never select checks automatically. Denials in the effective parent
check ceiling still win; a policy the check backend cannot support leaves the
check unavailable. Permission checks remain guardrails, not confinement or a
proof about arbitrary shell behavior.

No selected checks means unavailable verification and ordinary flow completion.
`build-review` selects only trusted effective checks after #313 lands. A Docker
check without a supported confined backend produces an unavailable receipt and
never executes on the host.

## One owning lifecycle

1. Resolve flow/roles and freeze the check request before admission. Begin the
   parent record, acquire the required lease, start heartbeat, and persist the
   parent baseline before any step dispatch.
2. Execute steps through the existing engine, racing work against the parent
   heartbeat. Persist evidence for each attempt and retry, including parallel
   source checks and each member's cleanup reference. Revoke step capabilities
   only after their launch owners settle; revocation does not prove cleanup.
3. Before verification, require all required terminal step receipts, complete
   cumulative evidence, no interruption/quarantine, and confirmed cleanup for
   every runtime that could still write the candidate. Missing cleanup proof
   with possible survivors quarantines ownership; a backend declared unsupported
   before launch cannot be substituted with weaker proof afterward.
4. Persist cumulative parent evidence. Pin the final candidate fingerprint after
   these checks, not at the final model message or before child teardown.
5. Await `AcceptanceRunner` under the **same lease and heartbeat**. Run checks
   sequentially, against that candidate, with bounded execution and output. A
   mismatch stops further checks; a tree-changing check voids its receipt.
6. Publish immutable check receipts and the verification commit, then atomically
   publish terminal parent metadata referencing them. Release last. Quarantine
   is sticky even if outer `finally` blocks attempt release.

Outcome, review, self-check, and verification remain separate. Completed steps
can coexist with failed or unavailable verification. Review approval is never
a passed check. No incomplete/interrupted/quarantined flow starts checks.

## Cumulative evidence

The parent baseline is captured under its lease and persists for the entire
flow. Each attempt has a before/after evidence segment captured around the real
launch owner, after cleanup. Union full attempt deltas, parent-baseline deltas
at each boundary, and parallel source-change evidence. Include coder retries,
reviewers, planners, and failed attempts; snapshot-member edits are distinguished
from edits to the parent workspace. Persist the sorted cumulative parent
`delta_changed` and the separate final delta.

For example, coder attempt 1 changes `conftest.py`; a later coder attempt restores
it. The final delta may omit it, but the first attempt's segment keeps it in the
cumulative set. `infra_changed()` receives that cumulative set for every check.
Preexisting dirty files unchanged by the flow remain preexisting, not attributed
to the flow. Rename evidence includes both relevant source and destination paths.

Persist a private versioned manifest with baseline identity, segment IDs/digests,
attempt coverage, attribution, cumulative paths, and `complete`. Use full
`SessionDelta` data before display clipping; never reconstruct it from children's
200-path metadata summaries. Bound the manifest at 100,000 paths and 16 MiB;
overflow, unreadable segments, unsupported attribution, missing attempts, or a
torn record sets incomplete evidence and withholds verification. Display lists
may be clipped only with explicit coverage information. No truncated list can
claim completeness.

This is boundary-based filesystem attribution, consistent with the existing
delta owner. It does not observe a write restored **within the same step** before
any evidence boundary, ignored-file writes, or every metadata mutation. That
limit must be explicit in review and shipped documentation. If #311 is intended
to require an audit of all transient writes, a filesystem-event mechanism is a
separate prerequisite; endpoint deltas cannot fulfill that stronger claim.

The candidate uses the existing acceptance fingerprint semantics, including
their Git/dirty-file scope. It is not a full host filesystem attestation.
Unmanaged writers and malicious same-user rewriting of all private records are
outside the cooperative lease/digest guarantee. A read-only Garuda session may
share the workspace; a second mutating session must be refused until publication
and release finish.

## Async process ownership and bounded teardown

Proposed internal boundaries (names are not public APIs):

| Boundary | Input | Output / responsibility |
|---|---|---|
| Check resolver | Workspace, effective configuration, explicit CLI checks | Immutable authorized request and source/ceiling binding. |
| `AcceptanceRunner.run` | Request, pinned candidate, complete flow evidence, live parent capability, store | Ordered immutable check receipts and candidate-bound verification; never acquires/releases the parent lease. |
| `ProcessSupervisor` protocol | Owned launch ID, immutable command request, cancellation/deadline | Actual process identity, bounded execution, cleanup evidence; unknown cleanup remains unknown. |
| Flow publisher | Step outcome, review, receipt manifest, verification commit | One atomic parent metadata update preserving separate fields. |
| Reconciler | Persisted intent/activation/receipt/commit, current owner facts | Classification and permitted next action; no uncertain automatic launches or ownership takeover. |

Each check persists a private, fsynced `prepared` intent before process creation.
Persist `launch-authorized` **before** spawning or sending an activation permit.
Only that state permits execution. Record the supervisor and command PID/start
identity/process group as soon as known; publication failure after possible
launch triggers teardown or quarantine, never a fabricated never-launched state.
The pre-spawn authorized window is deliberately ambiguous after a crash.

Defaults proposed for the internal runner: 900 seconds per check, 3,600 seconds
per phase, 3 seconds TERM grace, then 3 seconds to establish cleanup after KILL.
These are hard bounds that trusted policy may narrow; do not add schema keys in
#312. Continuously drain both output streams into bounded ring buffers, apply
the existing redaction/tail policy, and keep no unbounded raw-output file. Store
at most the existing 2,000-character redacted tail per check. Closed stdin and
the existing string-shell / argv execution distinction remain.

Baseline/delta/fingerprint reads, lock acquisition, and fsync also must not block
the event loop. Existing `capture_baseline()` performs synchronous Git calls
with per-call timeouts and file hashing; putting only the check command on the
async path would leave a heartbeat stall. Run these probes/publication operations
off the loop through a bounded owned worker, await settlement before release,
and preserve the existing fingerprint/delta semantics. Git probes must disable
repository-directed helper execution (such as fsmonitor) and optional index
writes. A probe worker that can still spawn or write after cancellation requires
cleanup evidence or quarantine, just like a check. An observation timeout is not
proof that a worker stopped.

Timeout, caller cancellation, heartbeat loss, or store failure requests owned
teardown. Shield only bounded cleanup from repeated cancellation. Signal only
matched owned identities; never signal an unrelated reused PID. Leader exit,
EOF, a cancelled future, or a successful signal syscall is insufficient proof.
Publish a terminal cleanup receipt only after the supported backend establishes
that the owned writers are stopped. Grace expiration, probe failure, supervisor
death, missing closure evidence, or ambiguous identity quarantines the parent.
Cancel can end with unavailable verification and cancelled outcome after proved
cleanup; timeout can produce a failed check after proved cleanup.

### Proposed platform contract requiring review

For the initial strong host backend, recommend a dedicated **Linux subtree
supervisor**, established as a subreaper before it launches any command. Keep it
outside the command's process group. Drain/reap ordinary children and adopted
orphans, including session/group-detached helpers. During teardown, stop the
original group and continue identity-bound cleanup of adopted children until
the supervisor has no remaining children; the supervisor may not fork unrelated
work or auto-ignore `SIGCHLD`. Persist closure evidence before it exits.

Linux documents orphan adoption by the nearest live ancestor subreaper in
[PR_SET_CHILD_SUBREAPER](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html).
The [wait manual](https://man7.org/linux/man-pages/man2/waitpid.2.html) documents
waiting for children and `ECHILD`. Using those semantics to establish subtree
closure is this design's inference; #312 must prove it with real detached-child
tests. A host check requesting work from an unrelated preexisting daemon remains
outside owned descendants; this is process supervision, not sandbox confinement.

Do not set subreaper state on the shared Garuda process, which has other runtime
children. Do not claim arbitrary macOS subtree proof from repeated `ps` polling
or `killpg`. Initial macOS/other-host flow checks remain unavailable until an
equally supported supervisor backend exists; existing synchronous checks remain
usable. Docker check execution remains unavailable in this increment.

Flow-step closure needs the same honesty: borrowing a lease does not supervise
all step descendants. #313 must integrate structured cleanup evidence at the
shared native/ACP launch owners, including relevant tool children, before
marking such a flow eligible for checks. Runtime configurations that cannot
provide it are unsupported for P1b verification. Uncertain already-launched
step cleanup retains ownership. This prerequisite increases #313's scope and
must be accepted or split into an explicit prerequisite issue during review.

## Durable records and publication order

Store records below the parent's private session directory, not the workspace
or generated `.context/`. Use the strict store's no-follow, owner-only,
descriptor-bound, fsynced publication patterns; immutable receipts must appear
complete or absent. Validate schemas, containment, owner/flow/workspace/epoch
binding, and digests on read. Local digests detect inconsistency; they are not
signatures against a writer with control of the whole store.

An intent contains version, flow/check/attempt IDs, redacted definition summary and
authority/source/ceiling digests, workspace and parent ownership binding,
candidate fingerprint, evidence-manifest digest, limits, and phase. Process
records add actual supervisor/command identities and activation history.
Receipts add exit/timeout/cancel facts, bounded redacted output, before/after
fingerprints, authority revalidation, cleanup reference, and existing policy
status. Raw environment secrets never enter intent or receipt records.

The runner evaluates each check against the fixed final-step candidate. Check
authority and candidate are revalidated before launch and after teardown, and
again before final publication. A tree change voids the check and prevents
later checks from verifying a replacement candidate. Infrastructure guard and
aggregate precedence remain those in `acceptance.py::verification_from`;
an explicit candidate/authority mismatch invalidates the phase even if an
earlier receipt passed. Unsupported checks cannot yield a partial pass.

Publication is ordered, not a fictitious atomic transaction across files:

First establish a versioned private P1b publication-policy marker before creating
the parent metadata. Every shared metadata writer for that session must recognize
the marker independently of readable metadata. Use one common sidecar lock
(`meta.json.lock`), with no-follow owner-bound descriptors and fail-closed lock
acquisition; do not add a second lock that ordinary writers ignore. Refuse corrupt
or replaced metadata instead of rebuilding it from partial updates. Fsync the
complete temporary metadata file, atomically replace it through the retained
directory descriptor, and fsync that directory. Adapt the existing shared
session writer to this strict flow policy rather than write metadata behind its
back. Legacy sessions retain their existing API/receipt semantics. Later tag,
link or observability updates to a P1b flow must use that same policy and preserve
the committed verification fields.

1. Fsync complete cumulative evidence and every terminal check/cleanup receipt.
2. Publish an immutable verification commit referencing the exact candidate,
   authority request, evidence, ordered receipt digests, and derived verdict.
3. In one locked atomic parent-meta mutation, publish `acceptance_receipts`, the
   commit reference, final `flow_state`/legacy `status`, and validated state.
   Explicitly preserve the committed verification in that state. Review and
   self-check remain their own evidence.
4. Release parent ownership only after this publication succeeds. A publication
   failure keeps the lease quarantined even when processes are proven stopped;
   retain the evidence for operator reconciliation.

For the new versioned P1b flow records, consumers count verification only when
the commit and referenced evidence agree with parent publication. Existing role
acceptance receipts keep their schema and interpretation; legacy/P1a flows stay
unavailable. A receipt written before parent publication is evidence
to reconcile, not permission to display a passed flow. An old `finished()` call
must not overwrite the committed verdict during resume/finalization.

## Restart and reconciliation

Read/classify before changing state. A live/unknown issuing owner or retained
quarantined lease cannot be replaced by a fresh guard. Reconciliation never
signals processes from a bare PID, launches uncertain work, or releases a
foreign lease. This increment does not invent an automatic quarantine override.

| Persisted condition | Verification / ownership | Permitted action |
|---|---|---|
| Complete prepared intent; no launch authorization; owner settled | Never launched; unavailable | On explicit resume, revalidate original sources, candidate, complete evidence and lease authority; create a new attempt. Absence of PID alone is insufficient. |
| Launch authorization, including crash before PID publication; no terminal receipt | Execution possibly happened; unknown cleanup | Quarantine; no automatic replay, even if the recorded leader is absent. Inspect evidence and use explicit operator recovery. |
| Launched with matching terminal receipt and cleanup proof; no parent commit | Check happened; publication incomplete | Reconcile existing evidence only after candidate/authority and ownership validation. Repair publication; never rerun that check. |
| Complete commit and matching parent publication | Recorded terminal verdict | Return that evidence; do not execute checks again. A different live candidate is stale/invalid for a new run, not retroactive proof of it. |
| Completed steps; no verification request/intent | Step completion only; unavailable | Legacy/P1a flow stays unavailable. A fresh explicit request creates new work, not retroactive verification on resume. |
| Frozen P1b request; complete steps/evidence/cleanup; no first check intent or authorization | Verification pending; not executed | On explicit resume, prove the complete record chain and unchanged continuation candidate/authority under valid ownership, then create the first intent. Do not infer never-launched merely from a missing PID or unreadable journal. |
| Interrupted/quarantined steps or incomplete cumulative evidence | No eligible candidate | No checks; retain quarantine where cleanup is uncertain. Missing evidence cannot be regenerated by a fresh baseline. |
| Cancelled verification, proved cleanup and receipt | Cancelled outcome; unavailable verification | Publish cancellation evidence before ordinary release. Resume does not auto-rerun cancelled checks. |
| Cancelled verification, uncertain cleanup or lost receipt | Cleanup unknown | Retain quarantine and do not replay. |
| Authority/candidate changed, corrupt/torn/foreign record, unsupported version | Invalidated or unavailable with diagnostic; never passed | Stop execution/publication; preserve original records. Cleanup still belongs to the live owner; uncertainty retains ownership. |
| Owner died or supervisor vanished during any active phase | Death does not prove descendants stopped | Retained lease blocks fresh mutation; reconcile receipts only. No automatic reclaim or check replay. |

Resuming partially completed steps with a frozen check request additionally
requires the original parent baseline and complete prior segment chain to match
the recorded workspace at the continuation boundary. Do not use the ordinary
evidence inheritance fallback to a fresh baseline to make earlier flow edits
disappear from verification. Fail closed when continuity cannot be proved.

## Wiring and delivery order

- **#311:** review this design, record the decisions below, and link the reviewed
  revision in the issue. A documentation merge alone does not complete the gate.
- **#312:** implement the internal runner, minimal shared policy extraction,
  strict intent/receipt/reconciliation records, and supported process supervisor.
  No flow flag, starter marketing, or change to synchronous callers.
- **#313:** integrate step cleanup/evidence prerequisites; freeze requests in
  `FlowExecutionService`; finalize inside `FlowRunner`; preserve verification
  in atomic publication and resume. Only then add `checks=()` to the service,
  `flow run --check`, and trusted automatic checks to `build-review`.

CLI, starter execution, and later HTTP use that single shared service. Configured
same-name flows still override packaged ones; existing reviewer independence and
`max_rounds: 2` (at most three pairs) stay unchanged. Update previews/cards and
CLI/reference/features/use-case documentation only when behavior lands. Record
the adopted public/security contract in `.context/decisions.md` with P1b code,
not merely because this proposed document exists.

## Owner-level validation required before shipping

These are acceptance obligations, not tests added by this design-only PR. New
tests must use real owners and an observable failing control; avoid replacing
the very lease/process/evidence boundary being tested with a fixture verdict.

| Owner / change | Observable acceptance control |
|---|---|
| #312 async execution | Real check writes an entry marker then waits. Heartbeat and an independent event-loop task progress while it runs; releasing a thread/future cannot satisfy cleanup. |
| #312 supervisor | Shell leader exits while a helper writes later; also double-fork/`setsid` helper and TERM-resistant descendant. No terminal pass/release until all supported owned writers are stopped; forced unknown probe/supervisor death retains quarantine. |
| #312 cancellation/bounds | Cancel the real running check after its entry marker; verify writers stop before ordinary release. Huge stdout/stderr stay bounded without deadlock; timeout and cleanup have separate limits. |
| #312 recovery/identity | Fault after authorization, after spawn before PID publication, after cleanup before receipt, and after receipt before commit. Lost possibly-launched receipt never replays; PID reuse/foreign records never signal. |
| #312 synchronous compatibility | Existing `tests/test_acceptance.py` still proves authority, fingerprint, infrastructure guard, void receipts, redaction and aggregate behavior. Existing role-run checks continue on synchronous acceptance. |
| #313 lease enforcer | A genuinely separate mutating session tries the same workspace while the actual adapter's check is blocked and during publication. Refused until commit/meta publication and release; admitted afterward. Injected unknown cleanup keeps it refused. |
| #313 cumulative evidence | Earlier coder attempt changes `conftest.py`, later attempt restores it; passing pytest check remains unverified. More than 200 paths puts relevant infrastructure beyond the child's display clip; complete parent evidence still catches it. |
| #313 candidate/authority | Actual check changes the tree; external mutation or trust/source/ceiling change between pin, launch, cleanup and commit invalidates verification. An untrusted project check never writes its launch marker. |
| #313 flow/step eligibility | Stopped/rejected/interrupted steps start no checks. Missing native/ACP cleanup proof or attempt evidence cannot be upgraded by `success=True`, no-edits output, or a final Git diff. Cover retries and parallel members. |
| #313 publication/resume | Failed checks coexist with completed step outcome and approved review; final publication preserves failed verification. Restart at every publication boundary uses existing receipts and never treats finished steps as passed checks. |
| #313 metadata durability | A concurrent real metadata writer uses the same lock. Inject lock, file-fsync, rename, directory-fsync and corrupt-record failures: no unlocked/partial-update fallback, no ordinary lease release, and no successful finalizer return. Preserve already-published records for reconciliation rather than pretend a failed fsync undid a rename. Later tag/link updates retain committed verification. |
| #313 product wiring | Production service/CLI actually invokes the runner with frozen authorized checks; configured flow override and review-round bounds remain. Planning has no automatic checks; explicit planning checks use a mutating parent lease. |
| Supported-environment limits | Linux real descendant controls pass before host support is claimed. macOS/unsupported mode or ceiling produces unavailable without launch or host fallback. Optional Docker/vendor/OS integrations are disclosed separately. |

Use a disposable mutation that removes the parent-owned finalization/release
barrier, cumulative union, or unknown-cleanup refusal to establish that the
corresponding owner test fails. Do not add a parallel production seam solely to
make tests pass. Run focused checks first, then repository pytest/Ruff/docs gates
on each implementation PR.

## Decisions needed to complete #311

1. **Platform and cleanup support:** approve the Linux-subtree-supervisor-first
   proposal, with flow checks unavailable on macOS until an equivalent backend
   exists, or require that backend before the public #313 release. Process-group
   polling alone is not proposed as equivalent evidence.
2. **Step closure prerequisite:** accept the shared native/ACP cleanup-evidence
   work in #313, or split it into an explicit prerequisite issue before wiring
   verification. Existing launch return values do not meet the proof contract.
3. **Evidence scope:** confirm cumulative boundary deltas (including changes
   restored by later steps) as §5.3's requirement. If transient writes within a
   single step must also be retained, require event capture before P1b code.

Recommended decisions: Linux first; split shared step-closure work if its
implementation cannot stay focused within #313; retain the current delta owner's
boundary-based scope and disclose it. These are proposals, not recorded approval.
Until review records all three choices, #311 remains open and #312/#313 remain
behind the design gate.
