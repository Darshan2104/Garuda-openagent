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
- Recovery classifies from persisted records only: resumable sources resume, prepared switches roll back first, identity-matched orphans are reaped and verified dead, ambiguous terminals refuse, and no exit code ever becomes a success claim. It runs on resume only, after the resuming run's lease; indeterminate liveness, identity, or owner state is non-recoverable without operator action.
- Generated context files are versioned Markdown+frontmatter (v1): required task/source_runtime, bounded lists, unknown fields round-trip, unknown versions rejected. Raw transcripts, reasoning, and secrets never belong in them.
- `ContextPackManager` is the sole writer of generated context files: deterministic compile from card/session/git evidence, atomic publish, any other target refused, briefs budgeted with provenance intact. `RunState` syncs at checkpoint and after compaction; resume restores the persisted card first so pack facts survive compaction and restart.
- Pack writes scrub secrets automatically (flagged, never silent) with best-effort pattern redaction over every string/key including nested unknown fields and full PEM blocks, rebuilt from the scrubbed map; oversize bodies, escaping paths, or reasoning markers block. Redaction transforms in-memory pack text only and cannot reach durable docs.
- The ACP v1 wire subset is owned, not vendored: newline-delimited JSON-RPC with numeric protocol version 1, required session workspace/MCP parameters, structured prompt blocks, `sessionUpdate`-keyed updates, permission as an agent-to-client request answered exactly once (never widened to `allow_always`; unadvertised client methods refused), one subprocess per agent in its own process group, minimal child environment with explicit extras, stderr as diagnostics only, and close() that always reaps.
- Execution authority assigns exactly one owner per tool family; strict policies fail closed when the agent cannot honor them, and the map snapshots into session capability records. It records negotiated intent from agent-declared extension fields; it is not an enforcement boundary.
- ACP normalization is per-session and stateful: causal order beats arrival order, partials emit once with no duplicate final, turn completion reopens on new_turn while cancellation/failure terminate, and raw records stay redacted diagnostics.
- One generic AcpRuntime carries every adapter through the shared conformance suite against the fake test server; version mismatches fail at handshake, and cross-process resume stays out until the wire grows the methods for it.
- Approvals flow through one broker: ceilings decide, asks park with timeout and disconnect denial, every outcome persists to the session, and strict gaps refuse before running.
- Discovery probes only what trusted manifests declare (version/auth argv) with a minimal child env: missing tools explain setup, unknown versions display as unknown, login state is never guessed, and users can disable any runtime.
- Vendor adapters ship bare adapter binaries (never npx auto-download): Claude Code and Codex run on the user's own subscription login with credential paths documented as untouchable, login state stays unknown (no login probe ships), and the wire subset limits are written down.
- Shipped adapter manifests are package configuration and join the one trusted runtime catalog; a global `runtimes:` entry with the same id replaces them. Adapter children get only PATH/HOME/LANG, so API-key variables and custom vendor config dirs never reach them and the docs do not advise exporting keys. Adapters take an absolute session root; product launch of ACP harnesses is not wired at this layer and selection refuses loudly.
- Cursor and OpenCode follow the same shape over native subcommands; unavailable ACP paths refuse loudly (never silent fallback) and version mismatches name the upgrade.
- Pi, Goose, and user-configured servers ride the generic path: same wire contract and conformance, but non-builtin commands carry an explicit no-vendor-guarantees warning.
- One runtime-registry builder: `agents/setup.py::build_runtime_catalog()` owns shipped manifests, global overrides, advisory project refs, and disablement; `prepare_runtime_catalog()` and `acp.catalog.shared_registry()` only feed it inputs. `adapter_for_registry` discovers the one resolved manifest and binds its accepted executable.
- Auth UX presents, never performs: login flows are manifest-declared user actions, missing credentials yield guidance, quota is pass-through-or-unknown, vendor policy governs use, and Python code may not name credential stores. The gate is a token-level AST scan of the whole `garuda/` package (string constants and folded `+` chains outside docstrings, secret-store imports such as `keyring`, token-helper identifiers) with a self-test of known bypass shapes; it is a regression tripwire, not proof that no code path can reach a credential.
- CLI runtime controls preview by default and mutate only on explicit flags: handoff needs --confirm, --runtime defaults to native, and every command renders text and JSON diagnostics.
- Dashboard runtime controls are pure read models plus write-gated prepares: the UI renders discovered health, auth guidance, diffs, and recovery reports exactly as returned, with unknown states displayed, never invented.
- SDK and JSON-RPC runtimes default to native with identical behavior; explicit harnesses run ACP turns with wrapped results and held sessions, methods are versioned (runtime/v1), and manifests resolve per request so jobs share no registry state.
- Traces gain lanes, not rewrites: unified segments overlay identity/authority/recovery on the untouched native rebuild, normalized ACP trails persist per session, and exports carry counts only.
- The contract matrix generates scenarios from declared capabilities with skips named, not hidden; interactive and never-answering profiles prove their lifecycle under driven checks instead of the unattended suite; any failure blocks support.
- Live harness checks are gated, capped, and reported: env-selected harnesses only, one trivial prompt in a fixture workspace, exact harness/version/auth/elapsed in the report, and CI spends nothing by never opting in.
- Workspace leases live outside the workspace with heartbeat-TTL liveness: one mutating owner, read-only sharing, audited stale takeover that replaces only the lease file, corrupt leases fail closed, and parallel worktrees isolate by real path.
- Git and the filesystem are the delta truth: baselines fingerprint preexisting dirt separately, diffs clip inline but persist fully, ACP hints are reconciled (never applied), and only read-only git verbs run. Attribution is possible only where the host path is the mutated tree (local, sandbox, tmux, bind-mounted docker); remote is recorded `unsupported_nonlocal`, a non-repo workspace `unsupported_nonrepo`, and unknown kinds fail closed. Every entry point that persists a session goes through `workspace/evidence.py`: it persists the baseline before any prompt or refuses, and its verifier, finish, and close consume that exact record or fail closed; a git failure is an error, never an empty delta. The verifier gates on the record being readable and attaches the delta as evidence; it does not judge delta contents. Resume starts a fresh baseline; SDK `Conversation` and `recipe run` carry no baseline yet; the CLI handoff passes `workspace=` and carries the recorded delta.
- Recovery signals only a persisted Garuda-launched process-group leader bound to the session runtime whose recorded start-time/command identity still matches; a recycled PID is retired without a signal. It refuses while a live lease names the session or the recorded owning Garuda process is alive, and audits checkpoint, trail, runtime identity, and ACP authority before any signal. Descendants outside the child's group are out of reach (guardrail, not sandbox). Cancellation audits are append-only evidence, written best-effort without ever blocking the cancel; a failed write surfaces afterwards as a typed error.

