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
- Runtime switches are transactions: acknowledgement requires the source frozen at the boundary (never merely idle), so two mutating owners cannot exist; pre-ack failure or cancellation returns to exactly one resumable owner with a typed event per move.
- Recovery classifies from persisted records only: resumable sources resume, prepared switches roll back first, orphans are reaped and verified dead, ambiguous terminals refuse, and no exit code ever becomes a success claim.
- Generated context files are versioned Markdown+frontmatter (v1): required task/source_runtime, bounded lists, unknown fields round-trip, unknown versions rejected. Raw transcripts, reasoning, and secrets never belong in them.
- `ContextPackManager` is the sole writer of generated context files: deterministic compile from card/session/git evidence, atomic publish, any other target refused, briefs budgeted with provenance intact.
- Pack writes scrub secrets automatically (flagged, never silent) and block on oversize bodies, escaping paths, or reasoning markers; redaction transforms in-memory pack text only and cannot reach durable docs.
- The ACP wire subset is owned, not vendored: JSON-RPC with Content-Length framing pinned to a negotiated version, one subprocess per agent in its own process group, minimal child environment with explicit extras, stderr as diagnostics only, and close() that always reaps.
- Execution authority assigns exactly one owner per tool family; strict policies fail closed when the agent cannot honor them, and the map snapshots into session capability records so resumed sessions prove prior ownership.
- ACP normalization is per-session and stateful: causal order beats arrival order, partials emit once with no duplicate final, turn completion reopens on new_turn while cancellation/failure terminate, and raw records stay redacted diagnostics.
- One generic AcpRuntime carries every adapter through the shared conformance suite against the fake test server; version mismatches fail at handshake, and cross-process resume stays out until the wire grows the methods for it.
- Approvals flow through one broker: ceilings decide, asks park with timeout and disconnect denial, every outcome persists to the session, and strict gaps refuse before running.
- Discovery probes only what trusted manifests declare (version/auth argv) with a minimal child env: missing tools explain setup, unknown versions display as unknown, login state is never guessed, and users can disable any runtime.
- Vendor adapters ship bare adapter binaries (never npx auto-download): Claude Code and Codex run on the user's own subscription login with credential paths documented as untouchable, login state stays unknown until a run, and the wire subset limits are written down.
- Cursor and OpenCode follow the same shape over native subcommands; unavailable ACP paths refuse loudly (never silent fallback) and version mismatches name the upgrade.
- Pi, Goose, and user-configured servers ride the generic path: same wire contract and conformance, but non-builtin commands carry an explicit no-vendor-guarantees warning.
- Auth UX presents, never performs: login flows are manifest-declared user actions, missing credentials yield guidance, quota is pass-through-or-unknown, vendor policy governs use, and Python code may not name credential stores.
- CLI runtime controls preview by default and mutate only on explicit flags: handoff needs --confirm, --runtime defaults to native, and every command renders text and JSON diagnostics.
- Dashboard runtime controls are pure read models plus write-gated prepares: the UI renders discovered health, auth guidance, diffs, and recovery reports exactly as returned, with unknown states displayed, never invented.
- SDK and JSON-RPC runtimes default to native with identical behavior; explicit harnesses run ACP turns with wrapped results and held sessions, methods are versioned (runtime/v1), and manifests resolve per request so jobs share no registry state.
- Traces gain lanes, not rewrites: unified segments overlay identity/authority/recovery on the untouched native rebuild, normalized ACP trails persist per session, and exports carry counts only.
- Runtime metrics record per-adapter phases with triage buckets while native accounting stays pinned; support bundles redact every string and tally kinds instead of copying payloads.
- Inbound ACP serving reuses the runtime boundary with per-instance sessions: stdio only, version-checked both directions, unknown methods fail closed, and no listener exists to misconfigure.
- Direct transports need six admission artifacts (https citation, capabilities, double, opt-in test, cost semantics, migration notes); anything private is inadmissible and subscription access stays behind ACP.
- The contract matrix generates scenarios from declared capabilities with skips named, not hidden; interactive and never-answering profiles prove their lifecycle under driven checks instead of the unattended suite; any failure blocks support.
- Live harness checks are gated, capped, and reported: env-selected harnesses only, one trivial prompt in a fixture workspace, exact harness/version/auth/elapsed in the report, and CI spends nothing by never opting in.
- Eval comparisons keep model and harness as separate dimensions with unknown costs never zero-filled, prompts hashed instead of stored, per-cell trial counts, and no vendor claim from thin cells.
- Workspace leases live outside the workspace with heartbeat-TTL liveness: one mutating owner, read-only sharing, audited stale takeover that replaces only the lease file, corrupt leases fail closed, and parallel worktrees isolate by real path.
- Git and the filesystem are the delta truth: baselines fingerprint preexisting dirt separately, diffs clip inline but persist fully, ACP hints are reconciled (never applied), and only read-only git verbs run.
