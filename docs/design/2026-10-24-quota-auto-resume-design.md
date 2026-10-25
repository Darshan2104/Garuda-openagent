# Quota-aware workflow continuation

Status: approved on 2026-10-24; implementation in progress.

## Problem and outcome

A Codex-backed Garuda workflow can stop when its subscription usage window is
exhausted. Resetting the quota does not resume the workflow. The private watchdog
installed for the Garuda self-improvement run watched a fixed list of sessions;
later work was outside that list. A reviewer denied permission was also correctly
ineligible for a quota retry, but that distinction was poorly surfaced.

With recovery enabled, Garuda should automatically register an unfinished
quota-stopped flow, persist its continuation state, check quota hourly, and
continue the interrupted step after confirmed availability. Completed steps and
their artifacts remain intact. A completed coder whose reviewer ran out of quota
continues at the reviewer; the coder is not rerun merely to obtain a review.

## Approaches considered

1. Built-in recovery service with an hourly user-cron trigger: durable across
   Garuda process exits, testable independently of scheduling, and automatically
   enrolled by execution. Recommended.
2. Extend the private watchdog: quicker locally, but leaves recovery outside the
   installed product and repeats hand-maintained session registration.
3. Keep a sleeping worker alive: simpler scheduling, but loses reliable recovery
   when the terminal or worker exits and retains process resources while idle.

## Delivery scope

The first delivery covers Codex-backed sequential CLI flows, including paired
coder/reviewer loops and failures in either role. Each newly launched eligible
flow enrolls automatically when recovery is enabled; no session allowlist is
maintained by hand. Resumed work retains the policy for a later quota pause.

Standalone chat/run sessions, parallel review groups, and protected dashboard or
acceptance-check parents do not yet have equivalent safe continuation contracts.
They must expose unsupported recovery explicitly rather than silently restart.
Extending them requires their respective lifetime/publication owners to supply
checkpointed continuation. Existing refusal of their generic resume stays in
force. This release must describe its scope as flow recovery, not universal
session recovery. Claude remains unsupported until it has a proved quota source.

## User controls and visibility

Provide user-owned recovery configuration and CLI commands to enable/disable the
hourly schedule, inspect pending work, cancel a pending continuation, and run one
check manually. Schedule installation uses absolute executable paths and preserves
unrelated crontab entries. Project configuration cannot authorize cron changes.

Once explicitly enabled, quota-paused eligible flows register automatically.
Record the next check, observed reset, last probe, pending step, attempt count,
and blocked reason. Flow show and session/dashboard detail project that record
without running a quota probe or launching work on a GET request.

States are waiting-for-quota, dispatch-reserved, running, completed, blocked,
cancelled, or uncertain. A blocked workflow states the concrete reason, such as
permission denial, unsupported parent, changed configuration, or unresolved
ownership. The next check continues monitoring waiting records; it does not
silently declare a stopped workflow complete.

## Quota evidence

Reuse the official CLI's read-only account and rate-limit interface through
`garuda/acp/limit_probe.py`, with bounded subprocess lifetime and no model prompts.
Do not read vendor credential files or store tokens or account names. Persist only
the existing locally salted account digest and normalized limit evidence.

The five-hour window is provider-reported, not a sleep of five hours starting at
failure. A reset timestamp is a hint for when to check, not proof of availability.
Dispatch requires a fresh, valid observation for the same account and exact
supported CLI version, with all applicable windows below exhaustion and ordinary
usage affirmatively allowed. A weekly cap still blocks a five-hour reset.
Unknown, malformed, stale, changed-account, or changed-version evidence never
launches work. The current proved-source table only lists Codex 0.159.3; newer
versions require explicit interface conformance coverage before enabling them.

Classify a quota pause using a settled Codex step failure and official exhaustion
evidence, not arbitrary error-message matching alone. Permission failures,
cancelled turns, invalid review output and generic internal errors remain
non-quota failures even when an unrelated account window is exhausted.

## Durable continuation and execution ownership

Keep scheduling in a dedicated shared recovery service. The flow engine owns
execution, receipts, leases, review state and artifact validation; CLI handlers
only adapt requests. Store recovery records in the user's Garuda home using
atomic documents and cross-process locking, outside the workspace context pack.

At admission, capture the authorized recovery policy and source/configuration
identities. After a proven quota stop and child closure, persist a continuation
bound to the parent, failed step/attempt, receipt/journal digest, runtime/model/
effort, account, workspace identity and workspace content checkpoint. Preserve
existing edits. Later workspace or authority changes block automatic execution
instead of resetting files or silently accepting a different configuration.

Persist review-loop progress at settled boundaries: current round, completed
coder receipt, pending reviewer, prior verdicts and findings. Validate against
the actual receipts on resume. A quota-interrupted reviewer resumes the pending
review; a quota-interrupted coder continues with the same round's feedback.
Existing round limits and reviewer independence checks still apply. A quota
continuation must never manufacture an approved review or reopen an exhausted
review budget. Generic resume remains conservative for legacy records without a
validated review checkpoint.

Continuation means the interrupted step receives its retained task, prior
artifacts and bounded partial-work context. Use harness-native session load only
when supported and identity-validated; otherwise explicitly record continuation
from saved workflow/workspace context. Do not promise restoration of hidden model
state or rerun completed shell actions from a transcript.

The hourly tick claims a due record under lock, revalidates authority and current
quota, then durably reserves a single dispatch before launching through the
existing flow execution service. The worker acquires the existing workspace and
runtime-capacity owners. Concurrent ticks cannot dispatch the same continuation.
Waiting releases execution capacity and workspace ownership only after proved
child closure. Unproved closure remains quarantined.

A crash after reservation is reconciled against the recorded worker/session
identity. An ambiguous launch becomes uncertain and visible; a stale lock or
elapsed timeout alone is not permission to replay. Manual resume, Stop and cancel
must coordinate with this same record to prevent a cron/manual duplicate.
Scheduler disable stops future cron dispatch; cancellation revokes a specific
pending continuation and does not implicitly signal an unrelated live process.

## Validation and documentation

Use deterministic fake quota/time inputs and real persisted flow receipts to
prove exhaustion -> wait -> reset -> one continuation. Cover quota failure in
planner/coder/reviewer, retained partial edits, reviewer continuation without
repeating its completed coder, repeated exhaustion, and unchanged review budgets.

Adversarial cases include duplicate ticks, a manual-resume race, cancellation,
restart before/after dispatch reservation, weekly exhaustion, unknown/malformed
quota, account/version/config/workspace changes, permission refusal, missing
receipts and unproved child closure. Verify that no quota probe sends a prompt or
requests credentials. Exercise scheduler installation against a fake crontab
with unrelated entries. No live subscription quota is needed for these tests.

Document supported scope and runnable enable/status/cancel examples in the
automation use cases and feature reference. Update CLI reference, architecture,
module map and durable decisions for the new public recovery boundary. Run
focused tests, the full suite, Ruff and documentation contracts before a PR.

## Implementation sequence

1. Add validated quota eligibility and durable recovery records/claims.
2. Add engine-owned settled review checkpoints and targeted continuation.
3. Connect automatic enrollment and dispatch through the shared flow service.
4. Add cron management, status/cancel commands and read-only status projection.
5. Add behavioral/race tests, documentation and full validation; open a PR.
