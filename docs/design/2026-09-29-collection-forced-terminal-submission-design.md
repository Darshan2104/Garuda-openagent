# Collection forced terminal submission

## Context

Collection workers are bounded, read-only children. Their only successful
terminal operation is `submit_collection`, whose gate validates a structured,
evidence-backed report. A live DeepSeek collection run with a two-turn worker
budget read its allowed files but used its final ordinary turn for prose. The
worker therefore exhausted its budget without submitting a report, and the
coordinator fell back to the reasoning model.

The worker already receives explicit trusted instructions to finish only by
calling `submit_collection`; this change does not alter that prompt, tool
scope, report schema, fallback policy, or collection opt-in policy.

## Decision

Enable Garuda's existing forced-final-submission mechanism only for collection
workers. When a collection child reaches its ordinary turn limit without a
valid terminal report, the runner makes one final model call with only
`submit_collection` available. The existing `CollectionCompletionGate`
continues to validate every field, evidence reference, buffer reference, path,
and permission before accepting the report.

The forced final turn is a terminal wind-down attempt, not additional evidence
gathering: it cannot call read, shell, network, mutation, delegation, or parent
completion tools. A malformed or unsupported report still fails normally and
the configured fallback behavior remains visible in events and accounting.

## Implementation shape

`CollectionCoordinator._config` enables `force_final_submission` for child
runs. `CollectionCompletionGate` advertises that it supports forced submission.
No new public flag, configuration key, model role, or tool is introduced.

## Verification

Add an owner-boundary scripted regression where a worker consumes its ordinary
turn with a read, then submits a valid report only on the forced terminal call.
Assert the terminal call exposes only `submit_collection`, the resulting report
is accepted, and no write-capable tool is available. Retain the existing test
that prose-only workers fail when no valid report is supplied. Run focused
collection and terminal-strategy tests, then the full suite and Ruff before
review.

## Non-goals

- Increasing ordinary collection budgets or silently retrying successful
  submissions.
- Loosening report validation, scope checks, or the read-only ceiling.
- Enabling collection by default or changing rollout thresholds.