## 2026-09-25 — trusted runtime selection is a shared launch gate

- `prepare_runtime_catalog()` is the only CLI/SDK runtime builder. It reads
  manifests and `disabled_runtimes` from the trusted global settings file,
  resolves project aliases only against those manifests, and feeds the same
  disabled set to discovery and selection.
- Runtime settings parsing is fail-closed: a malformed or unreadable global
  file is a configuration error, never an empty set that could reactivate a
  disabled executable. Project `disabled_runtimes` remains recommendation-only
  and disabled built-in stubs stay visible with their policy annotation.
- Project runtime settings fail closed on authority, not on advice: a project
  `command` or capability widening refuses the run, while a malformed
  `disabled_runtimes` or alias entry is ignored with a warning so a repository
  cannot block every run through advisory settings.
- Building the catalog and selecting a runtime execute nothing. Version and
  auth probes run only when a list/inspect caller asks for discovery, with
  stdin closed.
- Before the ACP launch facade was wired, selecting a configured ACP runtime
  refused before any toolkit, workspace, prompt, or process was started; it
  never silently fell back to the native runtime. `garuda run` reports a
  refused selection as a message with exit status 2.
- `chat`, `serve`, the web dashboard, and `recipe run` accept no runtime
  selection and always run the native loop, which cannot be disabled, so they
  do not consult runtime settings.

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
  exact absolute executable a discovery record accepted
  (`adapter_for_discovered`). It never performs a second `PATH` lookup and
  rejects missing, relative, directory, or non-executable paths, so a later
  `PATH` change cannot substitute a different process at launch. Replacing
  the file at the accepted path is out of scope (binding, not sandbox).
- Unsupported agent-to-client methods, including Cursor's blocking
  `cursor/ask_question` and `cursor/create_plan`, fail closed with
  METHOD_NOT_FOUND.
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
- A confirmed handoff acknowledges before it delivers: the handoff state and
  the target's active segment (native session id, authority snapshot) are one
  locked write, the target's child is then recorded, the source closes, and
  only then is the package sent as the target's first prompt (bounded). A
  failure before acknowledgement rolls back to the source; a failure after it
  is a target failure (target closed, `target_state: failed`), never a rollback
  over changes the target may have made. The launch binds the exact executable
  the pre-transaction discovery accepted.
- The CLI handoff is one-shot and ends consistent: the target is closed and
  reaped and `target_state: closed` recorded. Ownership stays with the target:
  `recover()` classifies an ACP-active session `external` (children still
  reaped) and every native resume path refuses it. The way back is explicit:
  `reclaim_native` (`garuda runtime reclaim`) re-appends the native segment in
  one locked write (the checks repeat inside it), only on process evidence that
  the target stopped: no lease names the session, recovery leaves no live
  recorded child, the target left a retired child or a `closed`/`failed` state,
  and a native checkpoint exists. A double fault on return-to-source raises
  naming reclaim, which that recorded state satisfies.
- `garuda run --runtime <acp>` and the CLI handoff share the native run's
  invariants through `interfaces/run_guard.py` (workspace lease with heartbeat
  race and release on every path, P0.17 broker approvals: headless deny-all
  audited, TTY prompt) plus a persisted session with the ACP segment, child
  record, and baseline/delta. An ACP run is recorded `completed`, not
  `success`: Garuda does not verify its result.
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

## 2026-09-26 — Contract-matrix support totals are non-vacuous

- Scenario selection is declaration-driven: ACP resume is not counted unless
  an adapter explicitly declares resumability, and a handoff requires a target
  response behavior before it is selected.
- Handoff checks deliver the compiled package through a real target prompt and
  require observable target events and an idle boundary before acknowledging
  ownership; adapters may expose messages, diffs, or tool events.
- Recovery checks start the selected adapter, consume a real turn, persist its
  identity/event evidence, then classify the same session. Vendor rows remain
  simulated and are reported separately from non-simulated support totals.
- The executable gate treats any failed or all-SKIP adapter identity as
  unsupported, including a mutation that forces an all-SKIP report.

## 2026-09-26 — Live smoke reports discovery truth and bounded roundtrips

- The live harness runner uses the discovered executable, version, and auth
  fields in its report; it does not infer authentication from prompt return.
- Discovery, ACP launch/handshake, one prompt, response-event validation, and
  cleanup share one timeout. A successful smoke requires an observable message
  event, not merely a returned turn number.
- Missing binaries produce explicit per-harness skipped reports so an `all`
  sweep remains attributable without turning local installation gaps into
  product failures. The opt-in gate remains absent from CI.
