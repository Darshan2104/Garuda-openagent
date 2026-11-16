# Roadmaps

[Remaining feature-quality campaign work](feature-quality-status.md) records the
12 November review of the four open implementation issues and their follow-up
PRs, with merged mechanisms, residual scope and validation owners kept explicit.

Garuda Starters ([epic #302](https://github.com/Darshan2104/Garuda-openagent/issues/302)):
the P0/P1a CLI and P2 read-only dashboard share existing runtime owners. P1b
trusted Linux flow checks and P3 Linux browser flow starts use the reviewed
ownership boundary, and run-kind browser starts run through one
owned role-run parent (#316), including background queue ownership, worktree
isolation, readonly ACP and owned fallback probes (#337). See the
[approved plan](../plans/2026-10-06-ready-to-use-workflow-scenarios.md)
and [current workflow guide](../use-cases/workflows.md).

Context-fork slice (#415, second half of #370): native checkpoint prefixes now
continue under a new identity with ordinary admission and fresh workspace evidence.
[Context forks](../use-cases/advanced.md#fork-an-earlier-conversation-context) rewind
conversation only; read-only investigation scopes remain a separate slice.

- [ACP orchestration and unified context](2026-08-acp-orchestration.md) — the
  approved delivery sequence, linked to the GitHub epic/sub-issue hierarchy.
- [Issue catalog](issue-catalog.md) — canonical parent/child issue payloads in
  delivery order.
