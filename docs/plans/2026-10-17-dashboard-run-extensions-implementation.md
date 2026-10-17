# Dashboard run extensions implementation (#337)

Approved scope: [design](../design/2026-10-17-dashboard-run-extensions-design.md).

1. Select worktrees in the run parent. Separate source authority/execution
   identity through engine, binding and projection; persist worktree metadata,
   settle/discard refused creation. Add real Git boundary tests. Self-review.
2. Add issued read-only lifetime snapshots/delegation. Share unchecked native
   asks; keep acceptance snapshots mutating-only. Exercise concurrent owners,
   forgery and withholding. Self-review.
3. Own exact ACP confinement container lifetimes. Supervise preflight/execution,
   verify removal, quarantine ambiguity. Enable only after positive/negative
   owner tests. Self-review.
4. Freeze fallback candidates and supervise probes. Enforce selected ceiling,
   persist selection before prompting, preserve skip semantics and one prompt.
   Capture every candidate's sources at compilation and reconcile owned role
   preparation against those admitted facts, including configuration changes
   during a probe, before any selected runtime starts.
   Enable only after owner tests. Self-review.
5. Add strict v2 intents and reserved background admission. Bind metadata before
   enqueue, delegate capacity to the owned worker, reconcile without dispatch,
   project worker state, retain durable approvals/Stop. Test crash boundaries
   and actual detached execution. Self-review.
6. Add Chrome role-start fixture/check to browser runner. Update architecture,
   reference, workflow, roadmap, backlog and durable decisions as behavior lands.
   Run full checks and review every #337 requirement before opening the PR.

The writing-plans skill is absent from the available local roots; this plan
records the implementation sequence directly.

## Implementation review: workspace and native ask ownership

Steps 1 and 2 are implemented. Review verified source/target identity separation,
frozen source profile use even when uncommitted profiles are absent from the
worktree, selected checkout closure binding, child workspace metadata, settled
cancellation cleanup and duplicate projection. Native asks are shared only when
their prepared permissions are readonly; acceptance keeps exclusive ownership.

Owner-boundary tests passed on macOS and in Linux Docker with `--init` (required
to give the test owner a process identity/group above PID 1). Real native/ACP
child closure, worktree checks, shared asks, checked-ask refusal and existing
lease/verification regressions ran. Ruff and documentation checks passed.
## Implementation review: readonly ACP ownership

Step 3 is implemented. Review checked container daemon lifetimes separately
from host process cleanup, exact ID/nonce ownership, cancellation settlement,
pinned Docker executable/context, create-only private resource intents and
closure publication before release. A known preflight refusal releases capacity
only after local and daemon closure; resource/publication ambiguity quarantines.
Closure validation uses the already-selected role record and never reprobes a
fallback. Dashboard readiness can use an image-only runtime without changing
CLI-only no-edits behavior.

Actual Linux/Docker checks exercised normal exit, active cancellation, timeout,
cleanup API failure, foreign-name collision, changed resource intent and
dashboard ask-role without a host CLI. Source/Git writes were denied and scratch
remained writable. Starter/ACP discovery regressions, Ruff and documentation
checks passed. Background, owned fallback probes and live Chrome role checks
remain under steps 4–6; #337 is not complete.

## Implementation review: fallback probes

Step 4 is implemented. Review verified static candidate/source capture, ceiling
checks on every candidate, bounded status/limit replies, supervision of detached
probe children, durable pre-prompt selection and consumption of the discovered
runtime without repeated discovery. Initial source facts remain attached to
owned role preparation, including worktree execution.

Linux owner checks cover missing/logged-out/unknown/oversized/timed-out status,
all-missing refusal, cancellation, source changes and prompt-at-most-once.
Readonly image-only ACP with a native fallback also ran against actual Docker.
The oversized quota-reply regression failed on the previous implementation and
passed after bounded reading. The full earlier suite identified one mutating
snapshot regression, fixed without relaxing its admission/cleanup contract.
Background admission and live Chrome checks remain under steps 5–6.
