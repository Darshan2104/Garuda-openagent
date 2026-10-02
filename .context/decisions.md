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

## 2026-09-29 — paired-trial reports are offline session evidence

- `garuda eval dual-model report` reads only completed native session records;
  it never launches a model, reads provider credentials, or copies raw event
  payloads into its report.
- A report requires an exact representative task assignment, one baseline and
  candidate session per task, and pinned model/price/prompt provenance. Missing
  candidate-call attribution fails release gates rather than being inferred as
  acceptable.
- Existing reports are preserved unless overwrite is explicit. Gate failure is
  recorded in the report; automation opts into a failing exit with
  `--require-passing-gates`.

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
- Policy routing is deterministic and logged: pins win or fail loudly (including budget and mutation gates), unknown/non-finite costs never read free, duplicates refuse, and the decision/rationale persists on the unified session before the runtime starts.
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
- Runtime metrics record per-adapter phases with triage buckets while native accounting stays pinned; support bundles redact every string and tally kinds instead of copying payloads.
- Inbound ACP serving reuses the runtime boundary with per-instance sessions: stdio only, version-checked both directions, unknown methods fail closed, and no listener exists to misconfigure.
- Direct transports need six admission artifacts (https citation, capabilities, double, opt-in test, cost semantics, migration notes); anything private is inadmissible and subscription access stays behind ACP.
- The contract matrix generates scenarios from declared capabilities with skips named, not hidden; interactive and never-answering profiles prove their lifecycle under driven checks instead of the unattended suite; any failure blocks support.
- Live harness checks are gated, capped, and reported: env-selected harnesses only, one trivial prompt in a fixture workspace, exact harness/version/auth/elapsed in the report, and CI spends nothing by never opting in.
- Eval comparisons keep model and harness as separate dimensions with unknown costs never zero-filled, prompts hashed instead of stored, per-cell trial counts, and no vendor claim from thin cells.
- Workspace leases live outside the workspace with heartbeat-TTL liveness: one mutating owner, read-only sharing, audited stale takeover that replaces only the lease file, corrupt leases fail closed, and parallel worktrees isolate by real path.
- Git and the filesystem are the delta truth: baselines fingerprint preexisting dirt separately, diffs clip inline but persist fully, ACP hints are reconciled (never applied), and only read-only git verbs run. Attribution is possible only where the host path is the mutated tree (local, sandbox, tmux, bind-mounted docker); remote is recorded `unsupported_nonlocal`, a non-repo workspace `unsupported_nonrepo`, and unknown kinds fail closed. Every entry point that persists a session goes through `workspace/evidence.py`: it persists the baseline before any prompt or refuses, and its verifier, finish, and close consume that exact record or fail closed; a git failure is an error, never an empty delta. The verifier gates on the record being readable and attaches the delta as evidence; it does not judge delta contents. Resume inherits the resumed session's recorded baseline only when the workspace equals the end state that session's finish recorded (same resolved path, HEAD, status, and dirty-file fingerprints) — commit ancestry alone cannot tell session work from a pull or an interim edit — and otherwise captures fresh with a recorded `baseline_inherit_refused` reason. Interrupted runs record their end state after teardown; opaque dirty paths (submodules, nested repositories) refuse; SDK `Conversation` and `recipe run` carry no baseline yet; the CLI handoff passes `workspace=` and carries the recorded delta.
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
## 2026-09-28 — Handoff targets are recorded from launch

- A handoff target's child is recorded `prepared` right after its process is launched, bound to the handoff attempt because its segment exists only after the acknowledgement: it is written only while the handoff is `prepared` for that `target_runtime` (checked inside the locked write), and the runtime is appended to an append-only `prepared_targets` list so a later attempt naming another target cannot unbind it. `bind_session` promotes that record in place to `live`; a changed identity refuses the bind, which returns ownership to the source.
- Recovery treats `prepared` children as signal candidates behind the same lease, live-owner, and identity gates as `live` ones, so a crash before the acknowledgement (or between it and the bind) leaves a reapable orphan instead of an unnamed one. `reclaim_native` counts them as live. A `prepared` child whose runtime is neither a segment nor in `prepared_targets` fails closed. A cancelled start reaps and retires its child.
- The identity is re-captured after the ACP handshake: a launcher (`env`, shebang shim) execs the agent and changes the command half of the identity. Before that refresh a mismatch reads as a reused PID and is never signalled — the orphan is missed, not a stranger killed. A target that cannot be recorded does not run.

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

## 2026-09-26 — Harness matrix is a persisted eval artifact

- The ablation command can persist native measured trials and merge validated
  external-harness trial feeds into one JSON/Markdown matrix artifact. External
  rows retain their discovered version, capability, cost, approval, and handoff
  fields; no vendor result is synthesized from a native run.
- `HarnessTrial` validates at construction and ingestion, including prompt
  hashes, non-negative measures, completion unknowns, and handoff states.
  Ablation conversion rejects any result whose task id is absent from the task
  set rather than hashing the id as a substitute prompt.

## 2026-09-26 — Runtime observability is persistent and least-privilege

