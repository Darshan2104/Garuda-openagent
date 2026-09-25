# Open-PR feedback remediation design

## Purpose and scope

This design records the approved order for resolving maintainer feedback on the
open runtime stack. The work is deliberately dependency-ordered: an external
harness may only launch through the trusted registry, every mutating session
must have an attributable baseline, and recovery may only act on persisted,
validated identity.

The design covers PRs #98 through #118. It does not merge PRs; each revised
PR is independently reviewed and pushed after its relevant tests pass.

## Delivery order

1. **Authoritative session evidence (#98).** Capture a local-workspace
   baseline at the shared session boundary before mutation, persist it
   atomically, and use only that record for later delta, handoff, dashboard,
   and verification decisions. A required baseline or final delta failure is
   an explicit failed session/verification outcome, never warning-only
   telemetry. Non-local workspaces report unsupported attribution explicitly.
2. **Safe recovery (#99).** Persist Garuda-launched child/process-group
   identities, validate them before signalling, record cancellation at each
   runtime boundary, and reject unreadable checkpoints, inconsistent runtime
   identity/authority, and indeterminate liveness both before and after a
   reap attempt.
3. **Trusted runtime launch path (#100–#107).** Bind discovery, disabled
   settings, registry selection, command launch, CLI, SDK, and dashboard to
   one shared setup path. Vendor adapters use the ACP v1 wire contract and
   real/reference interoperability evidence where available; no entry point
   may bypass registry authorization or silently fall back.
4. **Observable, valid evaluation (#108–#118).** Add durable trace segment
   identities and cursors, make unsupported contract-matrix cases failures
   rather than vacuous passes, validate smoke responses and lifecycle
   timeouts, and wire eval conversion, support bundles, and matrix metrics to
   real persisted session evidence. Stale-history PRs are rebuilt cleanly on
   their corrected predecessor before any code is pushed.

## First slice: #98 authoritative baseline

### Session lifecycle

The common boundary that constructs an `AgentSession` obtains and records a
baseline before any prompt or runtime mutation. Local repository workspaces
must persist that record or refuse the run. Dashboard chat must enter the same
boundary rather than constructing a bypass session directly. SDK and CLI use
the same service.

At session finish, delta calculation loads the persisted baseline rather than
capturing a new one. A missing, corrupt, or unreadable record makes a required
verification/session state fail explicitly. Handoff accepts a workspace only
when it can load its recorded baseline-backed delta; ownership never transfers
without the claimed evidence.

### Delta semantics

Baseline state includes Git status and a per-path content identity sufficient
to distinguish pre-existing dirt from later mutation. Delta comparison handles
paths already deleted at start, paths restored to `HEAD`, pre-existing modified
or untracked files that are deleted, and dirty paths modified again. A change
is marked pre-existing only when its final state is unchanged from the captured
baseline; otherwise it is session work with a pre-existing origin flag.

### Verification and failures

The verifier receives the recorded delta and can make evidence policy depend
on it. Capture, persistence, load, and finish failures remain typed domain
errors and are reflected in session state/events. No broad exception handler
may convert these failures into a successful run or unaudited handoff.

### Tests

Focused tests drive the real CLI/SDK/dashboard session boundaries and prove:

- capture and metadata-write failure refuses before a mutating prompt;
- finish-time baseline/delta failure becomes an explicit failed outcome;
- handoff refuses a missing or unreadable baseline;
- clean and dirty baseline cases, including deleted/restored/untracked paths,
  preserve correct attribution; and
- verifier behavior consumes the persisted record rather than inspection-time
  filesystem state.

The full suite and Ruff remain required before pushing. Review explicitly
checks the selector (baseline/delta APIs) and enforcer (actual run, handoff,
and verifier entry points) so helper-only coverage cannot mask a bypass.

## Error-handling rules

Security- and attribution-shaped ambiguity fails closed. The user receives a
typed actionable error that names the unavailable evidence; logs may add
diagnostics but cannot change the state transition. Best-effort reporting is
allowed only for optional non-local attribution, which must be visibly marked
as unsupported rather than silently omitted.

## Out of scope

This design does not claim host sandbox confinement, does not read or proxy
vendor credentials, and does not turn failed/partial comparisons into a score
or a successful recovery. Those limits remain documented product boundaries.
