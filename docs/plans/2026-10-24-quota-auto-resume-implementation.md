# Quota recovery implementation

Approved design: [Quota-aware workflow continuation](../design/2026-10-24-quota-auto-resume-design.md).

Scheduler readiness repair approved on 2026-10-26:

1. Extend the existing scheduler contract tests to reproduce wrong entries,
   missing/unsafe wrappers, unsuccessful readback and hidden OS diagnostics.
2. Validate the managed entry and wrapper; require readback before enablement
   or successful removal. Preserve unrelated entries and disabled-on-failure
   behavior.
3. Expose bounded, redacted subprocess stderr through the recovery CLI and
   document observed installation separately from persisted enablement.
4. Run the focused recovery tests, full suite, Ruff and documentation contracts;
   inspect live host status without launching model work. Leave the macOS OS
   cron refusal explicit if activation remains unavailable.

1. Reuse and harden official Codex quota observations; prove supported CLI
   versions and require affirmative account-bound availability.
2. Add engine-owned review checkpoints and targeted step continuation while
   retaining receipts, round budgets, review independence and workspace leases.
3. Add a private durable recovery store, automatic flow enrollment, single-claim
   dispatch, cancellation and conservative crash reconciliation.
4. Add cron enable/disable, tick/status/cancel commands and status projections.
5. Exercise recovery, races, refusal cases and scheduler preservation; update
   user documentation and durable boundary decisions.
6. Independently review, run focused/full validation, push a PR, wait for CI and
   merge to main as authorized by the user.

Validation uses fake quota/time and launch boundaries with real session/flow
records; no subscription prompts are used. Existing unrelated untracked files
are preserved. The private legacy watchdog is not part of the product.
