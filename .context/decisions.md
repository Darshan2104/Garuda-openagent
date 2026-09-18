# Durable decisions

## 2026-08-31 — external coding agents are runtimes, not models

Garuda will directly orchestrate ACP-compatible coding agents rather than place one agent loop behind Garuda's `Model` protocol. This prevents conflicting ownership of tools, permissions, retries, context, and session restoration.

## 2026-08-31 — use an ACP-first integration strategy

The first subscription lane uses user-installed, authenticated official CLIs or ACP adapters for Claude Code, Codex, Cursor, OpenCode, Pi, Goose, and compatible agents. Direct transports are later work and require documented, supported public vendor APIs.

## 2026-08-31 — context uses committed durable files and generated handoffs

Architecture, decisions, discoveries, and conventions are committed. Current task and handoff state are generated per session, gitignored, and retained in the Garuda session store. One mutating session owns one workspace; parallel mutation uses separate worktrees.

## 2026-09-16 — dual-model delegation and routing boundaries

- Reasoning and collection are model roles inside `NativeGarudaRuntime`; external harnesses remain `AgentRuntime` implementations, never `Model` transports.
- Collection is controller-requested via explicit delegation, not per-turn automatic routing.
- Initial harness routing may use an optional classifier fallback; mid-run switching is explicit and transactional only.
- Project configuration cannot authorize providers, endpoints, executables, credential sources, or looser ceilings.
- Unknown cost is recorded as unknown, never zero; release claims use total-trajectory paired comparisons.

## 2026-09-17 — AgentRuntime protocol v0.1 (P0.4)

- `garuda/runtime/` owns the generic harness contract: `AgentRuntime` Protocol, `RuntimeCapabilities`, `LifecycleState` with explicit allowed transitions, `RuntimeInfo`, typed `AgentRuntimeError` failures, and the normalized `RuntimeEvent` vocabulary (session/turn correlation, `kind` discriminant).
- The generic contract imports no vendor or ACP types; `RuntimeKind.ACP` is a label. ACP subprocess code belongs behind this boundary, never inside it.
- `FakeRuntime` is the deterministic test double with scripted scenarios; `run_conformance_suite` in `tests/test_runtime_conformance.py` is the lifecycle suite every future adapter must pass.
- Only global configuration authorizes a harness executable; project references name a registered id and may narrow capabilities but never define commands, widen capabilities, or carry secrets. One registry instance per job, never process-global.
- Unified sessions version the meta document (v1): legacy sessions resume unchanged and migrate in memory; the migrated form publishes via the atomic locked meta write, unknown future versions fail actionably, and event cursors never regress.
- `run_agent_task` is a facade over `NativeGarudaRuntime`: all entry points run through the boundary with identical execution (same agent call, same teardown); the bridge owns session/unified records, lifecycle, normalized events, and parked approvals only.
- Generated context files are versioned Markdown+frontmatter (v1): required task/source_runtime, bounded lists, unknown fields round-trip, unknown versions rejected. Raw transcripts, reasoning, and secrets never belong in them.
- `ContextPackManager` is the sole writer of generated context files: deterministic compile from card/session/git evidence, atomic publish, any other target refused, briefs budgeted with provenance intact.
