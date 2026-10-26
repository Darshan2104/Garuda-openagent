# Workflow review: 19–26 October 2026

This is a dated review of local Garuda execution evidence and the checkout at
`edeba79`. It describes observations as of 26 October, not ongoing monitoring.
The strongest immediate reliability gap is scheduler readiness: automatic
recovery exists, but it is disabled on this host and its installation status
does not validate the command it reports as installed.

## Evidence and limits

The review used local session metadata, flow journals, receipts and artifact
integrity checks, selected plan/patch/review contents, the private
self-improvement watchdog, CI repair and merge records, current implementation,
and focused recovery validation. The unit of analysis is a whole flow and its
children; child failures are not counted again as separate failed workflows.
No Phoenix endpoint was configured, so this review used local records.

The session store contains 124 records created in the interval: 29 flow parents
and 95 other sessions. The available records fall on 22–24 October; absence on
other days does not prove that no work occurred elsewhere. Of the flow parents,
13 concern temporary example workspaces and 16 concern Garuda self-improvement
or its CI repair. These overlapping attempts and continuations are a convenience
sample, not an independent benchmark or a measured success rate.

| Population | Completed metadata | Failed metadata | Other |
| --- | ---: | ---: | ---: |
| All 124 session records | 105 | 17 | 2 legacy `finished` |
| All 29 flow parents | 19 | 10 | 0 |
| 16 self-improvement and CI flow parents | 11 | 5 | 0 |

All 124 records report verification as `unavailable`. Ten of the eleven
completed self-improvement/CI flows have `review_approved`; the other is a
diagnostic flow. Neither completion nor model review proves acceptance checks
passed. Separate CI records provide additional evidence for the merged work.

Across all flow parents, 76 receipts refer to 68 output artifacts. Every output
artifact exists with its recorded SHA-256 and byte length, and every referenced
child session exists. One interrupted example flow has an intent without a
receipt; that is an unresolved execution boundary, not permission to replay it.

Persisted ACP trails exist for 93 sessions. Their ordinary message, tool, error
and lifecycle entries retain event identifiers and kinds, not payload contents.
Outbound prompt entries retain digest/length metadata. Consequently these files
cannot reconstruct full tool trajectories or explain a generic harness error.
This report does not claim to have read unavailable transcripts, infer token
cost from chunk counts, or recover hidden vendor conversation state.

## What worked

- **Worktree separation produced useful changes.** The bugs, technical-debt and
  shareability tracks produced the changes merged as PRs 350–352. Local Git
  history agrees with the saved merge records. Examples include project
  permission-ceiling enforcement, directory-rule handling, overlapping-edit
  fixes, shared write-tool classification, and clearer onboarding.
- **Independent review caught real defects.** Flow
  `9d327d92-104f-4bdd-ba38-b0e011408195` requested changes for a remaining queue
  publication race, then approved the second round. Flow
  `66c9c74a-ec7f-474d-8a3b-5253015a5986` identified that an SSE transport check
  could pass through a nonstreaming fallback. Preserve these executable checks
  alongside model review.
- **No-edits checks detected actual changes.** The planner stop in
  `cd505a4e-f183-444a-a22b-a991a3050ec0` records `.claude`; an example reviewer
  stop records `.pytest_cache/v/cache/nodeids`. Outputs were withheld. Do not
  solve this friction by ignoring arbitrary hidden directories.
- **Permission denial remained distinct from quota.** The shareability review
  in `960a278a-1647-4fc5-a4f8-3712ca44a19b` stopped after a denied `Run command`.
  Its approval record confirms denial. The private watchdog kept it manual
  even after quota became available; that was correct.
- **External validation found failures model review missed.** CI repair records
  include dependency transport checks, browser checks, and deterministic
  publication regressions. The saved final record reports all 36 checks across
  the three PR heads passing before their merge. This is historical evidence,
  not a fresh query of GitHub.
- **The new recovery owner has useful refusal behavior.** Current focused
  validation covers retained work, reviewer-only continuation, preserved review
  budgets, exclusive claims, repeat quota pauses, cancellation, changed evidence,
  and uncertain cleanup. Completed steps remain receipted rather than replayed.

## Stops and remaining gaps