- ACP adapters always own a `RuntimeMetrics` recorder; session-aware CLI, SDK,
  and handoff construction passes the session store and persistence directory so
  metrics survive adapter close and are available to support reporting.
- Support bundles expose only an explicit metadata allowlist plus lane/tally
  structure and metrics. Raw task prompts, workspace paths, transcripts, and
  arbitrary session fields are excluded; every public string and event-tally
  key is recursively scrubbed.
- Runtime metrics use the statistical median for even samples, while native
  turn accounting remains on its existing schema and path.

## 2026-09-26 — Direct model transports require admission at construction

- Direct transports are registered only with a canonical, fully-admitted
  `TransportRecord`; factory builds re-check that record so test-only catalog
  coverage cannot be bypassed by a new builder.
- Admission requires structural auth kind, primary HTTPS citations, capability,
  cost, migration, test-double, and collectable integration-test artifacts.
- ACP CLI execution relies on the adapter's post-handshake store attachment;
  callers do not manually append a pre-start segment, preventing duplicate or
  authority-free runtime lanes.

## 2026-09-26 — Inbound ACP uses the public v1 transport

- Garuda's inbound stdio ACP server uses the same newline-delimited JSON-RPC
  framing and content-block prompt shape as the outbound adapter; it refuses
  missing or mismatched initialize versions before session methods.
- Prompt turns stream normalized events while the runtime is active. Permission
  requests are bidirectional JSON-RPC requests, so a client can answer an
  approval before the turn completes; cancellation remains concurrently
  dispatchable.
- EOF cleanup cancels pending turns and closes sessions/writers under a bound.
  Native server sessions retain and close the MCP manager returned by setup so
  editor disconnects do not leak subprocess resources.
## 2026-09-26 — P1 model role bindings (issue #79, part of #74)

- `prepare_agent_run` is the single binding-resolution point returning `PreparedNativeRun`; `--model` is the explicit reasoning alias and parser defaults are `None` so omission never masks profile/project/global bindings.
- SDK `Model` objects are kept by identity per run; no API keys in specs; project/profile collection overlays narrow key-wise against the global ceiling; the server builds fresh clients per request.

## 2026-09-26 — P1 initial runtime selection (issue #77, part of #74)

- Initial selection is its own layer (`garuda/runtime/selection/`, `Initial*`/`Selection*` names) beside the P2 policy router (`router.py`, `Routing*` names); neither imports the other.
- Task regexes are global-trust-only; project routes auto-select only when global config sets `trust_project_routes`, otherwise recorded as recommendations.
- Trait detection is bounded direct-filesystem inspection (no shell, no project import, no symlink following); startup fallback requires an unchanged baseline and fails closed outside a repository.
- The CLI constructs one trusted catalog for initial selection and execution; the selected id controls the native/ACP executor, and its rationale is persisted after lease/session creation but before runtime start. An ACP start failure can transfer only once to native, only with an unchanged baseline, and retains the same session trail.

## 2026-09-28 — P1 initial-runtime classifier fallback (issue #80, part of #74)

- The classifier lives in `garuda/runtime/selection/classifier.py` and runs only through `select_initial_async` after explicit, profile, and rule sources produce no selection. The synchronous `select_initial` never calls a model.
- Only trusted global `routing.classifier` enables it. It picks one of the two approved bindings (`collection` by default, `reasoning` only when named or explicitly allowed) and cannot name a model, provider, endpoint, command, or tool. A project may only disable it.
- One tool-free call over an approved candidate table fixed before the call, with a single transport attempt (`ModelSpec.max_attempts=1`), a timeout, and an output budget. Thinking and reasoning settings are dropped so the budget holds, and a missing binding skips classification instead of using the built-in model. The answer is untrusted: its runtime must be in the approved table and revalidated, its claimed capabilities only narrow, and a confidence threshold applies. Every refusal uses the configured default; the classifier cannot start a runtime or request a handoff.
- The persisted record (selection schema v2) stores binding role, `call_purpose: classifier`, model identity, request digest (not the prompt), sanitized output (model-supplied ids recorded only when they name a configured runtime or capability; unexpected keys counted, never echoed), latency, usage, and cost. Unpriced cost is `null`, never zero, and any unexpected reply shape is `malformed` rather than a selection failure.

## 2026-09-28 — P1 collection tool effects and authority (issue #78, part of #74)

- Tool effects fail closed to `UNKNOWN`. Collection trusts built-in declarations
  by registry object identity, not by name or a project tool's self-declared
  metadata.
- MCP effects are authoritative only when declared for an exact remote tool in
  the user-owned global `mcp.json`; project MCP configuration cannot grant an
  effect. Lazy `use_tool` dispatch re-checks the selected tool's effect and the
  ordinary permission engine.
- Collection tools are the ordered intersection of profile, global, request,
  network, permission, and trusted-effect selectors. Optional shell inspection
  uses a wrapper around the real built-in `bash` and the existing fail-closed
  side-effect classifier. This policy is a guardrail, not workspace confinement.

## 2026-09-28 — P1 structured collection execution (issue #76, part of #74)

