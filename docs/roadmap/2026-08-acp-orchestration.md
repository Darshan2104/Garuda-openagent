# Roadmap: ACP orchestration and unified context

**Status:** approved and decomposed into GitHub issues; implementation not started

**Owner:** Garuda maintainers
**Planning date:** 2026-08-31

## Outcome

Garuda becomes a control plane for both its own native LLM harness and external coding harnesses. A user can select a logged-in Claude Code, Codex, Cursor, OpenCode, Pi, Goose, or another ACP-compatible agent without handing Garuda their subscription credentials. Sessions can pause, hand off, and resume with a common, repository-local context pack and a consistent audit trail.

The first release is **supervisor mode**. Garuda does not reimplement the external agent's loop and does not place an external agent behind `Model`. Supported direct inference transports remain a separate later lane and must use documented public vendor APIs only.

## Why this boundary

An external coding agent already has its own context, tool execution, permissions, retry behavior, and session lifecycle. Wrapping it as a Garuda model would create two competing agent loops.

```text
Unified UI / CLI / SDK
          │
Garuda Orchestrator
  ├─ session manager
  ├─ context-pack manager
  ├─ agent router
  ├─ permission broker
  ├─ diff manager
  ├─ event normalizer
  └─ verifier
          │
    AgentRuntime protocol
     ┌────┴──────────┐
     │               │
ACP runtime      Native Garuda runtime
     │               │
ACP agents       Garuda loop + Model transports
```

ACP is the universal first integration surface. Garuda is the ACP client and external harnesses are ACP servers. Native vendor protocols may be introduced later only when they expose capability or reliability that ACP cannot represent.

## Non-goals

- Copying OAuth tokens, reverse engineering private subscription endpoints, or treating subscription quota as API entitlement.
- Translating opaque internal transcripts or reasoning between vendors.
- Silently switching models/harnesses during an active turn.
- Claiming that Garuda can enforce an external harness's internal sandbox or permissions when the protocol does not expose control.
- Building a hosted multi-tenant service in this roadmap.

## Unified context contract

```text
.context/
├── architecture.md     # committed system boundary summary
├── decisions.md        # committed ADR-style decisions
├── discoveries.md      # committed evidence and constraints
├── conventions.md      # committed repository working rules
├── current-task.md     # generated, gitignored active state
└── handoff.md          # generated, gitignored transfer package
```

`ContextPackManager` is the only writer of generated files. Durable files are reviewed repository content. Runtime files are Markdown with versioned YAML frontmatter and checkpointed into the existing session store. One mutating session owns a workspace; concurrent mutation requires isolated worktrees.

At a switch boundary Garuda checkpoints the active backend, captures repository delta and verification evidence, writes/validates the handoff, starts the target runtime, and asks it to read `AGENTS.md` plus `.context/`. If startup fails, the old backend session remains resumable.

## Core contracts

### `AgentRuntime`

The runtime interface must support discovery, capabilities, authentication status, start, resume, prompt, cancellation, close, normalized events, and permission replies. It must expose its own native session ID and version. The existing Garuda loop becomes `NativeGarudaRuntime`; ACP implementations become `AcpRuntime` instances.

### Unified session

A Garuda session maps to one active runtime session and records:

- Garuda session ID, runtime ID, native session ID, version, and capability snapshot;
- workspace/worktree, baseline commit, initial dirty-state fingerprint, and context revision;
- permission ceiling, execution-authority allocation, normalized event cursor, and lifecycle state;
- handoff attempts, verification results, cancellation reason, and recovery target.

### Permissions, tools, and diffs

ACP negotiation assigns each tool family to exactly one owner. Garuda brokers approval requests, applies its policy ceiling, and refuses strict configurations an adapter cannot honor. ACP diff events provide progressive UI feedback; workspace/Git deltas are the final truth. Existing uncommitted user changes form the baseline and are never reverted by Garuda.

## Delivery sequence

### P0 — foundation and safety

1. Repository contract, documentation, and durable context baseline.
2. `AgentRuntime` protocol, runtime registry, and unified session schema.
3. Transactional context pack and handoff state machine.
4. ACP subprocess client, JSON-RPC lifecycle, capability negotiation, and fake-agent conformance fixtures.
5. Workspace leases, permission ceiling, dirty baseline, and authoritative diff tracking.

### P1 — usable integrations

6. First-party configuration/discovery for Claude ACP, Codex ACP, Cursor Agent, OpenCode, Pi, and Goose; no automatic installation or secret import.
7. Unified UI/CLI/SDK runtime selection, health, switch preview, approval cards, normalized trace, and recovery controls.
8. Reliability, observability, and benchmark/conformance coverage across fake agents and opt-in real harnesses.
9. P1 routing scope is initial runtime selection (explicit > profile > deterministic rule > optional classifier > default) plus explicit transactional handoff at a safe boundary. Autonomous mid-run policy routing stays in P2 and must not block P1 initial-only routing.

### P2 — platform expansion

9. Expose Garuda as an ACP server for ACP-native editors.
10. Add direct model transports only where vendor documentation supports required authentication, tool calling, streaming, and session semantics.
11. Optional routing policies based on declared capability, user preference, budget, availability, and measured quality.

## Issue-ready work breakdown

