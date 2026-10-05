# Durable decisions

## 2026-10-05 — Parent death cannot reclaim ordinary capacity (#216)

- Ordinary reservations can authorize dispatch but version 2 records contain
  no descendant cleanup receipt. Both ordinary callers and queue selection
  retain those reservations after owner death instead of deleting them during
  admission. A matching dead/unknown owner record also cannot grant an ordinary
  activation retry. Parent liveness, TTL and an apparently empty process group
  do not prove that a runtime in another group has stopped.
- Explicit matched release remains a trusted coordinator cleanup assertion.
  This change adds no automatic recovery or fabricated reap evidence; a proved
  recovery/cleanup receipt protocol remains session-kernel and D.2 work.
  Refused admissions preserve capacity bytes and holder identity. Tests use a
  real parent that exits while its separately grouped child continues writing.

## 2026-10-05 — Revoked queue adoption cannot grant dispatch (#216)

- Direct dispatch requires the complete committed process-local ticket as well
  as the claiming instance's retained owner. A release attempt revokes adoption
  before publishing intent; failed publication cannot resurrect activation from
  an old claimant handle, including a release made through another store instance
  or a workspace refusal.
- Ordinary capacity activation rechecks the complete adopted ticket under its
  file lock and again after activation publication, before returning launch
  authority. A ticket captured before release cannot grant authority after
  revocation, including revocation while publication is in flight. A published
  activation with revoked authority retains its protected slot; ambiguity is
  quarantined instead of replayed. The adoption mutex is never held over I/O.

## 2026-10-05 — Background admissions resolve canonical runtime capacity (#216)

- Background launch resolves an explicit runtime reference through the existing
  shared trusted runtime catalog before creating a session, queue record or
  worker. The queue uses the canonical runtime id and its user/harness lane;
  launch arguments retain the original reference for downstream capability
  resolution instead of being rewritten to the canonical id. This resolution runs no discovery probes.
- Before worker identity publication or selection, the worker checks that its
  current reference resolves to the admitted lane, its session matches, and
  serialized launch arguments and the launch receipt match the queued digest.
  Retargeted aliases and changed argument/receipt records refuse without
  rewriting the admission or reserving capacity. Removed admissions start no
  work. This receipt covers serialized arguments; freezing every resolved
  configuration file and proving descendant cleanup remain separate work.

## 2026-10-05 — Dispatch-ready queue bindings name their FIFO scope (#216)

- A fully bound queued session must use the scope formed from its declared user
  and harness. A different label cannot create a second FIFO lane for the same
  user/harness pair. Enqueue checks before storage initialization; ticket creation
  checks again before selection, so preexisting mismatched waiting records cannot
  bypass the admission check. Refusal preserves the source and does not reserve
  capacity or infer replacement bindings.
- Incomplete low-level allocations remain nonactivatable; this check does not
  invent missing session/configuration evidence. The default user remains the
  local uid, and `scope_for()` constructs its corresponding scope. Synthetic
  user scopes must supply their matching user explicitly.

## 2026-10-05 — Journal queue selection separately from activation (#216)

- Queue version 3 records intent before protected shared-capacity reservation
  (capacity version 2), publishes the claim and commits selection before granting
  process-local activation authority. These are ordered durable publications
  across separate files, not one atomic transaction. Release removes the claim
  durably before returning its slot. A durable capacity release fence blocks
  launch threads that captured adoption before invalidation and retains prior
  activation history; ordinary capacity callers cannot release
  or reclaim queue slots, even for confirmed-dead owners.
- Only the successful claiming process with real birth/group identity and full
  frozen bindings may activate. Background workers persist activation before
  invoking the runner, including unlimited harnesses. Recovery never launches:
  dead pre-activation transactions reconcile; activated or ambiguous dispatch
  stays quarantined because worker death alone does not prove descendant cleanup.
  Full launch supervision and cleanup receipts remain D.2 lifecycle work.
- Workspace-refused claims return in original durable sequence, regardless of
  callback completion order. Pending transactions remain visible in pure reads
  and cannot be rebound before recovery. Adoption invalidation matches the full
  ticket under a process-local lock; finishing an old release cannot clear a
  replacement queue's activation authority. Forks clear inherited adoption.
- Empty legacy queues and fully bound, ordered version 2 waiters preserve exact
  private `.v1`/`.v2` archives before upgrade. Version 1 work and version 2 claims
  refuse missing binding/activation evidence. Nonempty capacity version 1 records
  refuse mutation without reservation origin evidence; empty ones archive before
  upgrade. Ambiguous backups remain intact. All publications use owned locked
  directory descriptors; process-death tests do not prove hardware power-loss behavior.

## 2026-10-04 — Legacy queues cannot invent admission bindings (#216)

- Version 1 work stays readable for diagnosis but every mutation refuses: it
  lacks user/session/configuration binding evidence, even for dead owners.
- Only a fully validated empty legacy queue migrates. Its exact source bytes
  are backed up and file/directory-fsynced before v2 publication through the
  locked directory descriptor. Existing backups must be private regular files
  matching the source; partial, unrelated, symlinked and nonregular artifacts
  refuse and remain available. Legacy capacity never widens a shared ceiling.
