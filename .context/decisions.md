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
- The ACP wire subset is owned, not vendored: JSON-RPC over bounded NDJSON pinned to public protocol version 1, one subprocess per agent in its own process group, minimal child environment with explicit extras, stderr as diagnostics only, and close() that always reaps.
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
- The contract matrix generates scenarios from declared capabilities with skips named, not hidden; interactive and never-answering profiles prove their lifecycle under driven checks instead of the unattended suite; any failure blocks support.
- Workspace leases live outside the workspace with heartbeat-TTL liveness: one mutating owner, read-only sharing, audited stale takeover that replaces only the lease file, corrupt leases fail closed, and parallel worktrees isolate by real path.
- Git and the filesystem are the delta truth: baselines fingerprint preexisting dirt separately, diffs clip inline but persist fully, ACP hints are reconciled (never applied), and only read-only git verbs run. A local session must persist its baseline before any prompt; its verifier, handoff, and close path consume that exact record or fail closed. Non-local attribution is recorded as unsupported rather than silently omitted.
- Recovery signals only a persisted Garuda-launched child/process-group identity bound to the session runtime. It audits a readable message checkpoint and runtime identity before resume, validates ACP authority snapshots, and refuses indeterminate liveness before or after reaping. Turn, switch, and process cancellation boundaries persist their audit record before terminal transition.

## 2026-09-25 — trusted runtime selection is a shared launch gate

- `prepare_runtime_catalog()` is the only CLI/SDK runtime builder. It reads
  manifests and `disabled_runtimes` from the trusted global settings file,
  resolves project aliases only against those manifests, and feeds the same
  disabled set to discovery and selection.
- Runtime settings parsing is fail-closed: a malformed or unreadable global
  file is a configuration error, never an empty set that could reactivate a
  disabled executable. Project `disabled_runtimes` remains recommendation-only
  and disabled built-in stubs stay visible with their policy annotation.
- Before the ACP launch facade was wired, selecting a configured ACP runtime
  refused before any toolkit, workspace, prompt, or process was started; it
  never silently fell back to the native runtime.

## 2026-09-25 — ACP adapters speak the public v1 stdio contract

- ACP subprocesses use bounded NDJSON, numeric `protocolVersion: 1`, absolute
  `cwd` plus `mcpServers` in `session/new`, and text content blocks in
  `session/prompt`; Content-Length framing and string prompts are not accepted.
- ACP is bidirectional JSON-RPC. Agent-originated client requests receive a
  controller response or an explicit fail-closed JSON-RPC error; they cannot
  be silently queued as notifications and hang a vendor process.
- Vendor conformance uses a strict v1 fixture that rejects the previous
  private transport and request shapes. Installed vendor smoke remains opt-in
  and creates a session only; it never sends subscription-consuming work.

## 2026-09-25 — ACP launch binds discovery to execution

- The shared ACP adapter factory replaces a manifest's bare command with the
  exact absolute executable accepted during discovery. It rejects missing,
  relative, or non-executable paths, so a later `PATH` change cannot substitute
  a different process at launch.
- Cursor uses the documented `agent acp` command (with the usual
  `~/.local/bin/agent` installation location). As with every vendor adapter,
  setup guidance may name the user's CLI but never reads or proxies its
  credentials.

## 2026-09-25 — CLI execution shares the runtime authority boundary

- `garuda runtime list`, `inspect`, handoff confirmation, and `garuda run
  --runtime` resolve through one configured registry: native and built-ins plus
  trusted global manifests, with project aliases constrained to references.
  Duplicate IDs, untrusted command-bearing project data, and globally disabled
  targets refuse before discovery can present them as launchable or a process
  can start.
- A confirmed handoff generates and sends the handoff package to the started
  target before acknowledgement. Delivery failure closes the target and rolls
  the source back; successful CLI delivery is supervised through its target
  turn and then explicitly closed/reaped when the command exits.

## 2026-09-26 — SDK and JSON-RPC runtime authority stays trusted and transactional

- JSON-RPC runtime methods resolve only the server's trusted configured registry;
  request parameters select an existing runtime id and cannot define commands.
- SDK ACP approval requests enter the persisted `ApprovalBroker`, including the
  permission ceiling and fail-closed audit path, before an adapter receives a
  response.
- ACP-to-ACP SDK switches deliver the compiled handoff package inside the one
  pause/checkpoint/start/ack transaction. Native-to-ACP switching through a
  conversation refuses until it can use that same transaction, preventing two
  mutating owners.

## 2026-09-26 — External trace lanes use per-tenure event identity

- Every persisted ACP event carries the immutable ACP session identifier for
  the tenure that produced it. Cross-runtime readers group external records by
  that identifier rather than treating one shared file as one lane.
- ACP cursors describe the external stream and never become ranges in the
  native `events.jsonl` stream. Native ranges remain based only on native
  event cursors; ambiguous legacy external records are not copied into every
  lane.
- SDK, CLI, and transactional handoff adapters receive the session store and
  external persistence directory at construction, so production execution
  populates the same trace lanes as direct adapter tests.

## 2026-09-26 — Dashboard runtime controls use the shared registry

- The web dashboard resolves runtimes through the same trusted global settings,
  project aliases, built-ins, and disablement rules as the CLI and SDK.
- Handoff preview and prepare resolve and availability-check the target before
  reading or writing handoff state. Unknown, disabled, and unavailable ACP
  targets fail closed; native remains the explicit in-process target.
- Browser coverage exercises configured, disabled, unavailable, and unknown
  targets so the visible controls cannot drift from the route policy.
