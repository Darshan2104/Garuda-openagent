# Shared owned role execution

Status: implementation in progress for #330, a prerequisite split from #313
under the user-approved [P1b ownership design](../design/2026-10-08-flow-verification-ownership-design.md).
#329 supplies the check subprocess owner. Public flow checks and P3 launches
retain their separate gates.

## Implementation order

1. Reuse the prepared Linux subreaper for bounded role workers. Give check and
   role requests separate enforced limits; acceptance cannot select a wider
   role budget through command text or environment. Keep existing imports and
   synchronous behavior compatible.
2. Add bounded private control tied to the actual worker PID/start identity,
   admitted parent capability, workspace and operation. Parent-owned admission
   remains live through physical closure. A child neither reconstructs a lease
   grant from disk nor releases the parent's workspace/capacity.
3. Separate pure frozen native/ACP preparation from owned activation through
   shared Setup/execution services. Preserve exact role/model/effort/profile,
   source authority, inherited ceilings, approval brokers and no-edits policy.
   Execute from frozen source values rather than reopening changed definitions.
4. Run the existing native loop/tool and ACP protocol owners inside the owned
   worker. Carry structured actual closure and child linkage to the flow launch
   boundary. Unknown cleanup/publication quarantines the original parent.
5. Prove actual native tool and ACP detached-writer cleanup, parent competition,
   cancellation, caller/source/policy refusal and identity parity. A local model
   protocol fixture or ACP shim is an external transport fixture, never a
   fabricated terminal cleanup receipt or vendor capability claim.
6. Review enforcement/identity/claims, run focused and full gates, and raise the
   prerequisite PR. Enable public flow checks only through #313's cumulative
   evidence, phase admission and strict publication owner.

## Boundaries

`Model` remains inference; coding harnesses remain behind `AgentRuntime`.
Private IPC conveys scoped requests to the retained authority, not vendor OAuth
tokens. Supervision is process ownership, not confinement. Unsupported execution
configurations remain explicitly unsupported for verified flows. The revision-5
snapshot stays unchanged; generated context is not collaboration storage.

## Review checkpoint

The draft implements separate worker-enforced process purpose budgets, pure
native planning followed by activation, trust-filtered frozen MCP configuration,
and actual native/ACP execution in the Linux subprocess owner. Four actual
native/ACP completion and repeated-cancellation cases prove detached writer
closure. They also observe the runtime's logical release and prove that its
capacity remains occupied during physical cleanup. A separate actual process
case rejects a correct nonce from a foreign PID.

Source digests are now captured at parsing reads and checked by an owned helper;
source/ceiling mutations, quarantine failures, approval/no-edits parity, full
suite validation and final enforcement review are still pending. This record is
an implementation checkpoint, not a claim that #330 or #313 is complete.