- Collection is an explicit parent tool call, never an automatic per-turn model
  swap. The public tool exists only when both a collection model and enabled
  trusted policy resolve; single-model tool schemas remain unchanged.
- Every job owns a separate collection-model `ContextManager`, terminal
  `submit_collection` contract, attempt/session identity, and sibling JSONL
  trace. Parent and child logs join by job, attempt, and child session IDs.
- Handoffs are only `none` or bounded `brief`; full parent transcripts are
  rejected. Reports must be structured and evidence-backed, and file, URL, and
  buffer evidence is validated against request scope before reaching the parent.
- `submit_collection` can end only the child run. All mutation and normal
  `task_complete` authority stays with the reasoning controller, and ordinary
  `invoke_subagent` continues to use the reasoning binding.

## 2026-10-02 — Subagent delegation ceiling (issue #142)

- A subagent's permission engine is a `DelegatedPermissionEngine`: each call is
  decided by the child profile and by the parent's *effective* engine, and the
  strictest of DENY > ASK > ALLOW wins. Allow-prefix rules are never merged, and
  an ASK goes once to the parent's installed handler. Nesting composes, so the
  root ceiling (CLI `--permission-mode`, dashboard `--max-permission`, profile
  rules) holds at every depth.
- A child selects its tools from the parent's admitted toolkit and never opens
  its own MCP connections; a requested tool the parent lacks is dropped with a
  warning. A subagent profile's `mcp_config_path`/`mcp_servers` are ignored.
- The nominal mode order is used only to report the delegated posture, never to
  authorize a call.

## 2026-10-02 — Project authority: profile ceiling and MCP trust (issue #143)

- A profile whose file is inside the workspace is project content. Its
  `permission_mode` may not exceed the user-only `agents.project_ceiling`
  (global settings, default `smart`); a widening refuses with
  `agent.project_widening` before any model, MCP server or project code starts.
  An explicit caller permission mode is user authority. Location decides, so
  `--agents-dir` pointing into the repository does not change the authority.
- A project MCP entry (`.agent/`, `.garuda/` or `.cursor/` config, or a project
  profile's `mcp_config_path`) starts or connects only with a user trust grant
  bound to the repository path, server name, a digest of the raw entry (before
  `${VAR}` interpolation) and the bytes/targets of repository files its command
  line names. Untrusted entries are filtered before name merging, so they never
  shadow a user server; a profile that requires one refuses.
- Grants are made by `garuda mcp trust` (interactive, or `--yes`) and kept in an
  owner-only, locked, atomically written store beside the global settings that
  never follows symlinks; a damaged store grants nothing. There is no global
  "trust all project MCP" switch.

## 2026-10-02 — Guard hooks fail closed; rewritten calls are re-authorized (issue #144)

- A `before_tool` hook is a guard. Exit 0 allows, exit 2 blocks, and every other
  outcome — another exit code, a start failure, a timeout, an exception in a
  programmatic guard, a malformed return, or the run's deadline passing — blocks
  the call with `hook.failed_blocked`. Only a hook in the user's global settings
  (`on_failure: allow`) or a programmatic hook registered with
  `on_failure="allow"` may be advisory; a project's hooks always fail closed.
- Hooks run in their own process group with a per-hook timeout of at most 300s,
  bounded by the run's deadline; timeout and cancellation kill the whole group.
- Permissions are checked before hooks run. When a hook changes the call's tool
  or arguments (including in place), the final call must name a tool the run
  has and gets its own permission decision; approval of the original arguments
  never covers rewritten ones. An unchanged call is not asked about twice.

## 2026-10-02 — Transport admission splits runtime and repository checks (issue #145)

- Import-time and build-time admission (`assert_admissible`) validates only the
  shipped `TransportRecord`, including that its integration test is a
  well-formed relative nodeid. It reads nothing outside the package, so a
  non-editable install imports without the repository's `tests/`.
- Whether that integration test exists and defines the named function is a
  repository contract (`assert_repository_admission`), enforced by the
  transport tests in CI. A `wheel install` CI job builds the wheel, installs it
  outside the checkout and runs `garuda --help`.

## 2026-10-02 — One profile parser; ambiguous security values refuse (issue #146)

- YAML and `agent.md` profiles go through one parser driven by the
  `AgentProfile` fields, so no field can be readable in one format and dropped
  in the other. The `agent.md` body remains the system prompt.
- Unknown keys warn (with a suggestion) and unknown tool names warn after the
  toolkit resolves, each naming the profile file. Duplicate keys, an unknown
  `permission_mode`, and malformed `tool_rules`/`path_rules`/`bash_rules`
  refuse the profile instead of being normalized.

## 2026-10-02 — Explicit `--runtime native` pins the native runtime (issue #147)

- `garuda run --runtime` now defaults to "not given". Naming any runtime,
  including `native`, is an explicit selection: the policy router, trusted and
  project rules and the classifier are not consulted. Omitting the flag keeps
  the previous precedence (router, rules, classifier, `default_runtime`,
  native). This supersedes the earlier "`--runtime native` counts as no choice".