- Process-interruption tests cover flush/publication boundaries and exact
  retry. They prove process recovery, not hardware power-loss behavior. The
  queue/capacity crash journal remains a separate requirement in issue #216.

## 2026-10-04 — Queue inspection does not initialize or migrate storage (#216)

- Queue construction, `entries()` and `snapshot()` are pure reads. Missing
  roots stay missing; existing permissions, document bytes and inventory stay
  unchanged. Both APIs read the atomic document without taking a writer lock.
- Historical documents are projected in memory for diagnosis. Mutations keep
  their locked private-storage initialization and migration behavior; safe
  legacy migration and the queue/capacity crash journal remain open work.

## 2026-10-04 — Queue mutation requires the complete claimant identity (#216)

- Heartbeat, release and workspace requeue match pid, process start identity,
  process group and epoch against the persisted claim. An implicit mutation
  uses only the instance's retained successful claimant in the same process;
  a new instance or fork cannot derive authority from a shared record.
- Workspace refusal/exception returns only the original owner's slot. If
  another owner replaced the claim, its queue and capacity records stay intact.
- The queue/capacity crash journal, legacy migration and inspection gates in
  #216 remain outstanding.

## 2026-10-04 — Queue enqueue retries preserve the admitted binding (#216)

- Within one queue store, an item id binds one scope, user, harness, session and
  frozen configuration digest, whether waiting or claimed. Exact retries of
  current-version records are read-only and preserve FIFO sequence; conflicts
  refuse under the queue lock. Already duplicated records refuse retries and remain available
  for diagnosis. Cancellation and release retain their existing behavior.
- This repair does not satisfy the outstanding queue/capacity crash journal,
  safe legacy migration, ownership or inspection gates in #216.

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

## 2026-10-02 — Initial selection probes only selectable runtimes (issue #148)

- `garuda run` no longer runs every ACP manifest's version/auth probes. An
  explicit runtime or profile pin probes only that runtime; otherwise only
  rule targets (project rules when trusted), classifier candidates when the
  classifier may run, and the default/fallback runtimes are probed. A plain
  native run probes nothing.
- Selection reuses a probe result for 60 seconds, keyed by runtime id,
  executable path/identity and probe argv, from an owner-only cache that stores
  the derived record (availability, extracted version, auth/health, warnings),
  never raw output. Listing and inspection bypass the cache.

## 2026-10-02 — `latest` is project-scoped; sessions record absolute workspaces (issue #149)

- `--resume latest` (CLI, SDK, JSON-RPC) resolves to the newest session whose
  recorded workspace belongs to the current project. Project identity is the
  resolved path, with linked Git worktrees mapped to the main repository root
  (read from `.git`/`commondir`, never by running `git`). `--all-projects` /
  `resume_all_projects=True` keeps the old "newest anywhere" meaning; JSON-RPC
  has no global form. Sessions without an absolute workspace are never a
  project's `latest`. Teams task B.1 later replaces the path with an opaque id.
- Native sessions now record the absolute workspace instead of `"."`.

## 2026-10-02 — Queue and lock contract proved by the A.4 spike (issue #153)

- Cross-process mutual exclusion uses an exclusive `fcntl.flock` on a
  no-follow, owner-only lock file; any failure to lock refuses the mutation.
  State is one versioned JSON document replaced atomically (temp file, fsync,
  rename, directory fsync); corrupt, symlinked or future-version state refuses.
- Ownership = pid + process start identity + process group. Reclaiming an
  expired claim needs confirmed death (a reused pid counts as dead; a live
  group member keeps it alive); a live owner past its TTL keeps the claim and
  indeterminate liveness quarantines it.
- Waiting is polling with bounded exponential backoff — portable and immune to
  lost wake-ups. Capacity is claimed before the workspace and released if the
  workspace is not immediately available, so no process holds a slot while
  waiting on a workspace.
- POSIX only for now (macOS, Linux); the spike refuses other hosts.

## 2026-10-02 — Git snapshot and publication contract proved by the A.5 spike (issue #154)

- Snapshots never run repository code: Git runs with no inherited `GIT_*`,
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, and
  `core.fsmonitor=false`, `core.hooksPath=/dev/null`, signing off. Content is
  read without following symlinks, hashed with `--no-filters` and placed in a
  temporary index; the source index, stash, refs and checkout are unchanged.
  A manifest change during capture refuses. v1 refuses submodules, sparse
  checkouts, `filter=` attributes, symlinked parent directories, special files
  and oversized trees.
- Consult snapshots live in a detached repository with its own metadata, no
  alternates and no hardlinks.
- Integration publishes only `refs/garuda/integration/<id>` with an
  expected-old-value compare-and-swap and returns `git merge --ff-only`; the
  destination branch, HEAD, index and checkout are never changed. Required
  checks run in Docker with the detached source read-only, no network, no
  socket, an unprivileged user and a tmpfs scratch; a tree change voids the
  evidence.

## 2026-10-02 — Strict ownership storage and shared runtime capacity (issue #157, B.0)

- Ownership records (workspace leases, capacity slots) use one strict storage
  contract: owner-only directories, an exclusive no-follow `flock`, atomic
  fsynced writes, and refusal of corrupt, symlinked or future-version records,
  which are kept for diagnosis. Lock failure is an error, never an unlocked
  write.
