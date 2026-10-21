# Dashboard run start extensions (#337)

Status: full design approved by the user on 2026-10-17; implementation in progress.
Extends the Linux-first gateway from #315 and foreground role parent from #316.

## Scope and approach

Complete every item in [#337](https://github.com/Darshan2104/Garuda-openagent/issues/337):
background queue admission, worktree/auto isolation, confined readonly ACP,
owned fallback probes, native ask-role read-only parent leases and a live Chrome
role-start check. Preserve ceilings, frozen sources, approval audit, strict
publication, at-most-once dispatch and sticky quarantine.

Extend existing gateway, intents, role-run parent and background worker.
A second dashboard executor duplicates lifecycle policy; invoking the public CLI
obscures operation binding and cleanup ownership. Neither is selected.

## Background operation

Version-2 intents add a closed queue-admission record alongside the reserved
session. Read version-1 records without rewriting. Unknown schemas and conflicts
refuse. Bind the operation in session metadata before enqueue. Admission binds
scope, queue item, runtime, launch digest and session; no task/credential enters
the intent. Queue admission is distinct from a workspace lease epoch.

The v2 intent remains `launch-authorized`; worker progress belongs to the
strict session/queue/closure records, not to an intent phase rewrite.
Only the initial request may enqueue/spawn. Duplicates, lost responses and
restarts reconcile existing session/launch/queue records; they never dispatch.
Contradictory or missing evidence yields uncertainty. Background status follows
worker/queue/session evidence independently of dashboard liveness. Queued work
holds no workspace lease. Delegate queue capacity rather than acquiring twice.
The detached worker owns the parent/descendants and survives dashboard shutdown.
Use durable approvals and existing identity-checked Stop; unavailable approvals
fail closed.

## Workspace isolation

Select with `session_service.choose_workspace` before leasing. Distinguish the
previewed source path/device/inode from execution path/device/inode. Keep frozen
role/config authority from the source; never re-resolve it from the worktree.
Use the execution workspace for child, no-edits comparison, evidence and checks.
Persist existing worktree metadata and dirty-source disclosure: uncommitted
source edits are not copied. `auto` retains shared behavior outside Git.

Discard only a newly created, refused worktree after bounded preparation settles.
Retain admitted worktrees for inspection/explicit merge and retain them under
cleanup/publication uncertainty. Duplicate operations never create another.

## Read-only ownership and ACP

Unchecked native ask-role may share an issued read-only parent lease. Lifetime
snapshots and child receipts bind that exact mode/session/issuance. Acceptance
snapshots remain mutating-only. Checks can write, so checked asks retain exclusive
mutating ownership, disclosed in authority. Concurrent external edits may
invalidate no-edits evidence; withhold the answer without inventing attribution.

Readonly/no-edits ACP uses Docker confinement with no host substitution. Own
preflight and execution containers by allocated identifiers. Preserve read-only
source mounts, bounded scratch, non-root restrictions and mount validation;
pass no host vendor credentials. Normal exit, cancellation and timeout remove
and verify absence of the exact containers. Process reaping alone does not prove
daemon-owned container closure. Retain parent/capacity quarantine on ambiguity.

## Fallback selection

Preview describes all permitted candidates without executable probes. Freeze
trusted manifests/models/inherited posture for each candidate; projects cannot
widen that set. Run bounded discovery/login/eligible limit probes under the
operation's supervised lifetime after durable authorization.

Only proved missing CLI, logged-out state or fresh eligible exhaustion skips a
candidate. Unknown login/limits keep it; trust/config/confinement errors refuse.
Recheck the selected candidate's ceiling and record identity/skip reasons before
prompting. Never fall back after a prompt. Unproven probe closure quarantines.

## Evidence and self-review

Use owner-boundary regressions that fail before repair; avoid duplicate helper
tests and fixtures manufacturing admission or closure receipts.

- Background: concurrent requests, durable interruption boundaries, lost
  response, restart/shutdown, cancellation, worker death, conflicts and single
  capacity ownership. Observe real queue rows and launch counts.
- Isolation: real Git worktree execution with unchanged source, dirty disclosure,
  auto outside Git, refused cleanup, retained admitted worktree, changed
  identities and duplicate no-creation.
- Readonly: ask alongside mutating owner, forged issuance refusal, checked ask
  exclusion and no-edits answer withholding.
- ACP: source writes denied, exact container absence after cancellation/timeout,
  API ambiguity quarantines, confinement refusal never launches on host.
- Fallback: real supervised probes, one prompt, actual identity/model recorded,
  ceiling/config denial, unknown semantics and detached descendant cleanup.
- Browser: Linux Google Chrome through real dashboard, run/ask, duplicate
  projection, progress/result links, stale source, approval and server lifecycle.

Run focused/full pytest, Ruff and documentation checks. Self-review each batch
and the full issue diff for dispatch, identity, authority, closure and claim gaps.
Report live Linux/Docker/Chrome/vendor coverage accurately. Do not close #337
until every requirement is implemented and verified.
