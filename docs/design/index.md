# Design records

These dated records capture designs for substantial changes. Check each record's
status; a proposed record has not been approved or implemented. They are
kept for rationale and review history; current behavior belongs in the user,
reference, and architecture documentation.

- [Quota-aware workflow continuation](2026-10-24-quota-auto-resume-design.md) — proposed hourly quota monitoring and continuation of interrupted Codex-backed flows.

- [Dashboard run extensions](2026-10-17-dashboard-run-extensions-design.md) — full #337 design approved on 2026-10-17; implemented through owned queue/workspace/container/probe lifetimes and Linux Chrome checks.

- [Dual-model delegation and runtime routing](2026-09-14-dual-model-delegation-and-runtime-routing-design.md)
- [Pluggable terminal strategy](2026-09-28-pluggable-terminal-strategy-design.md)
- [Collection forced terminal submission](2026-09-29-collection-forced-terminal-submission-design.md)
- [Paired-trial report CLI](2026-09-29-paired-trial-report-cli-design.md)
- [Runtime session-reference resolution](2026-09-29-runtime-session-ref-resolution-design.md)
- [Documentation refresh](2026-09-30-documentation-refresh-design.md)
- [Teams and sessions](2026-10-01-teams-and-sessions-design.md)
- [Agent definitions](2026-10-01-agent-definitions-design.md)
- [Flow verification ownership, evidence, and recovery](2026-10-08-flow-verification-ownership-design.md) — approved for implementation on 2026-10-12; runtime conformance remains required.
- [Owned one-click flow starts](2026-10-08-owned-one-click-flow-starts-design.md) — approved on 2026-10-12 and implemented for Linux flow-kind starters in #336; foreground run-kind starts reuse it through one owned role-run parent (#316).