- An owner is pid + start identity + process group (used only when the owner
  leads it) + epoch. An expired lease or slot is reclaimed only when its owner
  is confirmed dead; a live owner past its TTL keeps it, and indeterminate
  liveness blocks takeover. Heartbeats and releases from a superseded epoch are
  refused. Legacy leases without an identity stay readable and can only be
  proven dead.
- A capacity holder/session id is not ownership authority (#213). A repeat
  reservation is idempotent only for the exact pid/start identity/process
  group/epoch. A different owner cannot replace a live or unknown holder,
  even when capacity remains free; takeover requires confirmed death. Release
  checks the full recorded owner, not the epoch alone.
- Every launch through `WorkspaceLeaseGuard` reserves capacity for its runtime
  key (`native`, or the ACP runtime id) before taking the workspace lease, and
  gives it back if the workspace is held — never waiting while holding a slot.
  Ceilings are user-only (`capacity:` in global settings); an unset key is not
  limited until teams C.1 supplies packaged defaults.
- Journaled launch intents and supervised process-group identity before
  dispatch remain B.6 work.

## 2026-10-02 — Opaque project ids and per-project session names (issue #157, B.1)

- A session records `project_id`: `p1_` + a domain-separated HMAC-SHA256 of
  the project's canonical root (worktrees and symlinks resolve to the
  repository root; non-Git work to its real path), keyed by a random
  user-local key created atomically at `<sessions root>/.identity/key`
  (`0600`) and never exported. The path and filesystem identity are stored
  only as local metadata. Losing the key while sessions carry ids refuses
  with `session.project_key_missing` instead of generating a replacement.
- Session names are unique per project, reserved by an exclusive `mkdir`
  under `<sessions root>/.names/<project_id>/`; an explicit `--name` must be
  free, an automatic one is the task's slug with the first free suffix.
- Names, prefixes and `latest` resolve within the current project (legacy
  sessions by recorded path); `--all-projects` searches everywhere; a full id
  always resolves.

## 2026-10-02 — Journaled project-id recovery (issue #157, B.1)

- `garuda doctor --recover-project-ids` rebuilds project ids under a new key
  only when the key is missing; it refuses while any lease owner may be alive.
  A session maps to a new id only when its recorded path still has its
  recorded filesystem identity; otherwise it keeps its old id (unmapped).
  Name reservations move with their project; merges that would collide refuse
  before anything changes.
- The plan is journaled (`recovery.json`) and the new key staged (`key.next`)
  before any session changes; metadata keeps `previous_project_ids`; the key
  is published last and the journal becomes a receipt. An interrupted run
  resumes from the journal; a finished one is a no-op.

## 2026-10-02 — Session state is four independent fields (issue #157, B.2)

- Session metadata gains a versioned `state`: `process` (starting, live,
  exited, missing, unknown), `work` (queued, working, waiting, done, stopped),
  `outcome` (unset while active; completed, failed, refused, cancelled) and
  `verification` (passed, failed, unavailable, invalidated, with an authority).
  Impossible combinations are rejected.
- A native completion-gate pass is a `self_check`, not verification. Only an
  authoritative grader (`answer_check`) yields `verification: passed` with
  authority `user-config`; trusted acceptance checks follow in C.5. Results
  carry `completion_gate` provenance (verifier ran, grader authoritative).
- `crashed` is derived at read time (process exited/missing while work is
  active with no outcome); a recorded live owner that is confirmed dead reads
  as `missing`. Legacy records map through a fixed table and keep
  `legacy_status`; the raw `status`, exit codes and API results are unchanged.

## 2026-10-02 — Generated context lives in the session store (issue #157, B.3)

- `current-task.md` and `handoff.md` are published by `ContextPackManager` into
  the session's own directory in the session store — the same place
  `runtime handoff` and the dashboard already used — never into the workspace.
  Two sessions in one repository keep separate packs, and a run no longer
  writes into the tree it is changing or its own delta.
- Committed durable `.context/` files remain read-only repository input. Packs
  older runs left in a workspace's `.context/` are neither read nor deleted.

## 2026-10-02 — Every mutating entry point holds the workspace lease (issue #157, B.4)

- CLI chat, dashboard chats, the SDK `Conversation` and recipes take the same
  `WorkspaceLeaseGuard` as `garuda run`: acquired before the environment or
  the first prompt, heartbeated for the session's whole life, each turn raced
  against the heartbeat (a lost lease stops the turn), released last on close
  and on every failure path. A `readonly` posture takes a shared read-only
  lease, which never blocks an editor.
- A second editor is refused: `garuda chat` exits 1 before any environment,
  the dashboard returns `409 workspace_busy`, the SDK raises
  `LeaseConflictError`.

## 2026-10-02 — Session worktrees and integration publication (issue #157, B.5)

- `garuda run --isolation shared|worktree|auto` (default `shared` until the
  team runtime chooses). A worktree session edits a linked worktree on its own
  `garuda/<session>` branch; uncommitted source changes are not inherited and
  the session records that they existed. It is a separate place to edit, never
  described as confinement.