| Evidence | Observed behavior | Implication |
| --- | --- | --- |
| `58705180-ef70-44e0-8df4-3d35175d388c`, `e773eb13-8959-4db8-97dd-a5aaa524c810` | Claude reviewer/planner hit subscription limits | Codex-only recovery cannot resolve these; retain explicit unsupported state until a proved Claude source exists |
| `960a278a-1647-4fc5-a4f8-3712ca44a19b` | Reviewer needed a command its policy denied | Prepare a sufficient review packet and show effective tool permissions before spending a review turn |
| `cd505a4e-f183-444a-a22b-a991a3050ec0` | No-edits planner changed `.claude` | Test adapter startup and planner behavior against the declared workspace policy; surface the changed path |
| `9765d35a-2072-4567-a356-88a231139f58` | Second build stopped with a generic internal error; later resume reports unresolved review | Preserve both the original execution failure and the later recovery refusal; never classify this as quota from guesswork |
| `c7b228bc-84c5-490b-ad35-6e2855bf82a7` | Model review approved `bootstrap_environment: false` in a profile that ignored it | Require an executable loader/effective-config check for configuration fixes; correction was made in `19d24cc6-fcc8-4755-9e18-a5992449b1ee` |
| `83fede6a-d7da-4e4f-8917-1f3c7e8c80b2` | Example metadata says completed but retains an earlier `flow.review_invalid` stop | Historical status is contradictory; the current resume path clears `flow_stop`, so do not call this an unfixed current success bypass without reproduction |
| `34579db2-37e7-49d3-bc18-14bb10bccc2f` | Interrupted example has an unmatched build intent | Retain quarantine/refusal; inspect ownership and workspace rather than synthesizing a receipt |

Model identity checks also have a narrower meaning than independent authorship.
The bugs-track plan explicitly records inherited drafts authored by the eventual
reviewer's model, while the engine's independence record compares launched role
identities. Make inherited provenance visible; do not claim those identity checks
prove independent origin of every line being reviewed.

## Cron and quota findings

The active user crontab contains only the old
`garuda-self-improve-quota-watchdog` block at minute 27 each hour. It watches three
fixed tracks. Its current state marks all three complete, including the linked
shareability continuation, so its ticks no longer probe quota. Its retained log
contains completed/manual decisions, with no automatic dispatch recorded. The
presence of this cron job therefore does not demonstrate successful automatic
quota resumption or coverage of new flows.

The built-in recovery store reports `enabled: false`, `installed: false`, and
no enrolled flows. The PR 353 merge record already recorded a failed activation
caused by macOS refusing the crontab write. This review reproduced the refusal:

```text
crontab: tmp/tmp.<pid>: Operation not permitted
```

Activation was attempted using the already installed Codex 0.163.0 binary.
Garuda kept recovery disabled after failure, preserved the legacy crontab, and
did not launch a continuation. A manual tick returned disabled with no flows.
The existing wrapper was refreshed by the enable attempt; a wrapper file alone
does not establish an installed schedule.

The ordinary shell now selects Codex 0.164.0. Garuda's proved quota sources cover
0.159.3 and 0.163.0 only. A live read-only probe through the installed 0.163.0
binary returned account-bound availability with both reported windows below
exhaustion. It sent no model prompt. Do not silently treat 0.164.0 as proved or
discard account/version binding to make recovery proceed.

A separate disposable reproduction found a current status defect:

```text
Input cron block: Garuda markers, schedule 0 0 * * *, nonexistent wrong wrapper
Reported status: installed=true, job_count=1, schedule=27 * * * *
Expected wrapper exists: false
```

`runtime/recovery_schedule.py::schedule_status` counts marker pairs without
checking their command, schedule, selected root or wrapper. `install` also
returns success without reading back the installed entry. The CLI catches the
crontab exception but prints its generic description, hiding the useful OS
stderr. These are actionable product gaps even though failed activation keeps
execution disabled.

Recovery currently supports future, admitted sequential Codex CLI flows. It
does not adopt these old sessions, resume standalone chat/run or this external
Codex conversation, cover Claude, or continue protected/check-enabled/dashboard
flows. Those exclusions are real contracts, not tasks to bypass. In particular,
adding trusted flow checks currently sacrifices automatic quota continuation;
combining both safely needs work by the verification and lifetime owners.

## Recommended order of work

1. **Make schedule readiness truthful.** Validate the exact entry, selected
   root and executable wrapper; verify installation by readback; show bounded
   crontab stderr and an actionable reason. Add recovery readiness diagnostics
   without probing providers from status reads.
2. **Provide a usable macOS schedule.** Consider an explicit user LaunchAgent
   backend invoking the same `recovery tick`, with absolute paths, exclusive
   claims, owner-private files and readback. Keep cron available. Never mark
   enablement successful because configuration was merely written.
