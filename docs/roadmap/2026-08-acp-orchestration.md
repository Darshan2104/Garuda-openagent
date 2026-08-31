# Roadmap: ACP orchestration and unified context

**Status:** approved design, implementation not started

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

### P2 — platform expansion

9. Expose Garuda as an ACP server for ACP-native editors.
10. Add direct model transports only where vendor documentation supports required authentication, tool calling, streaming, and session semantics.
11. Optional routing policies based on declared capability, user preference, budget, availability, and measured quality.

## Issue-ready work breakdown

| Order | Epic | Child work |
|---|---|---|
| 0 | Repository standards | Documentation migration; AGENTS/context contract; docs-link CI. |
| 1 | Runtime foundation | Runtime protocol; registry/config; unified session persistence; native runtime adapter. |
| 2 | Context and handoffs | Schemas; deterministic compiler; transactional switch; secret/size validation. |
| 3 | ACP foundation | Python client; capability/authority negotiation; protocol normalizer; fake ACP conformance server. |
| 4 | Safety and repository state | Workspace lease; permission broker; Git baseline/diff; recovery and cancellation. |
| 5 | Harness adapters | Claude/Codex; Cursor/OpenCode; Pi/Goose/registry discovery; health/auth UX. |
| 6 | Product surfaces | CLI; web dashboard; SDK/service; persisted trace reader. |
| 7 | Quality | Contract suites; opt-in real integrations; eval matrix; performance and observability. |
| 8 | Expansion | Garuda ACP server; supported direct transports; opt-in policy router. |

The detailed parent/child payloads are in the [issue catalog](issue-catalog.md).
Each child issue states scope, non-goals, dependencies, interface changes,
acceptance criteria, tests, security posture, telemetry, documentation, and
rollout plan. GitHub issue links are added after issue creation.

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