| Order | Epic | Child work |
|---|---|---|
| 0 | [Repository standards (#6)](https://github.com/Darshan2104/Garuda-openagent/issues/6) | [#7](https://github.com/Darshan2104/Garuda-openagent/issues/7) docs migration; [#8](https://github.com/Darshan2104/Garuda-openagent/issues/8) AGENTS/context; [#9](https://github.com/Darshan2104/Garuda-openagent/issues/9) docs CI. |
| 1 | [Runtime foundation (#10)](https://github.com/Darshan2104/Garuda-openagent/issues/10) | [#11](https://github.com/Darshan2104/Garuda-openagent/issues/11) protocol; [#12](https://github.com/Darshan2104/Garuda-openagent/issues/12) registry; [#13](https://github.com/Darshan2104/Garuda-openagent/issues/13) sessions; [#14](https://github.com/Darshan2104/Garuda-openagent/issues/14) native bridge. |
| 2 | [Context and handoffs (#15)](https://github.com/Darshan2104/Garuda-openagent/issues/15) | [#16](https://github.com/Darshan2104/Garuda-openagent/issues/16) schemas; [#17](https://github.com/Darshan2104/Garuda-openagent/issues/17) compiler; [#18](https://github.com/Darshan2104/Garuda-openagent/issues/18) switch; [#19](https://github.com/Darshan2104/Garuda-openagent/issues/19) validation. |
| 3 | [ACP foundation (#20)](https://github.com/Darshan2104/Garuda-openagent/issues/20) | [#21](https://github.com/Darshan2104/Garuda-openagent/issues/21) client; [#22](https://github.com/Darshan2104/Garuda-openagent/issues/22) authority; [#23](https://github.com/Darshan2104/Garuda-openagent/issues/23) normalizer; [#24](https://github.com/Darshan2104/Garuda-openagent/issues/24) fake agent. |
| 4 | [Safety and repository state (#25)](https://github.com/Darshan2104/Garuda-openagent/issues/25) | [#26](https://github.com/Darshan2104/Garuda-openagent/issues/26) workspace lease; [#27](https://github.com/Darshan2104/Garuda-openagent/issues/27) permissions; [#28](https://github.com/Darshan2104/Garuda-openagent/issues/28) diff; [#29](https://github.com/Darshan2104/Garuda-openagent/issues/29) recovery. |
| 5 | [Harness adapters (#30)](https://github.com/Darshan2104/Garuda-openagent/issues/30) | [#31](https://github.com/Darshan2104/Garuda-openagent/issues/31) discovery; [#32](https://github.com/Darshan2104/Garuda-openagent/issues/32) Claude/Codex; [#33](https://github.com/Darshan2104/Garuda-openagent/issues/33) Cursor/OpenCode; [#34](https://github.com/Darshan2104/Garuda-openagent/issues/34) Pi/Goose; [#35](https://github.com/Darshan2104/Garuda-openagent/issues/35) auth UX. |
| 6 | [Product surfaces (#36)](https://github.com/Darshan2104/Garuda-openagent/issues/36) | [#37](https://github.com/Darshan2104/Garuda-openagent/issues/37) CLI; [#38](https://github.com/Darshan2104/Garuda-openagent/issues/38) web; [#39](https://github.com/Darshan2104/Garuda-openagent/issues/39) SDK; [#40](https://github.com/Darshan2104/Garuda-openagent/issues/40) trace reader. |
| 7 | [Quality (#41)](https://github.com/Darshan2104/Garuda-openagent/issues/41) | [#42](https://github.com/Darshan2104/Garuda-openagent/issues/42) contracts; [#43](https://github.com/Darshan2104/Garuda-openagent/issues/43) real integrations; [#44](https://github.com/Darshan2104/Garuda-openagent/issues/44) evals; [#45](https://github.com/Darshan2104/Garuda-openagent/issues/45) observability. |
| 8 | [Expansion (#46)](https://github.com/Darshan2104/Garuda-openagent/issues/46) | [#47](https://github.com/Darshan2104/Garuda-openagent/issues/47) Garuda ACP server; [#48](https://github.com/Darshan2104/Garuda-openagent/issues/48) direct transports; [#49](https://github.com/Darshan2104/Garuda-openagent/issues/49) policy router. |

The detailed parent/child payloads are in the [issue catalog](issue-catalog.md).
Each child issue states scope, non-goals, dependencies, interface changes,
acceptance criteria, tests, security posture, telemetry, documentation, and
rollout plan. GitHub tracks each listed child as a sub-issue of its linked epic.

## Definition of done

A P1 release is complete only when a user can select at least two authenticated ACP harnesses, run a task in an isolated workspace, stop at a safe turn boundary, switch harnesses using a validated handoff, inspect normalized events and diffs, and recover from a failed target startup without losing the original session.

No API key, OAuth token, raw reasoning, or unredacted transcript may be written to the Git repository, `.context/`, or normal event views.

## Measurable acceptance targets

- 100% of runtime adapters pass the same fake-agent lifecycle contract suite.
- Zero silent handoff data loss in injected failure tests.
- Every mutating session records a baseline and final repository delta.
- Every denied/expired approval and adapter exit produces a typed event.
- Switching an agent does not create a second active mutating owner of a workspace.
- Direct transport additions each include a vendor-support reference and opt-in integration test.