3. **Publish tick health.** Record a bounded latest attempt/result, including a
   zero-pending tick. Distinguish installed, runnable, recently checked, waiting,
   blocked and unsupported. Host sleep still delays execution.
4. **Make role preparation executable.** Show effective adapter/mode/policy and
   required review inputs. Exercise the real loader for configuration fixes;
   provide review packets suited to permissions. Keep no-edits detection intact.
5. **Connect acceptance evidence and continuation.** Preserve review, execution
   and verification as separate fields; design protected-flow checkpoints before
   extending recovery. Attach external CI evidence with exact source revisions
   rather than upgrading a session's verification from a prose claim.
6. **Make later reviews cheaper to investigate.** Retain bounded structured
   failure classes, original and recovery causes, artifact availability and
   provenance. Keep reasoning, secrets and large tool output out of context packs
   and shared exports. Token cost remains unknown unless a proved source reports
   it; repeated broad validation should be justified by new changes or failures.

Three approaches are available: harden the current cron implementation first
(smallest repair, but macOS activation remains blocked), add a platform scheduler
backend that reuses the recovery owner (more work, addresses this host), or keep
using the private fixed-list watcher (quick locally, misses future flows). Start
with truthful readiness, then add the native scheduler as a separate change.
Do not extend the private watcher into another execution engine.

## First proposed change for review

Scope the first patch to `runtime/recovery_schedule.py`, the recovery CLI,
existing scheduler tests and relevant reference/use-case documentation. Parse the
managed cron entry into observed evidence and compare it with the expected
minute-27 entry for the selected private root. Verify the expected wrapper is an
owner-controlled executable file. Preserve unrelated entries and fail closed
on malformed or ambiguous managed blocks. Read back after installation before
publishing enabled settings. Report bounded OS error details without environment
or credential dumps. No provider calls belong in the status path.

Extend the existing scheduler contract tests with the observed wrong-command,
wrong-schedule, missing-wrapper and failed-install cases. Demonstrate the status
regression on the baseline, then verify the corrected behavior, idempotence and
unrelated-entry preservation. A native macOS backend, tick-health persistence,
new harness versions and broader recovery scopes remain separate designs.

This is a concrete proposal, not an approved implementation or a claim that the
cron blocker has been removed.

## Validation performed in this review

```bash
pytest tests/test_flow_quota_recovery.py tests/test_recovery_schedule.py tests/test_quota_dispatch_guards.py tests/test_quota_acp_cleanup.py tests/test_quota_availability.py -q
```

Result: 57 passed. The live read-only supported-version quota probe succeeded.
Ruff and whitespace checks passed. The documentation command contract passed
against a temporary copy of tracked repository content plus this report. The
whole-workspace command check failed on a pre-existing untracked spec under
`docs/superpowers/` that uses the obsolete `garuda run --flow` option; that file
was preserved. A strict rendered documentation build could not run because
MkDocs is not installed in this Python environment.

Real schedule installation failed at the OS crontab write. The wrong-entry
status reproduction used a temporary private store and a substituted crontab
reader; it did not modify the real schedule. No full suite, live model task,
Chrome, Docker, tmux, Harbor or OS-sandbox integration was run for this review.

The original untracked `.opencode/` and `docs/superpowers/` content was preserved.
No session evidence was rewritten, no work was replayed, and no generated
context-pack file was written.

## Follow-up: approved scheduler repair

The user subsequently approved the first proposed repair. The working tree now
validates the actual selected-root cron entry and private executable regular
wrapper, reads back installation before publishing enablement, verifies removal,
and reports a reason for missing or mismatched installation. The CLI exposes
bounded, redacted OS stderr. The current guide, reference, architecture and
durable decision record describe those contracts; fixed entries were removed
from the open backlog.

New scheduler regression cases reproduced false-positive status, absent
installation readback and hidden OS diagnostics before the repair. The focused
recovery run after the repair reported 71 passed. Ruff, whitespace and the
documentation command contract for tracked content plus this report passed.
The full suite reported 4,141 passed, 126 skipped and one failure in
`test_maintained_docs_commands_match_real_cli_parser`, caused by the pre-existing
untracked spec's obsolete command noted above. That same documentation test
passed against tracked content plus this report.

A real enable attempt through the supported installed Codex binary still failed
with `crontab: tmp/tmp.<pid>: Operation not permitted`, now visible directly in
the CLI. Recovery remains disabled and the legacy cron block is preserved.
This repair does not resolve that host permission issue or implement a native
macOS scheduler. No additional live model, Chrome, Docker, tmux, Harbor or
OS-sandbox integration was run.
