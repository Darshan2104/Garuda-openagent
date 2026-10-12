# Owned asynchronous acceptance runner

Status: implemented for #312; flow integration remains #313.
The user approved the recommended
[P1b design](../design/2026-10-08-flow-verification-ownership-design.md) and
[P3 design](../design/2026-10-08-owned-one-click-flow-starts-design.md) on 2026-10-12.
This increment adds the internal acceptance owner; #313 owns flow finalization,
and #315/#316 retain their implementation conformance gates.

## Delivery order and owners

1. Merge the approved design records and resolve their shared navigation without
   changing the approved revision-5 snapshot.
2. Implement a dedicated Linux subreaper in `runtime/process_worker.py`, prepared
   before authorization. `runtime/process_supervision.py` owns its async lifetime,
   identity-bound activation, cancellation, bounded output, and terminal evidence.
   No command is sent before durable check authorization. `ECHILD` must follow
   actual reaping; process-group disappearance is insufficient.
3. Use that same owner for fixed acceptance probe/storage helpers. Move Git,
   fingerprints, source reads, lock acquisition and durable publication off the
   event loop. Helper cancellation must settle owned descendants or surface
   uncertainty. Never treat cancellation of a future as writer-stop evidence.
4. Add frozen check/request/context values and strict private records under the
   parent session. Bind intent, authorization, activation and immutable receipt
   to the parent, workspace, issued owner, candidate, evidence and authority.
   Validate schemas and bounded descriptor reads, reject symlinks and ambiguous
   publication, and serialize duplicate attempts across processes.
5. Implement `core/acceptance_runner.py` with sequential checks and fixed phase
   and command bounds. Validate the retained live parent capability, authority,
   sources and candidate at the owning boundaries. Keep the parent lease and
   heartbeat with the caller; uncertainty quarantines that same owner.
6. Extract only reusable pure receipt policy from `core/acceptance.py`. Preserve
   synchronous entry points, authority precedence, infrastructure-change guards,
   void-on-tree-change behavior and output redaction. Docker and unsupported
   platform checks stay unavailable without a host fallback.
7. Reconcile the actual producer's records without replay. Prepared without
   authorization, possible launch without receipt, and complete terminal evidence
   have distinct classifications. Missing PID is never proof of non-execution.
8. Review the diff, document shipped internal behavior and platform limits,
   run focused and full repository gates, open the #312 PR via Darshan2104,
   and merge after review and required CI. Keep #312 open until all its acceptance
   obligations pass. Continue with #313, then P3 in priority/dependency order.

## Acceptance proof

`acceptance_records.py` intentionally keeps the phase registry, transitions and
receipt schema together in one transaction owner (slightly over 300 lines).
Private comparisons preserve JSON numeric types; malformed values cannot compare
equal to an issued PID or a cleanup boolean. Redaction imports no inference
transport, including when called first in a cold process.

- Real Linux children: ordinary completion, shell/argv, closed stdin, stdout and
  stderr pressure, binary output, event-loop progress, timeout, repeated
  cancellation, double-fork/setsid and TERM-resistant writers. An unrelated live
  process must survive cleanup. Supervisor loss yields uncertainty.
- Mutating away the cleanup/reaping gate must leave a real writer and fail the
  owner test. Tests retain stable fixture signal handles for containment.
- Actual runner calls produce their own intent/activation/receipt chain; inject
  failures at real publication transitions rather than constructing those
  receipts in fixtures. Competing duplicate attempts launch once or refuse.
- Authority/candidate/source changes, malformed/private-store violations,
  unavailable modes and unknown cleanup cannot become verification passes.
- Existing acceptance tests protect synchronous compatibility. Flow lease
  competition, cumulative evidence, final publication and CLI enablement belong
  to #313, where the actual engine will consume this adapter.
- Run narrow tests first, then full pytest, Ruff and docs checks. Linux Docker
  supplies local process-supervision conformance; macOS supplies actual refusal
  coverage. Disclose live vendor, tmux, Harbor and OS-sandbox coverage separately.