- `garuda sessions merge` never changes the destination branch, HEAD, index or
  checkout. It publishes only `refs/garuda/integration/<session>` by
  compare-and-swap after required checks pass in Docker against the merged
  tree; without a check or without Docker it refuses — a host check is never a
  substitute. Applying is the person's `git merge --ff-only`.
- `garuda sessions remove-worktree` refuses a worktree whose work was never
  published unless `--force`. Deferred: trusted worktree setup commands
  through the permission engine.

## 2026-10-02 — One session lifecycle for every native entry point (issue #157, B.6)

- `garuda run`, `garuda chat`, dashboard chats and the SDK `Conversation`
  follow one ordered lifecycle (`interfaces/session_service.py`): workspace,
  capacity then lease, session record and baseline, approval broker,
  environment; at the end reap background processes, tear down, persist, and
  release the lease last. Chats and the SDK now also get the session broker's
  audited approvals and background-process reaping; the SDK `Conversation`
  now records a session with a baseline and a delta.
- A session is recorded only once its workspace is leased: a refused lease
  leaves no record on any entry point.
- A background process that cannot be proven dead quarantines the session:
  the workspace lease stays held (heartbeat stopped, so it is taken over only
  once this process is confirmed dead), the capacity slot is returned, and
  the session records the pids. Fail closed rather than let a second editor in
  beside a possible stray writer.

## 2026-10-02 — Session tags carry bounded briefs, never transcripts (issue #157, B.7)

- There is no automatic cross-session index. Another session's brief reaches
  a run only when the user tags it: `--with NAME`, or an `@name` token that
  exactly matches a current-project session name. `@pytest.fixture`, unknown
  names and full ids in message text stay text.
- Another project's session needs `--with-id FULL_ID` plus the user's grant
  (`--allow-cross-project-context`, or yes at an interactive preview).
  Project configuration and model or tool output can never grant it; plain
  `--with` refuses with `session.cross_project_context_denied`.
- A brief holds only the redacted task, state, changed files, baseline
  commit, checks and the end of the final output, each bounded. It is
  escaped and line-quoted inside an envelope that labels it data; checks run
  against a different tree render stale and are never current evidence.
- Both sessions record the link; a cross-project grant writes an immutable
  receipt (ids, projects, field names, fingerprint, provenance) without the
  brief's text. ACP runs now record the end of the agent's reply as
  `final_message` so their briefs have an output.

## 2026-10-02 — Resume modes: native, proven reload, or a brief (issue #157, B.7)

- `--resume S` always continues in a new, linked session (`resumed_from`,
  `resume_mode`). A native session restores its transcript. An ACP session
  uses the agent's own `session/load` only for an adapter version on which it
  was exercised (`runtime/resume.py` `PROVEN_LOAD`, empty today: Claude Code
  and Codex only declare it); otherwise, and whenever the agent stops
  declaring `loadSession` at the handshake, it starts a new session on the
  same runtime with `S`'s brief (`resume_mode: brief`, with the reason).
- `--as RUNTIME` continues on another runtime through a brief. A model or
  role change on a native resume is recorded as `link_reason`.
- A session whose owner is still live refuses (`session.live_owner`).

## 2026-10-02 — File-backed approval channel (issue #157, B.8)

- A parked approval is also published to `<session dir>/approvals/` so
  another process can answer it (`garuda approvals answer`). The broker stays
  the only place a decision is made; the terminal, the dashboard and a file
  answer race to it and the first counts, recorded once by exclusive creation.
- A file answer counts only if it is a regular owner-only file (not a
  symlink) bound to the session, approval id, request digest and nonce,
  arrives before expiry, and the permission ceiling is unchanged. Anything
  else is a denial with its reason. Answers appear by exclusive `link`, so a
  partial file is never visible.
- A decision's delivery to an ACP runtime is reserved before and
  acknowledged after. A reservation without acknowledgement (a crash) is
  never resent: nothing proves the runtime's acknowledgement idempotent.

## 2026-10-02 — An additive garuda.yaml with source-derived authority (issue #158, C.1)

- Roles, harnesses, flows, checks and consult limits live in a new, optional
  `garuda.yaml` (user file beside the settings; project file at the
  repository root). `settings.yaml` and every legacy resolver are unchanged;
  `garuda config migrate` only adds.
- Authority comes from the file a value was read from (`user-config`,
  `trusted-project`, `user-request`); an `authority` key is refused anywhere.
- Layering: package < user < project < CLI; whole-definition replacement for
  named roles and flows; ceilings intersect; checks accumulate; harnesses,
  `sessions.keep_days`, fallback chains and consult grants are user-only and a
  project may only narrow them.
- In C.1 roles are validated but not yet applied to a run: a project-chosen
  model string must wait for hash-bound project trust (C.2) and exact
  resolution (C.3).

## 2026-10-02 — Project garuda.yaml trust is hash-bound and interactive (issue #158, C.2)

- A project file's commands (`checks`) and native model strings run only
  when its exact bytes are trusted for that repository and schema version,
  in the #143 content-bound trust store (same store, same semantics). Any
  byte change, another repository or a symlinked file means no trust.
- Untrusted values are withheld, not fatal; everything that can only narrow
  still applies, and the run reports `config.project_untrusted`.
- The parsed values that run come from the same bytes that were hashed, so a
  file swapped after the check cannot run as trusted. Headless runs cannot
  create trust. A project-added role is capped at `agents.project_ceiling`.

