# Owned flow verification

Status: foundation in progress for #313 under the approved
[P1b ownership design](../design/2026-10-08-flow-verification-ownership-design.md).
The acceptance runner (#312) and native/ACP lifetime owner (#330) have landed.
Public flow checks remain disabled until the integration gates below pass.

## Implementation order

1. Add opt-in strict publication at the shared SessionStore metadata owner.
   All writers use the existing sidecar lock. A registry outside the session
   preserves strict protection when local records disappear. Durable metadata
   heads detect missing, corrupt or replaced publications. Legacy sessions
   retain their existing behavior.
2. Capture check definitions and original configuration source identities once
   before admission. Preserve absent inputs and reject untrusted configured
   checks. Requested checks have explicit user-request authority.
3. Under the parent's mutating lease, capture a complete starting baseline and
   every step/attempt boundary delta through owned helpers. Retain changes later
   restored and actual native/ACP closure evidence. Refuse incomplete or
   unsupported evidence rather than clipping an authoritative manifest.
4. Persist the original verification phase and bindings before acceptance.
   Pin one final candidate; retain the parent heartbeat through checks,
   immutable verification commitment and strict parent metadata publication.
   Unknown closure or publication quarantines the original owner.
5. Reconcile recorded bindings on resume without launching uncertain attempts
   again. Missing check-side files cannot authorize fresh work. Preserve the
   committed verification across final status, tag and observability updates.
6. Enable service checks, CLI --check and authorized build-review checks only
   after owner-level integration proves competition, cancellation, cumulative
   evidence, candidate/source binding and restart behavior. Keep execution,
   review and verification as separate outcomes.

## Scope and validation

Linux is the initial owned execution backend. Process ownership is not
confinement; unsupported role or check environments stay unavailable. Existing
synchronous run checks, same-name configured flows and review round bounds
retain their contracts. Planning starters never select automatic checks.

Tests must drive the production flow/runtime/check owners. Transport fixtures
may answer model or ACP protocols but cannot supply closure records or verdicts.
Exercise actual competing owners, check writes, final publication faults and
loss of the entire check directory. Run focused tests before the full suite,
Ruff, documentation commands, strict site and exact-head CI. Keep the approved
revision-5 snapshot unchanged and generated context with its existing owner.
