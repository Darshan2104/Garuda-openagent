# Feature quality campaign: 26 October 2026

This implementation map follows the [opportunity study](../archive/2026-10-26-agent-feature-study.md) and [existing-feature comparison](../archive/2026-10-26-existing-feature-comparison.md). GitHub [campaign #355](https://github.com/Darshan2104/Garuda-openagent/issues/355) is the authoritative work tracker; this dated plan records the initial scope and sequencing, not live completion state.

## Goal and authorization

Improve existing Garuda mechanisms, then add independently scoped opt-in capabilities justified by concrete workload needs. The user authorized detailed issues under Darshan2104, sequential implementation and PRs, self-review without approval pauses, and cron continuation when supported quota evidence permits work. Publishing a PR is not the same as merging it or completing every issue.

## Plan self-review

Three approaches were considered: selectively improve native owners, add a separate OMP external runtime, or port the whole upstream application. Selective native work is the primary approach; OMP gets its own conformance study. A whole-stack port would duplicate lifecycle/permission/evidence systems without measured benefit.

Start with demonstrated buffer integrity and streaming metadata defects, typed-result delivery, and precise capability documentation. Failure/artifact/review/budget owners precede dependent workers and execution services. Existing accepted limitations are not silently reopened. Broad capabilities stay opt-in; no benchmark improvement is inferred from source inspection.

## Scope and dependencies

Each linked issue contains why it matters, observed versus proposed behavior, current owners, implementation expectations, acceptance criteria, tests, risks, non-goals and dependencies.

| Issue | Priority | Work | Dependencies |
| --- | --- | --- | --- |
| [#356](https://github.com/Darshan2104/Garuda-openagent/issues/356) | P1 | Publish tool-output buffers atomically and reject incomplete captures on discovery | None |
| [#357](https://github.com/Darshan2104/Garuda-openagent/issues/357) | P1 | Preserve validated structured child outputs through ordinary subagent handoffs | #356 |
| [#358](https://github.com/Darshan2104/Garuda-openagent/issues/358) | P1 | Preserve provider terminal reasons across streaming and ordinary model responses | None |
| [#359](https://github.com/Darshan2104/Garuda-openagent/issues/359) | P1 | Publish the comparative studies and correct ACP verification and native streaming descriptions | None |
| [#360](https://github.com/Darshan2104/Garuda-openagent/issues/360) | P1 | Add bounded causal failure bundles with explicit native and ACP evidence coverage | #359 |
| [#361](https://github.com/Darshan2104/Garuda-openagent/issues/361) | P1 | Unify read-side artifact completeness, provenance and retention metadata | #356, #360 |
| [#362](https://github.com/Darshan2104/Garuda-openagent/issues/362) | P1 | Give reviewers candidate-bound evidence packets and structured finding provenance | #361, #360 |
| [#363](https://github.com/Darshan2104/Garuda-openagent/issues/363) | P1 | Enforce a shared run-tree spend budget and preserve failed-call usage attribution | #358, #361 |
| [#364](https://github.com/Darshan2104/Garuda-openagent/issues/364) | P1 | Build a frozen minimal evaluation baseline and workflow incident regression corpus | #359 |
| [#365](https://github.com/Darshan2104/Garuda-openagent/issues/365) | P1 | Bound underlying text-file reads for range and tail access | None |
| [#366](https://github.com/Darshan2104/Garuda-openagent/issues/366) | P2 | Add opt-in structural source outlines with exact omission and version evidence | #365 |
| [#367](https://github.com/Darshan2104/Garuda-openagent/issues/367) | P1 | Bound search production and add fair paging with truthful result limits | #361 |
| [#368](https://github.com/Darshan2104/Garuda-openagent/issues/368) | P2 | Add opt-in source-version edit preconditions and inspectable model edit contracts | #365, #364 |
| [#369](https://github.com/Darshan2104/Garuda-openagent/issues/369) | P2 | Make post-edit checker availability and diagnostic freshness explicit | #361 |
| [#370](https://github.com/Darshan2104/Garuda-openagent/issues/370) | P2 | Add evidence-preserving investigation scopes and explicit context forks | #361, #364 |
| [#371](https://github.com/Darshan2104/Garuda-openagent/issues/371) | P2 | Retrieve reviewed memory with provenance, freshness and supersession | #361 |
| [#372](https://github.com/Darshan2104/Garuda-openagent/issues/372) | P2 | Expose required MCP readiness and bounded reconnect lifecycle | #360 |
| [#373](https://github.com/Darshan2104/Garuda-openagent/issues/373) | P2 | Add authorized MCP resource and prompt discovery through existing lifecycle owners | #372, #361 |
| [#374](https://github.com/Darshan2104/Garuda-openagent/issues/374) | P2 | Inspect foreign instruction formats and explain effective configuration conflicts | #359 |
| [#375](https://github.com/Darshan2104/Garuda-openagent/issues/375) | P1 | Make approval provenance and ceiling enforcement consistent across interfaces | #360 |
| [#376](https://github.com/Darshan2104/Garuda-openagent/issues/376) | P2 | Show workspace ownership, worker progress and safe reconciliation in one read model | #360, #361, #375 |
| [#377](https://github.com/Darshan2104/Garuda-openagent/issues/377) | P2 | Preserve web source provenance and improve bounded search/extraction fidelity | #361 |
| [#378](https://github.com/Darshan2104/Garuda-openagent/issues/378) | P1 | Persist bounded recovery tick health separately from scheduler installation | #360 |
| [#379](https://github.com/Darshan2104/Garuda-openagent/issues/379) | P2 | Add an optional verified macOS LaunchAgent backend for recovery ticks | #378 |
| [#380](https://github.com/Darshan2104/Garuda-openagent/issues/380) | P2 | Prove oh-my-pi as a distinct versioned ACP runtime before advertising support | #359, #360 |
| [#381](https://github.com/Darshan2104/Garuda-openagent/issues/381) | P2 | Add optional workspace-owned language-server navigation and fresh diagnostics | #365, #369, #360 |
| [#382](https://github.com/Darshan2104/Garuda-openagent/issues/382) | P2 | Preview and apply version-bound semantic WorkspaceEdits through Garuda authority | #381, #368, #361 |
| [#383](https://github.com/Darshan2104/Garuda-openagent/issues/383) | P2 | Plan bounded AST codemods with versioned preview and explicit apply results | #368, #361 |
| [#384](https://github.com/Darshan2104/Garuda-openagent/issues/384) | P2 | Add independently owned editing workers with checked integration after typed handoffs | #357, #363, #361, #362 |
| [#385](https://github.com/Darshan2104/Garuda-openagent/issues/385) | P2 | Add an opt-in read-only advisor at bounded review checkpoints | #362, #363, #360 |
| [#386](https://github.com/Darshan2104/Garuda-openagent/issues/386) | P2 | Inject trusted conditional reminders through existing hooks and safe turn boundaries | #364, #360 |
| [#387](https://github.com/Darshan2104/Garuda-openagent/issues/387) | P2 | Add optional confined code cells that call tools through the ordinary runner | #363, #361, #375 |
| [#388](https://github.com/Darshan2104/Garuda-openagent/issues/388) | P2 | Add optional DAP tools for agent-owned debuggees with explicit effect authority | #360, #361, #375 |
| [#389](https://github.com/Darshan2104/Garuda-openagent/issues/389) | P2 | Wire native live-token streaming through the existing loop without weakening lifecycle semantics | #358, #363, #360 |

## Delivery contract

Use one isolated issue worktree and focused branch at a time. Preserve user-owned work. Validate a bug regression against the original owner boundary, implement the complete issue contract, self-review, run narrow checks then required broader checks, and open a PR with exact evidence and residual limits. Larger issues can use several coherent PRs; a design-only change does not complete an implementation issue.

Use shared setup and protocols rather than duplicate entry-point wiring. Keep native inference Model separate from external AgentRuntime, reviews separate from authorized verification, memory separate from authority, and guardrails separate from confinement. New public behavior requires architecture/reference documentation, a feature row and an executable use-case demonstration.

## Continuation contract

Existing built-in recovery resumes only eligible new sequential Codex CLI flows after fresh account/version-bound quota evidence. It preserves step/review receipts and refuses uncertain replay. It does not restore this external conversation's hidden state or cover Claude/protected/dashboard/parallel flows.

The private operator campaign dispatcher is a separate scheduling concern: it selects the next dependency-ready issue, checks actual owner/process/flow/PR state, launches at most one admitted implementation flow, and leaves quota continuation to Garuda. It must not dispatch on missing/uncertain evidence or duplicate already published PRs. Cron configuration alone is not proof that a tick or continuation succeeded. Host sleep/offline periods delay execution.

## Validation

Deterministic local incident fixtures and real entry-point contracts come first. Use existing ablation/matrix/paired-report provenance. Unknown usage/cost stays unknown; no paid repeated-trial budget is assumed. Live model/harness, browser, Docker, tmux, Harbor and OS integrations must be reported according to actual evidence.

Completion requires issue acceptance criteria, a reviewable implementation, relevant test results and linked PRs. The tracker remains open for unfinished or unverified work.