## 2026-10-02 — Roles resolve to exact ids; ACP options only where proven (issue #158, C.3)

- A role resolves to one plan: runtime id, exact model id, effort,
  permission ceiling, profile, a config digest and provenance, recorded on
  the session. No friendly matching outside interactive setup.
- Native roles act through the existing flags; an explicit permission flag
  and the role's ceiling intersect.
- ACP model and effort are set with `session/set_config_option` before the
  first prompt, only for an adapter identity (runtime id + discovered
  version) proven by the A.3 captures, and only to a value the agent offered
  in `session/new`; the reply must show it applied. Otherwise the run refuses
  before any prompt (`role.options_unproven`, `role.model_unavailable`).

## 2026-10-02 — Onboarding and one diagnostics registry (issue #158, C.4)

- `garuda/diagnostics.py` holds one `Diagnostic(code, message, fix)` and the
  registry of stable codes; every code has a fix template and tests assert
  codes, never message text.
- `garuda doctor` probes only harnesses a role or `--runtime` names; a login
  check keeps "logged out" (the documented answer) apart from a failed,
  timed-out or unrecognized check, and caches only its conclusion for 60 s.
- `garuda init` writes only after confirmation (a yes in a terminal or
  `--yes`); `init --project` needs a terminal because it creates trust, and
  writes the project file and its trust record together. Proposals open no
  file and run nothing.

## 2026-10-02 — Verification comes only from acceptance checks (issue #158, C.5)

- Acceptance checks carry the authority of their source: `user-config`,
  `trusted-project` (exact bytes trusted) or `user-request` (`--check`). Only
  they set verification `passed`/`failed`; native gate and ACP commands are
  `agent-suggested` and only ever set the self-check. No acceptance check
  means `unavailable` with `verification.no_trusted_check`; an authoritative
  grader's verdict stands when there are none.
- Every check leaves a receipt with its authority and the code fingerprint it
  ran against. A check that changes the tree is void
  (`verification.check_changed_tree`). Test-infrastructure edits are judged
  per check: a pass is withheld only when the session changed the
  infrastructure that check's tool depends on
  (`verification.test_infra_changed`), never repository-wide.
- Outcome, verification and self-check stay independent; the legacy `status`
  is unchanged.

## 2026-10-02 — no-edits is a refusal plus an after-the-fact check (issue #158, C.10)

- `write_policy: no-edits` (role or `--no-edits`) refuses edits and commands
  — native `readonly`; ACP approvals denied and audited — and is never
  described as confinement.
- After every descendant exits, a bounded no-follow manifest is compared with
  the one taken before (ignored files, modes, symlinks, change times; git
  refs, HEAD, hooks, config and staged entries by content). Any change or an
  incomplete comparison withholds the outputs, stops the run (exit 3) and
  records the evidence; nothing is reverted. Only a complete, identical
  comparison says "no changes detected".
- A parallel group cannot be no-edits, and a no-edits run is checked in
  place (no `--isolation`).

## 2026-10-02 — Flows hold one lease and never replay a step (issue #158, C.6a)

- A flow's parent session holds the workspace lease for the whole flow and
  lends each step a capability it issues and revokes; a step cannot take,
  renew or keep the lease, and nothing outside the flow can take the
  workspace between steps.
- Intent is journaled before every launch and an immutable receipt written
  after. A launched step without a receipt is quarantined and never replayed;
  a resume continues only past receipted steps, each retry its own attempt
  and session.
- Artifacts come only from the bounded `<garuda-artifact type=…>` envelope;
  Garuda chooses where they live, and every input is checked for digest,
  file type, containment and workspace version before a step runs.

## 2026-10-02 — Reviews retry only the reviewed step and are never verification (issue #158, C.7)

- `review: {by: R, max_rounds: N}` names the flow's terminal reviewer. The
  review block is bounded and strict; invalid output stops the flow; a
  blocker or major finding requests changes whatever the verdict.
- Only the reviewed step reruns, with the findings as data; `max_rounds: 2`
  is at most three pairs. Workspace-bound artifacts (`patch`, `review`,
  `findings`) are regenerated; task-bound ones (`plan`, `notes`, `summary`)
  carry over.
- Independence (default on) compares actual runtime and model identities,
  including fallbacks and consults. The flow ends `review_approved` or
  `review_changes_requested`; verification is untouched.

## 2026-10-02 — Packaged flows run without Docker (issue #158, C.6b)

- `plan-only`, `pair` and `plan-build-review` ship as package data; their
  scout, planner and reviewer steps are `no-edits`, so they run with Claude
  Code and Codex under the no-edits guardrail rather than Docker.
- A configured flow of the same name replaces a packaged one; a flow whose
  roles are missing refuses with `flow.missing_roles`. Artifacts carry a
  version and an unknown one refuses (`flow.input_version`).

## 2026-10-02 — Read-only ACP roles need proven Docker confinement (issue #158, C.8a)

- An external ACP role with `permissions: readonly` (standalone, flow step
  or fallback) runs only in Docker after a preflight proves: read-only source
  and `.git`, writable bounded scratch, an unprivileged capability-free user,
  and no Docker socket, home, credential store or other workspace mounted.
