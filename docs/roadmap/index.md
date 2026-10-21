# Roadmaps

Garuda Starters ([epic #302](https://github.com/Darshan2104/Garuda-openagent/issues/302)):
the P0/P1a CLI and P2 read-only dashboard share existing runtime owners. P1b
trusted Linux flow checks and P3 Linux browser flow starts use the reviewed
ownership boundary, and run-kind browser starts run through one
owned role-run parent (#316), including background queue ownership, worktree
isolation, readonly ACP and owned fallback probes (#337). See the
[approved plan](../plans/2026-10-06-ready-to-use-workflow-scenarios.md)
and [current workflow guide](../use-cases/workflows.md).

- [ACP orchestration and unified context](2026-08-acp-orchestration.md) — the
  approved delivery sequence, linked to the GitHub epic/sub-issue hierarchy.
- [Issue catalog](issue-catalog.md) — canonical parent/child issue payloads in
  delivery order.
