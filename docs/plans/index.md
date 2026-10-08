# Implementation plans

These dated records describe approved designs and their implementation. Check
each record's status: a plan can include behavior that has not shipped. Use the
current guides for command reference and the [open backlog](../BACKLOG.md) and
linked GitHub issues for open work.

- [Ready-to-use workflow scenarios](2026-10-06-ready-to-use-workflow-scenarios.md) — revision 5; P0 and P1a approved, later phases gated; tracked in [epic #302](https://github.com/Darshan2104/Garuda-openagent/issues/302).
- [Starter P0 contract confirmation](2026-10-07-starter-p0-contracts.md) — current-main evidence and implementation constraints for the first CLI release.

- [Dual-model delegation and runtime routing](2026-09-14-dual-model-delegation-and-runtime-routing-implementation.md)
- [Collection forced terminal submission](2026-09-29-collection-forced-terminal-submission-implementation.md)
- [Paired-trial report CLI](2026-09-29-paired-trial-report-cli-implementation.md)
- [Runtime session-reference resolution](2026-09-29-runtime-session-ref-resolution-implementation.md)
- [Documentation refresh](2026-09-30-documentation-refresh-implementation.md)
- [GitHub Pages documentation](2026-10-01-github-pages-implementation.md)
- [Teams and sessions](2026-10-01-teams-and-sessions-implementation.md)
- [Agent definitions](2026-10-01-agent-definitions-implementation.md)

P0/P1a landed through [#318](https://github.com/Darshan2104/Garuda-openagent/pull/318)
through [#325](https://github.com/Darshan2104/Garuda-openagent/pull/325). P2's
read-only dashboard discovery, preview/copy and selected result evidence landed
in [#327](https://github.com/Darshan2104/Garuda-openagent/pull/327), independently
of P1b. P1b still requires review of the proposed
[flow verification ownership design](../design/2026-10-08-flow-verification-ownership-design.md)
in [draft #326](https://github.com/Darshan2104/Garuda-openagent/pull/326); trusted
flow checks and P3 browser launch remain separately gated. The revision-5 plan
snapshot is unchanged; current guides describe shipped behavior.
