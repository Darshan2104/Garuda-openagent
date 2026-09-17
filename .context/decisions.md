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

## 2026-09-22 — stack base scope (#83) and Ubuntu reaping

- PR #83 is accepted explicitly as a kitchen-sink stack base, not as a
  P0.3-only change. Vs `main` it carries ~89 files / ~16k lines (docs-contract
  plus dual-model routing, web dashboard, trajectory/observability, model
  bindings, and core churn) because the stack was cut from an unpublished
  worktree. Reviewers must not sign off on "docs contract" while landing a
  dashboard; the per-PR gates (#84-#118 incremental diffs) remain the review
  units, and a future split cherry-picking only `scripts/check_docs.py`,
  `tests/test_docs_contract.py`, and the docs-contract CI job onto `main`
  stays open as follow-up.
- Ubuntu pinned remains the landing gate. Failures in
  `test_killing_a_task_reaps_its_children_not_just_the_launcher` and
  `test_background_process_is_swept_before_verification` were zombies awaiting
  init reaping (SIGKILLed but listed by `pgrep -f` and `os.kill(pid, 0)`),
  not live leaks. Sweep probes and the `_alive` kernel check now exclude STAT Z.

## 2026-09-17 — AgentRuntime protocol v0.1 (P0.4)

- `garuda/runtime/` owns the generic harness contract: `AgentRuntime` Protocol, `RuntimeCapabilities`, `LifecycleState` with explicit allowed transitions, `RuntimeInfo`, typed `AgentRuntimeError` failures, and the normalized `RuntimeEvent` vocabulary (session/turn correlation, `kind` discriminant).
- The generic contract imports no vendor or ACP types; `RuntimeKind.ACP` is a label. ACP subprocess code belongs behind this boundary, never inside it.
- `FakeRuntime` is the deterministic test double with scripted scenarios; `run_conformance_suite` in `tests/test_runtime_conformance.py` is the lifecycle suite every future adapter must pass.
- Only global configuration authorizes a harness executable; project references name a registered id and may narrow capabilities but never define commands, widen capabilities, or carry secrets. One registry instance per job, never process-global.
- Unified sessions version the meta document (v1): legacy sessions resume unchanged and migrate in memory; the migrated form publishes via the atomic locked meta write, unknown future versions fail actionably, and event cursors never regress.
- `run_agent_task` is a facade over `NativeGarudaRuntime`: all entry points run through the boundary with identical execution (same agent call, same teardown); the bridge owns session/unified records, lifecycle, normalized events, and parked approvals only.
- Runtime switches are transactions: acknowledgement requires the source frozen at the boundary (never merely idle), so two mutating owners cannot exist; pre-ack failure or cancellation returns to exactly one resumable owner with a typed event per move. `execute_handoff` runs the full flow against real runtimes with the session store recording prepared/acknowledged/failed.
- Recovery classifies from persisted records only: resumable sources resume, prepared switches roll back first, orphans are reaped and verified dead, ambiguous terminals refuse, and no exit code ever becomes a success claim. Startup/resume runs classification first; indeterminate liveness is non-recoverable without operator action.
- Generated context files are versioned Markdown+frontmatter (v1): required task/source_runtime, bounded lists, unknown fields round-trip, unknown versions rejected. Raw transcripts, reasoning, and secrets never belong in them.
- `ContextPackManager` is the sole writer of generated context files: deterministic compile from card/session/git evidence, atomic publish, any other target refused, briefs budgeted with provenance intact. `RunState` syncs at checkpoint and after compaction; resume restores the persisted card first so pack facts survive compaction and restart.
- Pack writes scrub secrets automatically (flagged, never silent) with best-effort pattern redaction over every string/key including nested unknown fields and full PEM blocks, rebuilt from the scrubbed map; oversize bodies, escaping paths, or reasoning markers block. Redaction transforms in-memory pack text only and cannot reach durable docs.
- The ACP v1 wire subset is owned, not vendored: newline-delimited JSON-RPC with numeric protocol version 1, required session workspace/MCP parameters, structured prompt blocks, exposed bidirectional agent requests, one subprocess per agent in its own process group, minimal child environment with explicit extras, stderr as diagnostics only, and close() that always reaps.
- Execution authority assigns exactly one owner per tool family; strict policies fail closed when the agent cannot honor them, and the map snapshots into session capability records so resumed sessions prove prior ownership.
- ACP normalization is per-session and stateful: causal order beats arrival order, partials emit once with no duplicate final, turn completion reopens on new_turn while cancellation/failure terminate, and raw records stay redacted diagnostics.
- One generic AcpRuntime carries every adapter through the shared conformance suite against the fake test server; version mismatches fail at handshake, and cross-process resume stays out until the wire grows the methods for it.
- Approvals flow through one broker: ceilings decide, asks park with timeout and disconnect denial, every outcome persists to the session, and strict gaps refuse before running.
- Workspace leases live outside the workspace with heartbeat-TTL liveness: one mutating owner, read-only sharing, audited stale takeover that replaces only the lease file, corrupt leases fail closed, and parallel worktrees isolate by real path.
- Git and the filesystem are the delta truth: baselines fingerprint preexisting dirt separately, diffs clip inline but persist fully, ACP hints are reconciled (never applied), and only read-only git verbs run. A local session must persist its baseline before any prompt; its verifier, handoff, and close path consume that exact record or fail closed. Non-local attribution is recorded as unsupported rather than silently omitted.