- The in-container runtime is the user's own image and login; Garuda passes
  no host credential. The image is user-only configuration.
- Anything unproven refuses with `workspace.readonly_unenforced`. There is
  no host, worktree or guardrail substitution.

## 2026-10-02 — Parallel reviewers share one immutable snapshot (issue #158, C.8b)

- A parallel review group runs on one detached snapshot repository of the
  workspace. Native members are `readonly`; external members must be
  `readonly` roles in proven Docker confinement (C.8a) — the same refusal
  contract as standalone and sequential reviewers, checked before any
  member launches.
- Source and snapshot are compared after the group; any change stops the
  flow. Read-only runs take shared read-only leases so members never contend.

## 2026-10-02 — Role fallbacks happen once, before start, for proven reasons (issue #158, C.9)

- A role's fallback chain is walked once before the runtime starts. A
  candidate is skipped only when its CLI is missing or its documented login
  check, run fresh, says logged out; an unknown, failed or timed-out check or
  an unknown limit keeps it. `harness.limit_reached` waits for E.2's proof.
- Trust, configuration and confinement errors refuse; nothing falls back
  after a prompt. The session (and a flow step's receipt) records the
  primary, the entry taken and every skip reason before the prompt.

## 2026-10-02 — One versioned agent definition and one resolver (issue #159, H.1)

- Every agent loads through `agents/resolve.py`: project, then user
  (`<global home>/agents/`, new), then packaged; `garuda/<name>` is always
  packaged. Legacy profiles translate mechanically and resolve to exactly the
  same `AgentProfile` (golden-tested for every packaged profile).
- Version 1 is strict (unknown fields, tools, skills, MCP servers, missing
  instruction files, duplicate keys, cycles and future versions refuse) and a
  recognized field whose owner PR has not shipped refuses activation instead
  of being ignored.
- `extends` merges structurally first; authority is applied afterwards from
  provenance, so a project definition is held to the project ceiling even
  for values it inherits.

## 2026-10-02 — Every agent field has an owner and an observable effect (issue #160, H.12a)

- `context.condenser`, `limits.deadline_sec`, `completion.verifier` and
  `workspace.docker.network/memory/cpus` are now supported, each mapped to the
  `AgentConfig` or environment value that enforces it, with a test observing
  the effect.
- Budgets are checked before activation (`agent.invalid_budget` with the
  field path). A project or narrower delegate cannot switch off the verifier,
  nor the acceptance contract where the eval or rigorous posture requires
  it (`agent.required_gate`). Docker limits in a
  definition only narrow the operator's grant; legacy workspace kinds are
  never translated to `local`.

## 2026-10-02 — Agent inspection is pure and shows sources (issue #161, H.2)

- `garuda agent list/show/prompt/check/new` read definitions and
  configuration only; nothing is imported, connected, run or probed, and MCP
  tools a live server would list are reported `unknown`.
- `show` uses the same `static_agent_config` as `prepare_agent_run`, so it
  reports exactly the run's configuration; every value names its source.
- `prompt` reports the static first-request prompt and its digest. Runs
  record the actual outbound system-message digest (`system_prompt` events)
  whenever it changes; runtime blocks make the two differ, by design.
- Secrets are redacted unless `--raw`, which prints locally and is never
  persisted.

## 2026-10-02 — The system prompt is a plan of labelled sections (issue #162, H.4)

- Order: agent instructions, user memory, skills, project memory, (notes),
  context pack; runtime blocks are added by the run and show in its actual
  digest, not in the plan.
- Per-file cap with `memory.truncated`; total cap (default 32,000 characters)
  and the model's token budget after reserve and margin. Optional memory
  trims context pack first, then project memory, at paragraph boundaries;
  instructions and skills are never cut, and a version 1 definition whose
  mandatory sections do not fit refuses.
- Legacy profiles keep exactly their old sources (no user memory), so a
  zero-config prompt is byte-identical; `agent migrate` writes
  `memory: {user: false}` to preserve that. Version 1 loads `~/.agent/AGENTS.md`
  by default when it exists.
- Memory files stay inside the workspace: a link may point within it (for
  example AGENTS.md -> CLAUDE.md); an escape refuses for version 1 and is
  skipped with a diagnostic for legacy profiles.

## 2026-10-02 — Skills have sources, precedence and filters (issue #163, H.5)

- Sources: project, user (`<global home>/skills`, new), packaged; the nearer
  source wins a name collision and the others are recorded as shadowed.
  `from`, `include`/`exclude` and `load: index|full` select per agent; an
  unknown `include` refuses for version 1 and warns for legacy profiles,
  which keep exactly their old project-only sources.
- `allowed-tools` stays advisory (prompt wording kept, enforcement
  deferred); `agent check` reports `skill.tool_not_granted`. Skill files are
  read bounded and no-follow inside their source.

## 2026-10-02 — Tool presets, options and bounded delegation (issue #164, H.6)

- Presets are derived from each tool's declared effect, never a hand-kept
  list. Options are declared by the tool (`options_schema`), so an unknown
  tool or option refuses. `allowed_domains` is documented as a guardrail on
  the request (redirects included), not network confinement.
- Removals are part of the final toolkit: they drop explicit SDK tools of the
  same name and are refused behind `use_tool`.
- Version 1 agents may start only read-only subagents by default; legacy
  profiles keep "any" (migration pins it). One delegation budget (depth 2,
  8 launches) is shared by the whole tree so a child cannot reset it; a
  child never outruns its parent's turns or deadline.

## 2026-10-02 — Agents may declare a final-output JSON Schema (issue #165, H.12b)

- `jsonschema` (`Draft202012Validator`, no format checker) is a declared
  dependency, pinned in `constraints.txt`. The schema is a bounded local file
  checked at resolution: allowlisted keywords only, same-file `$ref` only, no
  cycles, bounded size, depth and expansion. `pattern` and `format` refuse
  (a file-supplied regular expression would run on model output; no format
  checker is run), so nothing is silently ignored.
- Validation happens first in the completion gate; two repair turns come out
  of the run's own turn and deadline budget, then `agent.output_invalid`
  ends the run without output. Valid output earns only the ordinary gates.

## 2026-10-02 — One agent definition from every entry point (issue #166, H.8)

- `AgentSpec` is the pure-resolution result (no model, MCP, hook or project
  code); `prepare_agent_run` takes a name, spec, mapping or path and activates.
  `--agent-file` is a source selection, not trust: location still decides
  authority and the project ceiling.
- `narrow` is an allowlist of tightenings (lower numbers, fewer tools/servers/
  subagents/domains, more deny/ask rules, checks on); anything else refuses.
- Remote callers (`serve`, web) select by name only; inline definitions refuse.
  The operator allowlist and a permission ceiling (default: the server's own
  agent) cap every named selection, including permissive packaged agents.
- Sessions record the definition digest; a resume with a changed definition
  starts a new identified segment rather than altering the earlier session.

## 2026-10-02 — Agents propose notes; only a person at a terminal accepts (issue #171, H.9)

- `memory.notes: propose` gives `remember(text, scope)`, which only records a
  proposal in the session store (`.memory/<project>/proposals`). The target
  file comes from the owner's binding, never from model-supplied paths.
  Acceptance is a locked, journaled, digest-bound, idempotent append (marker
  per proposal) and refuses symlinks, foreign projects and stale proposals.
- Secret-shaped text is rejected, and `remember` arguments are scrubbed before
  the event log or transcript see them. Accepted notes load as labelled data
  (user notes after user memory, project notes after project memory).
- Fail closed: a missing safeguard makes `notes: propose` unavailable.

## 2026-10-02 — The durable queue has no capacity of its own (issue #167, D.1)

- `runtime/queue.py` promotes the A.4 spike: a versioned (v2; v1 read and
  migrated), owner-only, locked document. Entries and claims bind user,
  harness, session, configuration digest and worker identity; FIFO per
  `<user>:<harness>`.
- Capacity is the B.0 `CapacityStore` under `harnesses.<id>.max_parallel` /
  settings `capacity`, so every launch kind shares one ceiling. Takeover is
  by confirmed death of the owner (process identity), never by TTL or clock;
  unknown liveness is quarantined. Inspection never locks or writes.

## 2026-10-02 — Background sessions are a queue entry plus a detached worker (issue #167, D.2)

- `run --bg` creates the session and queue entry and re-executes a hidden
  worker; the worker waits without a workspace lease, then runs the ordinary
  `run_task` path with a preassigned session id. Claim, heartbeat and release
  are bound to the worker's epoch; every exit path releases the slot, and a
  killed worker's slot is reclaimed on proof of death (derived `crashed`).
- Cancellation verifies the recorded process identity before signalling; a
  recycled pid is never signalled. Logs are bounded.

## 2026-10-02 — One read model, resumable by byte offset (issue #167, D.3)

- `core/read_model.py` is the only place session rows are built; the CLI
  (`sessions --json`) and the dashboard (`/api/sessions*`) render it. Reads
  only; unreadable records read `unknown`; a review outcome is never
  verification; accounting stays `unknown` until Set E.
- Live updates are server-sent events whose ids are byte offsets into the
  event log (after snapshots; polling remains), so `Last-Event-ID` resumes with
  no duplicate or skipped event; a torn last line is held back.

## 2026-10-02 — The approval inbox answers through the broker (issue #167, D.5)

- The dashboard inbox writes a session-bound answer file (B.8); the broker is
  still the only decider. The answer must bind the request digest the page was
  shown (stale pages refuse), is exclusive (one winner against terminal and
  other tabs), is refused after expiry, and the broker re-checks the ceiling.
  A 200 means "recorded for the broker". Token, Host and Origin gates apply.

## 2026-10-02 — The usage ledger counts identities, once (issue #168, E.1)

- One record per native model call, keyed `native:<session>:<event index>`; the
  writer refuses a repeated key under the lock, any field outside the per-kind
  schema, and path-like strings. Auxiliary calls are tagged through
  `model/accounting.py` so the ledger and `aggregate_model_metrics` agree.
- ACP usage reports are snapshots unless a policy for the exact adapter version
  is proved (the shipped table is empty). Cumulative sources keep a persisted
  high-water mark; replay, out-of-order, gap, decrease and adapter change never
  produce a delta. Records are appended before the cursor moves.
- Unknown cost is `null`; ledger totals exclude snapshots and say so.

## 2026-10-02 — Limit observations bind to an account or stay history (issue #168, E.2)

- A limit reading comes only from a source proved for the exact version
  (today: Codex app-server read-only methods); it records a local salted
  digest of the official account id, never a name. Exhaustion is fallback
  evidence only when fresh (<= 60 s), same account and version, reached, with
  an explicit future reset; every other case leaves the harness eligible.
  Enabling `harness.limit_reached` in C.9 is a separate change.

## 2026-10-02 — `harness.limit_reached` is enabled behind the proved-source check (issue #168, E.2)

- A role's fallback chain skips a candidate as `harness.limit_reached` only
  when a refresh made at selection time (`LimitStore.assess`) shows a fresh,
  same-account exhaustion with a future reset, for a harness whose limit source
  is proved for that exact version. Failure to refresh, or any doubt, keeps the
  candidate. Still decided once, before start; never after a prompt.

## 2026-10-02 — The conversation view reports, it does not attribute (issue #169, F.1)

- Models used come from ledger records grouped by work type × harness × model; an
  unreported model reads `not reported` and is never replaced by the selected one;
  snapshots are display evidence only and ACP turns are a separate measure. A
  session with no ledger records falls back to its metrics, labelled as such.

## 2026-10-02 — Consults are read-only questions in a snapshot, dispatched at most once (issue #170, G.2)

- A consult authorizes (user-file grants, no nesting), reserves a durable
  request id bound to the payload digest, reserves capacity without waiting,
  quiesces the asker, and runs the target read-only in an independent snapshot
  repository. The child must be reaped before the slot is released; if that
  cannot be confirmed the slot is quarantined.
- Native-tool quiescence is sequential tool execution plus a refusal when the
  run has background processes; the snapshot's stability check rejects a tree
  that changes during capture. External targets run only in Docker read-only
  confinement; otherwise `consult.isolation_unavailable`.
- Answers are untrusted advice in a labelled envelope, validated with
  `NoEditsGuard`; receipts are text-free. Dispatch is at most once: after the
  point of dispatch a failure is recorded, never retried or refunded.

## 2026-10-02 — ACP consult initiation stays closed until an adapter proves three gates (issue #170, G.3)

- The `consult` MCP tool is offered to an ACP asker only for an exact adapter
  identity (package and version reported at `initialize`) whose capture records
  forwarding, structured permission provenance and a quiescence handshake all as
  `supported`, and for which Garuda has implemented the handshake and the
  permission-identity reader. The shipped table has no such row: both captured
  adapters forward the server but prove neither other gate, so nothing changes
  for them.
- The endpoint is a local-process guardrail (a `0700` directory, a `0600`
  socket, a per-epoch 256-bit token, the asker's process identity), not a defence
  against a hostile same-user process. The token is registered with the redactor
  before use and is never persisted, logged or passed to the consulted child;
  resume rotates it. The session is fixed by the broker; a request cannot choose
  another.
- Permissions are auto-allowed only from the structured server and tool identity
  for the live session; display titles are never trusted.

## 2026-10-02 — Consult usage rolls up once and a missing receipt reads unknown (issue #170, G.4)

- A parent's views add its consulted children's ledger records once (de-duplicated by
  record key), keeping `origin` beside the original `call_purpose`; global
  statistics sum original records, never parent rollups. By-origin groups partition
  the records and add up to the total.
- A consult row with no receipt, or a receipt without comparison evidence, shows its
  changes as `unknown`, never `unchanged`; the question and answer are not stored and
  not shown.
- A review records the independence decision with the identities that actually ran
  (launched plan after fallback, roles really consulted) beside the configured ones;
  under the default policy a shared actual identity stops the flow
  (`flow.review_not_independent`).

## 2026-10-02 — A role's agent is projected for the harness that runs, never expanded (issue #172, H.10)

- `roles.<name>.agent` (alias of `profile`) is bound after fallback selection. Native roles
  carry the definition's digest in their identity. ACP roles get a projection of
  `model.effort`, `permissions.mode` (stricter of role and agent) and appended
  instructions (a labelled context block, not a system prompt); any other explicit or
  inherited request refuses `agent.field_unsupported`, a differing effort refuses
  `config.conflict`, and undeclared native defaults are not expanded. `tools.mcp` on ACP
  roles stays refused until forwarding is separately enabled.
- A consulted native role is its definition narrowed to the consult profile (readonly,
  consult tools only, no MCP or subagents, hooks and project tools absent). A definition
  that requires a schema or completion check refuses consult admission instead of being
  dropped or enabled. The child resolves the agent against the source workspace; the
  narrowing, not the snapshot path, bounds its authority.

## 2026-10-02 — The dashboard shows agent identities, never agent text (issue #173, H.11)

- Setup lists every agent definition with its source, declared settings and their
  provenance, definition digest, static prompt digest and section sizes; a session shows
  its agent and the system-prompt digests it actually sent. Neither returns instruction,
  prompt or memory text, values are redacted, and the view is read-only. A definition that
  cannot resolve is listed with its problem.
- A qualified agent name (`garuda/build`, `project/x`) is a name, not a path, in
  `garuda agent show|prompt`; only an existing file or a definition extension selects a file.
