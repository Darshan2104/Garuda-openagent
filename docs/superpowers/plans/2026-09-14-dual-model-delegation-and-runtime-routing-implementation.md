# Dual-model delegation and runtime routing implementation plan

**Status:** Ready for implementation

**Date:** 2026-09-14

**Design:**
[Dual-model delegation and cross-runtime routing](../specs/2026-09-14-dual-model-delegation-and-runtime-routing-design.md)

## Objective

Implement two routing layers without conflating their ownership:

1. A native Garuda reasoning controller can delegate bounded information
   collection to an independently configured cheaper model.
2. Garuda can choose an initial `AgentRuntime` using explicit selection,
   deterministic rules, or an optional classifier, then explicitly hand a live
   task to another runtime at a safe boundary.

The implementation must preserve current native single-model behavior when new
configuration is absent.

## Delivery rules

- Implement in the dependency order below. Do not begin ACP adapters or routing
  surfaces before runtime sessions, workspace authority, and handoff rollback
  are working with fake runtimes.
- Use tests first for every contract and state transition.
- Keep each commit and pull request focused on one numbered task or one tightly
  coupled pair explicitly identified below.
- Run the narrowest relevant tests before the full suite.
- Preserve existing user changes. The current worktree contains unrelated web,
  observability, context, and runtime-loop edits; begin implementation only from
  a clean branch or isolated worktree after those changes are integrated.
- Put shared entry-point wiring in `garuda/agents/setup.py` or the new runtime
  orchestrator, never in each interface independently.
- Treat permissions and logical execution-authority tokens as guardrails and
  orchestration invariants. Only Docker or another real isolation mechanism may
  be described as confinement.
- Do not add provider secrets or external-harness OAuth handling.
- Do not enable collection, external runtimes, project-controlled routing, or
  classifier routing by default during the initial rollout.
- Configure exactly two model bindings in this scope: `reasoning` and
  `collection`. Verifier, summarizer, rigorous planner/critic, controller, worker,
  and classifier are call purposes for accounting; they do not create more
  configurable model slots. Verifier, summarizer, planner, and critic continue
  to use the reasoning binding in this release, while worker and classifier use
  the collection binding unless the approved fallback applies.

## Dependency graph

```text
Baseline measurements
        |
        v
Model config -> model factory -> shared native setup
        |                              |
        v                              v
Terminal strategy -> collection policy -> collection coordinator
                                             |
                                             v
                              budgets, fallback, observability

AgentRuntime protocol -> unified sessions -> authority/diff/context
        |                                      |
        v                                      v
Native runtime bridge                    handoff transaction
                                                |
                                                v
                                 fake ACP client and conformance
                                                |
                                                v
                                      first ACP adapters
                                                |
                                                v
                            deterministic router -> classifier
                                                |
                                                v
                                      product surfaces/evals
```

The dual-model feature can ship after Task 7. Cross-runtime routing requires
Tasks 8 through 15.

## Task 0: establish baselines and update planning boundaries

### Files

- Create `docs/evaluation/dual-model-routing.md`.
- Create `garuda/eval/dual_model.py` if existing ablation helpers cannot express
  paired role comparisons cleanly.
- Create `tests/test_dual_model_evaluation.py`.
- Update `.context/decisions.md`.
- Update `docs/roadmap/2026-08-acp-orchestration.md`.
- Update `docs/roadmap/issue-catalog.md`.

### Steps

1. Define a paired result schema containing:
   - task and trial identity;
   - success and verification outcome;
   - total tokens and total cost;
   - reasoning-model tokens and cost;
   - collection-model tokens and cost;
   - classifier tokens and cost;
   - wall time, model time, and tool time;
   - file/search investigation counts;
   - collection job, fallback, and stale-report counts.
2. Add deterministic fixture tests proving that an unavailable price is emitted
   as unknown rather than zero.
3. Document a representative benchmark mix: read-heavy exploration, debugging,
   implementation, document analysis, and tasks where delegation should not be
   used.
4. Record the approved durable decisions:
   - reasoning and collection are model roles inside the native runtime;
   - external harnesses remain `AgentRuntime` implementations;
   - collection is controller-requested rather than per-turn automatic routing;
   - initial harness routing may classify, but mid-run switching is explicit;
   - project configuration cannot authorize providers or executables.
5. Amend the ACP roadmap so P1 includes initial selection and explicit handoff,
   while P2 retains autonomous mid-run policy routing.
6. Do not claim a cost win until a post-Task-7 paired run satisfies the release
   thresholds in the design.

### Verification

```bash
pytest tests/test_dual_model_evaluation.py tests/test_cost_accounting.py -q
ruff check garuda tests
```

### Commit

`docs(eval): define dual-model and routing baselines`

## Task 1: add model-role configuration types

### Files

- Create `garuda/model/config.py`.
- Create `garuda/config/routing.py`.
- Create `tests/test_model_bindings.py`.
- Create `tests/test_orchestration_config.py`.
- Modify `garuda/config/agent_home.py` only for reusable access to separately
  parsed global and project settings; do not weaken its trust boundary.

### Steps

1. Add immutable `ModelSpec` and `ModelBindings` types from the design.
2. Add `CollectionBudget`, `CollectionFallback`, and `CollectionPolicy` types.
3. Add typed configuration provenance so each resolved field can report whether
   it came from an explicit argument, environment, route, profile, project,
   global setting, legacy setting, or built-in default.
4. Parse global and project configuration separately. Do not apply trust rules
   after a shallow merge because doing so loses which file authorized a value.
5. Implement named global model aliases and named `model_bindings`.
6. Allow project settings and profiles to reference authorized aliases and
   narrow limits.
7. Reject project definitions of:
   - model provider strings not already authorized globally;
   - API bases;
   - runtime executable commands;
   - credential sources;
   - higher cost, token, concurrency, network, tool, or permission ceilings.
8. Implement the exact model precedence from the design. Preserve
   `GARUDA_MODEL` at environment-variable precedence for the reasoning role.
9. Validate non-negative limits, supported fallback values, known aliases, and
   mutually incompatible configuration.
10. Return typed actionable errors with source path and field name. Security- or
    trust-shaped ambiguity fails closed.

### Tests

- Empty configuration resolves one reasoning model and no collection model.
- `GARUDA_MODEL` retains legacy behavior.
- `GARUDA_REASONING_MODEL` beats `GARUDA_MODEL`.
- Explicit role values beat both environment variables.
- Route, profile, project, and global precedence follows the approved order.
- Project limits can narrow but cannot widen a global ceiling.
- Project provider, endpoint, executable, and credential declarations fail.
- Unknown aliases and invalid numeric limits fail with source-qualified errors.
- `.agent` continues to win over `.garuda` without losing nested unrelated
  aliases.

### Verification

```bash
pytest tests/test_model_bindings.py tests/test_orchestration_config.py tests/test_agent_home.py -q
ruff check garuda tests
```

### Commit

`feat(config): add trusted model-role bindings`

## Task 2: implement the model factory and role-aware model set

### Files

- Create `garuda/model/factory.py`.
- Modify `garuda/model/__init__.py`.
- Modify `garuda/model/litellm_model.py`.
- Modify `garuda/model/governor.py` only if role metadata must be accepted by its
  diagnostics; keep provider bucketing based on provider identity.
- Create `tests/test_model_factory.py`.
- Extend `tests/test_model_governor.py`.

### Steps

1. Add `ResolvedModels` containing a required reasoning `Model`, optional
   collection `Model`, their immutable specs, and provenance.
2. Implement a factory registry keyed by transport name. Register `litellm` as
   the built-in transport.
3. Map trusted `ModelSpec` fields onto `LitellmModel` without passing secrets
   through session configuration or events.
4. Support SDK-supplied `Model` objects for either role without wrapping them in
   LiteLLM.
5. Check `supports_tool_calling` for the collection role before a run starts.
6. Keep retry and concurrency behavior independent per client while retaining
   provider-wide governor buckets.
7. Make model identity available for events without exposing `api_base` query
   parameters or credentials.
8. Add compatibility translation from existing profile/config reasoning fields
   into the reasoning spec. Emit a deprecation warning only after every shipped
   entry point supports the new fields.

### Tests

- Two different LiteLLM provider/model names produce two clients.
- Two custom `ScriptModel`-compatible objects are retained by identity.
- Missing collection capability disables or rejects collection according to the
  resolved policy.
- The same provider shares the governor bucket; different providers do not.
- API keys are absent from repr, resolved config serialization, and events.
- Existing `LitellmModel.from_config` behavior remains compatible.

### Verification

```bash
pytest tests/test_model_factory.py tests/test_model_governor.py tests/test_reasoning.py -q
ruff check garuda tests
```

### Commit

`feat(model): construct reasoning and collection roles`

## Task 3: centralize native-run preparation

### Files

- Modify `garuda/agents/setup.py`.
- Modify `garuda/interfaces/main.py`.
- Modify `garuda/interfaces/cli.py`.
- Modify `garuda/interfaces/server.py`.
- Modify `garuda/interfaces/session.py`.
- Modify `garuda/sdk/software_agent.py`.
- Modify `garuda/sdk/conversation.py`.
- Modify recipe and evaluation entry points that construct models directly.
- Create `tests/test_setup_parity.py`.
- Extend `tests/test_model_defaults.py`, `tests/test_server_jobs.py`, and relevant
  SDK tests.

### Steps

1. Introduce a named `PreparedNativeRun` result in `agents/setup.py` containing
   profile, `AgentConfig`, resolved models, permissions, tools, agent, and MCP
   manager.
2. Extend `prepare_agent_run` to resolve model bindings and return the named
   result. Migrate all in-repository callers together so tuple position cannot
   drift.
3. Ensure argument parsers retain whether a model flag was explicitly provided.
   Do not populate the built-in model as an eager parser default.
4. Add `--reasoning-model`, `--collection-model`, and `--no-collection`.
   Preserve `--model` as an alias for the explicit reasoning value and reject
   conflicting values.
5. Add role-specific environment resolution through the shared configuration
   path, not direct `os.environ` reads in each interface.
6. Allow `SoftwareAgent` and `Conversation` constructors to accept strings,
   approved aliases, or supplied `Model` objects without breaking the existing
   `model=` parameter.
7. Ensure every entry point logs the same resolved model identities and config
   provenance.
8. Keep recipe-level model overrides scoped to their run and prevent one server
   job's models from leaking into another.
9. Carry an immutable collection setup/factory in `PreparedNativeRun`; do not
   construct the live `CollectionCoordinator` until `prepare_run` has the
   environment, parent context, buffer, events, permissions, and deadline.

### Tests

- CLI, chat, server, SDK, conversation, recipe, dashboard, and eval resolve the
  same inputs identically.
- Explicit `--model` still behaves as before.
- Omitted flags allow profile/project/global resolution rather than masking it.
- Conflicting alias flags produce an actionable parse error.
- Concurrent server jobs may use different model bindings without shared state.
- Single-model fixtures see no collection tool or extra model call.

### Verification

```bash
pytest tests/test_setup_parity.py tests/test_model_defaults.py tests/test_server_jobs.py tests/test_phase8.py -q
ruff check garuda tests
```

### Commit

`refactor(setup): centralize role-aware native run assembly`

## Task 4: parameterize terminal-tool handling in the native loop

### Files

- Create `garuda/core/termination.py`.
- Modify `garuda/core/loop.py`.
- Modify `garuda/core/run_state.py`.
- Modify `garuda/core/completion.py` only to adapt its current contract.
- Extend `tests/test_core_v2.py` and `tests/test_tool_call_adjacency.py`.
- Create `tests/test_termination_strategy.py`.

### Steps

1. Define a small `TerminalStrategy` protocol with:
   - the terminal tool name;
   - whether a forced final submission is supported;
   - an async attempt returning a typed accepted/rejected decision and summary;
   - deferred-note flushing needed to preserve provider message adjacency.
2. Wrap the existing `CompletionGate` in the default task-completion strategy.
3. Replace hard-coded `call.name == "task_complete"` checks with strategy
   matching.
4. Update forced final submission to use the strategy's schema and skip itself
   when the strategy does not support forced submission.
5. Preserve answer-closing behavior for accepted terminal calls and unexecuted
   siblings.
6. Keep all existing completion, verification, livelock, and event behavior
   byte-compatible where practical.
7. Do not add collection behavior in this task; prove the seam first with a
   deterministic fake strategy.

### Tests

- Existing `task_complete` acceptance and rejection paths are unchanged.
- Forced final submission still exposes only the correct terminal tool.
- A fake alternate terminal name can finish a child run.
- Rejected terminal calls preserve assistant/tool adjacency.
- Accepted terminal calls close every sibling tool call.
- A strategy without forced submission exhausts cleanly.

### Verification

```bash
pytest tests/test_termination_strategy.py tests/test_tool_call_adjacency.py tests/test_verifier_v2.py tests/test_gate_livelock_and_rewrite.py -q
ruff check garuda tests
```

### Commit

`refactor(core): make run termination pluggable`

## Task 5: add tool effects and the collection safety policy

### Files

- Modify `garuda/tools/protocol.py`.
- Modify built-in tools under `garuda/tools/` to declare effects.
- Create `garuda/core/collection.py` with request/report/policy types and toolkit
  filtering only.
- Modify `garuda/mcp/config.py` for globally trusted effect declarations.
- Modify `garuda/tools/discovery.py` so lazy dispatch re-checks the selected tool.
- Create `tests/test_tool_effects.py`.
- Create `tests/test_collection_policy.py`.
- Extend `tests/test_mcp_allowlist.py` and `tests/test_project_tools.py`.

### Steps

1. Add `ToolEffect` and a helper that returns `UNKNOWN` when a tool has no
   declaration.
2. Annotate every built-in tool deliberately. Do not infer safety from its name.
3. Define the hard collection ceiling and construct the effective toolkit by
   intersection with profile, request scope, network policy, and permissions.
4. Keep shell execution disabled by default. If enabled, expose a constrained
   readonly wrapper rather than the unrestricted `bash` schema.
5. Treat custom and MCP tool effects as unknown unless a trusted global manifest
   declares them.
6. Ensure project configuration cannot bless its own custom or MCP tools as
   read-only.
7. Make lazy MCP `use_tool` enforce the underlying effect and ordinary
   permission screen before dispatch.
8. Exclude `task_complete`, goal/todo mutation, background tools, tmux execution,
   edits, writes, nested delegation, and collection delegation from workers.

### Tests

- Every built-in has an intentional effect classification.
- Unknown tools are absent from collection.
- Project-owned declarations cannot turn unknown tools into read-only tools.
- Trusted global declarations may enable one exact MCP tool.
- Lazy MCP dispatch cannot bypass an effect denial.
- A malicious collection profile cannot add mutation or permission tools.
- Network-disabled collection excludes external-read tools.
- Readonly shell rejects composition and mutating commands.

### Verification

```bash
pytest tests/test_tool_effects.py tests/test_collection_policy.py tests/test_mcp_allowlist.py tests/test_project_tools.py tests/test_permissions_v2.py -q
ruff check garuda tests
```

### Commit

`feat(collection): enforce a non-mutating tool ceiling`

## Task 6: implement structured collection execution

### Files

- Complete `garuda/core/collection.py`.
- Create `garuda/tools/collection.py`.
- Modify `garuda/tools/__init__.py`.
- Modify `garuda/core/run_state.py`.
- Modify `garuda/tools/protocol.py` to carry the coordinator in `ToolContext`.
- Reuse focused helpers from `garuda/core/subagent.py` without making general
  subagents responsible for collection policy.
- Create `garuda/agents/defaults/collection.yaml` if a dedicated built-in profile
  is clearer than applying a hard ceiling over `explore`.
- Create `tests/test_collection_report.py`.
- Create `tests/test_collection_runner.py`.
- Create `tests/test_collection_integration.py`.

### Steps

1. Implement `CollectionRequest`, `EvidenceRef`, `CollectionReport`, attempt
   metadata, and job states.
2. Add `submit_collection` as an internal terminal tool and implement its
   terminal strategy.
3. Add public `delegate_collection` only when a collection model resolves and
   policy enables it.
4. Validate objectives, questions, path scopes, source scopes, turn/token
   overrides, and handoff choice before starting a child.
5. Build a separate `ContextManager` using the collection model and its limits.
6. Support only `none` and `brief` context. Build brief context from the working
   state card and validated buffer references; never fall back to full context.
7. Run the child through `DefaultAgent` with the collection terminal strategy,
   constrained tools, its own event store, and no main completion verifier.
8. Validate submitted evidence paths, URLs, commands, and buffers before
   returning a report.
9. Persist the child event log beside the parent and link parent event, child
   session, collection job, and attempt IDs.
10. Return only a bounded report to the primary context. Preserve full outputs in
    buffers and the child trace.
11. Register `delegate_collection` as parallel-safe only after the coordinator
    guarantees a non-mutating toolkit.
12. Bind the prepared collection factory inside `prepare_run`, after the monitored
    environment, parent context, buffer, and deadline exist.
13. Insert `delegate_collection` after ordinary profile filtering when trusted
    collection configuration enables it, while still honoring an explicit
    profile-level disable. Do not require every existing profile to list a tool
    that does not exist in single-model runs.
14. Keep general `invoke_subagent` runs on the reasoning binding; adding arbitrary
    per-subagent model selection is outside this scope.

### Tests

- Scripted reasoning and collection models complete an end-to-end delegated
  read.
- The reasoning model, not the worker, performs the eventual edit and completion.
- Worker termination cannot complete the parent.
- Full context is rejected.
- Brief context carries task state and buffer pointers but not raw transcript.
- Evidence pointing outside the allowed workspace or to a missing buffer fails.
- A tool-free prose response does not masquerade as a structured report.
- Child events remain separate and joinable from the parent.
- Single-model runs have an unchanged tool schema.

### Verification

```bash
pytest tests/test_collection_report.py tests/test_collection_runner.py tests/test_collection_integration.py tests/test_subagent_traces.py tests/test_context_budget.py -q
ruff check garuda tests
```

### Commit

`feat(collection): delegate bounded evidence gathering`

## Task 7: add collection budgets, fallback, staleness, and metrics

### Files

- Modify `garuda/core/collection.py`.
- Modify `garuda/core/metrics.py`.
- Modify `garuda/core/events.py`.
- Modify `garuda/eval/costs.py` and `garuda/eval/atif_export.py`.
- Modify `garuda/observability/tracing.py` and trajectory readers.
- Create `tests/test_collection_budget.py`.
- Create `tests/test_collection_fallback.py`.
- Create `tests/test_collection_staleness.py`.
- Extend `tests/test_metrics.py`, `tests/test_cost_accounting.py`, and
  `tests/test_run_observability.py`.

### Steps

1. Implement an `asyncio.Lock`-protected collection ledger that atomically
   reserves jobs, tokens, known dollar cost, and wall-time share.
2. Add a collection-specific semaphore in addition to the provider governor.
3. Release unused reservations on every terminal and cancellation path while
   retaining actual consumed usage.
4. Enforce token ceilings when model cost is unknown. Reject hard dollar-only
   budgets that cannot price the configured model.
5. Implement mode-specific fallback and guarantee at most one fallback attempt.
6. Restart fallback as a new bounded child attempt using the reasoning model and
   the same collection policy.
7. Capture Git HEAD, dirty fingerprint, scoped file fingerprints, and known
   background-writer state before and after the job.
8. Mark changed observations stale. In strict mode, block live collection with a
   known untracked writer unless an isolated snapshot is configured.
9. Track child tasks and cancel/await them if the parent is cancelled or closes.
10. Emit collection lifecycle and model-fallback events with role, model, job,
    attempt, duration, usage, cost-known status, and staleness.
11. Aggregate metrics by reasoning, collection, classifier, verifier, and
    summarizer roles without breaking current totals.
12. Record both `model_binding_role` (`reasoning` or `collection`) and
    `call_purpose` (`controller`, `collector`, `classifier`, `verifier`,
    `summarizer`, `planner`, or `critic`) so purpose-level metrics do not imply
    additional configurable models.

### Tests

- Concurrent jobs cannot oversubscribe a shared limit.
- Cancellation releases reservations and leaves no child task running.
- Provider failure triggers exactly the configured fallback.
- Eval and rigorous defaults return a failure without fallback.
- Fallback retains the worker safety ceiling.
- Both attempts count toward total tokens and cost.
- Unknown cost never passes a hard dollar-only limit.
- Workspace mutation during a job marks the report stale.
- Background-writer policy behaves differently in interactive and strict modes.
- Parent and child traces attribute every model call to one role.

### Verification

```bash
pytest tests/test_collection_budget.py tests/test_collection_fallback.py tests/test_collection_staleness.py tests/test_metrics.py tests/test_cost_accounting.py tests/test_run_observability.py -q
ruff check garuda tests
pytest -q
```

### Manual milestone check

Run paired single-model and dual-model trials from Task 0. Publish all metrics,
including regressions. Keep collection opt-in unless the approved cost and
quality thresholds pass.

### Commit

`feat(collection): enforce budgets and role-aware accounting`

## Task 8: define the runtime protocol, registry, and fake runtime

### Files

- Create `garuda/runtime/__init__.py`.
- Create `garuda/runtime/protocol.py`.
- Create `garuda/runtime/registry.py`.
- Create `garuda/runtime/fake.py`.
- Create `tests/test_runtime_protocol.py`.
- Create `tests/test_runtime_registry.py`.
- Create `tests/test_runtime_conformance.py`.

### Steps

1. Define immutable runtime identity, version, capability, health,
   authentication, workspace, permission, and failure types.
2. Define lifecycle states with explicit allowed transitions.
3. Define async `AgentRuntime` operations for health, start, resume, prompt,
   event polling/streaming, boundary pause, permission reply, cancellation, and
   close.
4. Include native session identity and normalized event cursors.
5. Build a per-orchestrator `RuntimeRegistry`; do not use process-global mutable
   registration in a multi-job server.
6. Parse trusted global manifests and project alias references according to Task
   1 trust rules.
7. Implement a deterministic in-process fake runtime capable of success,
   streaming, approval, malformed event, delayed boundary, mutation attempt,
   cancellation, and startup failure scenarios.
8. Write one parameterized lifecycle conformance suite that every runtime must
   pass.

### Tests

- Invalid lifecycle transitions fail explicitly.
- Registry instances cannot leak runtime registrations between jobs.
- Discovery does not install, log in, or inspect tokens.
- Unknown capabilities and incompatible versions are actionable.
- Fake runtime exercises every normalized lifecycle outcome.

### Verification

```bash
pytest tests/test_runtime_protocol.py tests/test_runtime_registry.py tests/test_runtime_conformance.py -q
ruff check garuda tests
```

### Commit

`feat(runtime): define lifecycle and registry contracts`

## Task 9: add unified sessions and the native runtime adapter

### Files

- Create `garuda/runtime/session.py`.
- Create `garuda/runtime/native.py`.
- Modify `garuda/core/sessions.py`.
- Modify `garuda/interfaces/runner.py`.
- Modify `garuda/core/events.py`.
- Create `tests/test_unified_sessions.py`.
- Create `tests/test_native_runtime.py`.
- Extend `tests/test_sessions_resume.py` and `tests/test_server_jobs.py`.

### Steps

1. Add an explicit session schema version and runtime-segment records.
2. Store routing decision, workspace baseline identity, active segment,
   capability snapshot, permission ceiling, authority generation, context
   revision, event cursor, handoff attempts, and recovery target.
3. Migrate legacy native sessions in memory and write the upgraded form only via
   the existing atomic locked update path.
4. Leave the original readable if migration or write fails. Reject unknown future
   versions.
5. Implement `NativeGarudaRuntime` as an adapter over the current loop, tools,
   model bindings, environment, and session checkpoints.
6. Make `run_agent_task` a compatibility facade over the native runtime rather
   than duplicating lifecycle setup.
7. Normalize existing native events with runtime-segment identity while keeping
   historical readers tolerant of absent fields.
8. Ensure native start, resume, cancel, close, and boundary pause satisfy the
   common runtime conformance suite.

### Tests

- A legacy session resumes as one native segment.
- Failed migration leaves original files unchanged.
- Future schemas fail with an actionable error.
- Native execution through `AgentRuntime` produces the same task result and
  verifier behavior as the compatibility facade.
- Concurrent session updates remain atomic and locked.
- A cancelled native runtime reaches an unambiguous terminal state.

### Verification

```bash
pytest tests/test_unified_sessions.py tests/test_native_runtime.py tests/test_sessions_resume.py tests/test_server_jobs.py -q
ruff check garuda tests
```

### Commit

`feat(runtime): bridge native runs into unified sessions`

## Task 10: implement workspace leases, authority, and authoritative diffs

### Files

- Create `garuda/runtime/authority.py`.
- Create `garuda/runtime/diff.py`.
- Create `tests/test_workspace_leases.py`.
- Create `tests/test_runtime_diffs.py`.
- Extend workspace safety and session tests.

### Steps

1. Canonicalize workspace identity without resolving it outside allowed roots.
2. Implement a process-safe workspace lease with owner session, runtime segment,
   mode, generation, heartbeat/expiry policy, and explicit release.
3. Treat readonly sharing separately from mutating ownership. Never infer
   writable safety from a permission label alone.
4. Add generation-based authority checks to every Garuda-mediated mutating
   operation.
5. For direct-filesystem external runtimes, require adapter process quiescence or
   an isolated worktree before transferring ownership.
6. Capture baseline commit, porcelain status, tracked diff, untracked inventory,
   and bounded fingerprints without altering the index or worktree.
7. Separate pre-existing user changes from runtime-introduced changes.
8. Represent additions, modifications, renames, deletions, and untracked files.
9. Reconcile external diff hints as progressive UI data while treating
   filesystem/Git inspection as authoritative.
10. Bound stored diffs and retain safe disk references for full content.

### Tests

- Two processes cannot acquire a mutating lease for one workspace.
- A stale generation cannot mutate through Garuda tools.
- Readonly observation does not become writable ownership.
- Lease release and crash-recovery behavior are deterministic.
- Dirty user files are not attributed to the runtime.
- Rename, delete, add, modify, and untracked cases round-trip.
- Diff collection never stages, resets, cleans, or rewrites user files.
- A non-quiescent direct-access runtime cannot hand off in place.

### Verification

```bash
pytest tests/test_workspace_leases.py tests/test_runtime_diffs.py tests/test_workspace_symlinks.py tests/test_permissions_v2.py -q
ruff check garuda tests
```

### Commit

`feat(runtime): enforce workspace ownership and diff truth`

## Task 11: implement transactional context packs

### Files

- Create `garuda/runtime/context_pack.py`.
- Create schema fixtures under `tests/fixtures/context_pack/`.
- Modify `.gitignore` only if current-task and handoff files are not already
  ignored correctly.
- Create `tests/test_context_pack_manager.py`.
- Create `tests/test_context_redaction.py`.

### Steps

1. Define versioned YAML frontmatter and bounded Markdown bodies for
   `.context/current-task.md` and `.context/handoff.md`.
2. Make `ContextPackManager` the only production writer of generated files.
3. Compile task, acceptance criteria, working state, repository delta,
   verification evidence, next action, blockers, buffer references, runtime
   identity, capabilities, authority snapshot, and context revision.
4. Preserve durable `.context` files byte-for-byte.
5. Validate workspace-relative paths and session-owned buffer references.
6. Redact secret-shaped values before persistence and record redaction metadata
   without storing the original value.
7. Exclude raw transcript, hidden reasoning, and unbounded command output.
8. Write to temporary files, fsync as appropriate, and atomically replace the
   generated targets as one revision. Retain the prior valid revision on failure.
9. Checkpoint the exact generated revision into the unified session store.

### Tests

- Valid context round-trips with a stable schema version.
- Unknown versions fail according to version policy.
- Secrets, cookies, tokens, reasoning blocks, and oversized output cannot enter
  generated files.
- Invalid absolute, parent-traversal, and missing buffer references fail.
- Durable context files are never rewritten by the manager.
- Injected write failure preserves the prior valid generated revision.
- Generated files remain gitignored while durable context stays tracked.

### Verification

```bash
pytest tests/test_context_pack_manager.py tests/test_context_redaction.py tests/test_context_archive.py -q
ruff check garuda tests
```

### Commit

`feat(context): compile transactional runtime handoffs`

## Task 12: implement the handoff state machine and recovery

### Files

- Create `garuda/runtime/handoff.py`.
- Modify `garuda/runtime/session.py`.
- Modify `garuda/runtime/authority.py`.
- Modify `garuda/runtime/diff.py`.
- Create `tests/test_runtime_handoff.py`.
- Create `tests/test_handoff_faults.py`.
- Create `tests/test_handoff_recovery.py`.

### Steps

1. Encode every approved handoff state and allowed transition.
2. Acquire one session switch lock and reject overlapping handoffs.
3. Validate target health, authentication, version, capabilities, permission
   ceiling, and workspace support before pausing the source.
4. Request a source safe boundary and refuse the handoff on timeout or ambiguous
   activity.
5. Revoke the source authority generation and establish adapter-level process
   quiescence.
6. Checkpoint source session, event cursor, authoritative diff, and verification
   state.
7. Generate and validate the context pack. Recheck workspace revision before
   target launch.
8. Launch the target provisionally as the sole potential mutator.
9. Require context/workspace acknowledgement and persist it.
10. Commit the active runtime segment and new authority generation atomically.
11. Keep the source suspended and resumable.
12. Implement rollback for every pre-commit failure.
13. Enter `recovery_required` when the target changes the workspace before
    acknowledgement, cleanup cannot establish absence, or the workspace becomes
    ambiguous.
14. Implement explicit recovery choices: resume source, continue target, or
    inspect/export state. Never choose automatically when workspace truth is
    ambiguous.

### Tests

- Happy path transfers one task between two fake runtimes.
- Every transition emits one typed event.
- Fault injection at each step produces rollback or `recovery_required` exactly
  as specified.
- Source pause failure never launches the target.
- Target startup/acknowledgement failure resumes the source only after clean
  target shutdown and unchanged diff.
- Cancellation before commit rolls back; cancellation after commit cancels the
  target and leaves source suspended.
- Concurrent handoff requests serialize or reject cleanly.
- No transition creates two live potential mutators.

### Verification

```bash
pytest tests/test_runtime_handoff.py tests/test_handoff_faults.py tests/test_handoff_recovery.py tests/test_workspace_leases.py -q
ruff check garuda tests
```

### Commit

`feat(runtime): add transactional handoff and recovery`

## Task 13: implement the ACP subprocess client and conformance agent

### Files

- Create `garuda/runtime/acp/__init__.py`.
- Create `garuda/runtime/acp/process.py`.
- Create `garuda/runtime/acp/client.py`.
- Create `garuda/runtime/acp/normalizer.py`.
- Create `tests/fixtures/fake_acp_agent.py`.
- Create `tests/fixtures/acp/` protocol fixtures.
- Create `tests/test_acp_process.py`.
- Create `tests/test_acp_client.py`.
- Create `tests/test_acp_normalizer.py`.
- Add the ACP runtime to `tests/test_runtime_conformance.py` using only the fake
  process.

### Steps

1. Confirm and pin the supported ACP protocol/version range before adding a
   dependency. Reject unknown incompatible versions.
2. Launch only the resolved trusted executable and arguments; never accept a
   command from prompt text.
3. Minimize the child environment and forward secrets only through explicit
   trusted configuration. Do not log inherited values.
4. Implement framed JSON-RPC read/write, request IDs, startup timeout,
   heartbeat, cancellation, graceful close, and forced cleanup.
5. Negotiate capabilities and assign execution/tool authority once.
6. Normalize messages, tool activity, approvals, diffs, usage, errors, and
   lifecycle changes without claiming unsupported semantics.
7. Keep raw malformed frames out of normal views while retaining a bounded,
   redacted diagnostic reference.
8. Build a fake subprocess supporting fragmented frames, malformed frames,
   hangs, delayed boundaries, approvals, diff hints, resume, cancellation, and
   abnormal exit.

### Tests

- Partial and multiple frames decode correctly.
- Malformed and oversized frames fail with typed protocol errors.
- Startup and heartbeat timeouts clean up the process.
- Cancellation and close leave no child process.
- Capability negotiation refuses strict unsupported configurations.
- External diff hints do not replace authoritative workspace diffs.
- Logs and support data contain no child-environment secrets.
- The fake ACP adapter passes the common runtime conformance suite.

### Verification

```bash
pytest tests/test_acp_process.py tests/test_acp_client.py tests/test_acp_normalizer.py tests/test_runtime_conformance.py -q
ruff check garuda tests
```

### Commit

`feat(acp): add supervised subprocess runtime`

## Task 14: add first runtime manifests and opt-in live checks

### Files

- Add built-in manifests under a focused package-data directory such as
  `garuda/runtime/manifests/`.
- Modify `pyproject.toml` to ship manifests.
- Create adapter-specific modules only where generic ACP configuration cannot
  express documented behavior.
- Create `tests/test_runtime_manifests.py`.
- Create opt-in live tests under `tests/integration/runtime/`.
- Update `docs/guides/configuration.md` with authentication boundaries.

### Steps

1. Start with two documented ACP-compatible harnesses.
2. Resolve executable path, version, login status, and warnings without
   installing, logging in, or reading credential files.
3. Use generic manifest configuration for standard capabilities.
4. Add adapter code only for a verified protocol or lifecycle difference.
5. Mark unknown versions clearly; reject versions outside a tested compatibility
   range when safe behavior is not known.
6. Make live checks require an explicit environment flag and runtime name. They
   must never run in default CI or silently consume subscription quota.
7. Record the official supported integration surface in documentation before an
   adapter is marked supported.

### Tests

- Manifests are schema-valid and included in wheel package data.
- Missing executable, logged-out state, unknown version, and disabled runtime
  produce actionable health results.
- Each supported runtime passes the common fake contract tests.
- Opt-in live smoke tests cover health, start, boundary pause, resume, cancel,
  and close without storing credentials.

### Verification

```bash
pytest tests/test_runtime_manifests.py tests/test_runtime_conformance.py -q
ruff check garuda tests
```

### Commit

`feat(runtime): add first ACP harness manifests`

## Task 15: implement deterministic initial runtime routing

### Files

- Create `garuda/runtime/router.py`.
- Modify `garuda/config/routing.py`.
- Create `tests/test_runtime_router.py`.
- Create `tests/test_runtime_traits.py`.
- Extend `tests/test_runtime_registry.py` and session tests.

### Steps

1. Add `RunRequest`, `RoutingRule`, `RoutingCandidate`, and `RoutingDecision`.
2. Implement runtime precedence exactly as approved.
3. Detect bounded repository traits with direct filesystem reads. Do not invoke a
   shell or import project code.
4. Support agent, mode, tags, required capabilities, workspace kind, permission
   ceiling, language, marker-file, substring, and glob conditions.
5. Permit task regular expressions only in trusted global configuration and cap
   task input and pattern size.
6. Sort rules by descending priority then declaration order.
7. Record every considered candidate, matched rule, and rejection reason.
8. Validate health, authentication, capabilities, workspace support, and policy
   before creating the unified session segment.
9. Implement configured startup fallback. Recheck workspace baseline before
   trying it.
10. Project routes without global trust become explainable recommendations and do
    not auto-select.

### Tests

- Explicit runtime beats profile, rules, classifier slot, and defaults.
- Rule ordering is stable and deterministic.
- Trait detection is bounded and side-effect-free.
- Project regex and executable definitions are rejected.
- Untrusted project rules become recommendations.
- Unhealthy, logged-out, incapable, or policy-incompatible runtimes are rejected
  before start.
- Startup fallback requires an unchanged workspace.
- Every outcome has a complete routing rationale.

### Verification

```bash
pytest tests/test_runtime_router.py tests/test_runtime_traits.py tests/test_runtime_registry.py -q
ruff check garuda tests
```

### Commit

`feat(runtime): route initial tasks with trusted rules`

## Task 16: add the optional routing classifier

### Files

- Create `garuda/runtime/classifier.py`.
- Modify `garuda/runtime/router.py`.
- Modify role-aware metrics and cost export.
- Create `tests/test_runtime_classifier.py`.
- Extend `tests/test_runtime_router.py` and cost tests.

### Steps

1. Invoke the classifier only after deterministic rules produce no selection.
2. Resolve its model from the collection role by default. If that role is absent,
   skip classification unless trusted configuration explicitly permits the
   reasoning model.
3. Send task, bounded traits, agent, mode, and an approved candidate/capability
   table. Send no tools or workspace file contents.
4. Require a strict structured result with runtime ID, confidence, required
   capabilities, and a short rationale.
5. Limit the call to one attempt and one configured output budget.
6. Treat the result as untrusted: validate candidate membership, health,
   authentication, capabilities, workspace support, permission ceiling, and
   confidence.
7. Use the configured default on timeout, malformed output, unknown ID, low
   confidence, or failed validation.
8. Persist classifier identity, input digest, output, validation, latency, usage,
   and known cost in the routing decision.
9. Do not expose any method by which the classifier can initiate a handoff.

### Tests

- A valid high-confidence candidate is selected.
- Deterministic matches avoid the classifier call entirely.
- Invalid JSON, unknown runtime, timeout, low confidence, and capability mismatch
  use the documented default.
- Prompt-injected task text cannot add a runtime, command, or capability.
- Candidate lists are fixed before the call.
- No tools are present in the request.
- Classifier cost is attributed separately and unknown cost remains unknown.

### Verification

```bash
pytest tests/test_runtime_classifier.py tests/test_runtime_router.py tests/test_cost_accounting.py -q
ruff check garuda tests
```

### Commit

`feat(runtime): classify unmatched initial routes`

## Task 17: expose runtime selection, handoff, and recovery everywhere

### Files

- Modify `garuda/interfaces/main.py`.
- Modify `garuda/interfaces/server.py` and job schemas.
- Modify `garuda/sdk/software_agent.py`.
- Modify `garuda/sdk/conversation.py`.
- Modify dashboard backend and frontend runtime views.
- Modify `docs/reference/cli.md`.
- Modify `docs/guides/using-garuda.md`.
- Modify `docs/guides/web-dashboard.md`.
- Create `tests/test_runtime_cli.py`.
- Create `tests/test_runtime_server.py`.
- Create `tests/test_runtime_sdk.py`.
- Add browser checks for runtime selection and failed-switch recovery.

### Steps

1. Add `--runtime` and `--route-explain` to task-starting commands.
2. Add `garuda runtime list` and `garuda runtime inspect ID` with text and JSON
   output.
3. Add `garuda session handoff SESSION --to ID` and `garuda session recover
   SESSION`.
4. Return safe previews containing target, capabilities, source state, diff
   summary, context revision, and rollback implications.
5. Add versioned JSON-RPC methods for registry inspection, route preview,
   handoff, recovery, approval, cancellation, and event streaming.
6. Add SDK types and methods rather than exposing raw dictionaries.
7. Add dashboard runtime picker, health/capability warnings, route explanation,
   handoff preview, transition timeline, and recovery controls.
8. Drive all surfaces through the same router and handoff manager.
9. Keep current commands and SDK constructors defaulting to native behavior.

### Tests

- All surfaces produce the same routing result from the same request.
- Text and JSON diagnostics contain no secrets or raw transcript.
- Explicit handoff and recovery map to identical state transitions.
- Historical sessions render without runtime fields.
- Disconnected approval and handoff clients leave recoverable state.
- Browser tests show source, target, context revision, authority change, and
  failure recovery based only on normalized events.

### Verification

```bash
pytest tests/test_runtime_cli.py tests/test_runtime_server.py tests/test_runtime_sdk.py tests/test_web_routes.py tests/test_web_live_runs.py -q
ruff check garuda tests
```

Run the documented browser checks when Chrome is available.

### Commit

`feat(interfaces): expose runtime routing and handoff`

## Task 18: complete evaluation, documentation, and rollout gates

### Files

- Modify `garuda/eval/ablation.py` and relevant exporters.
- Complete `docs/evaluation/dual-model-routing.md` with measured results.
- Update `README.md` only with concise navigation or safe quick-start changes.
- Update `docs/ARCHITECTURE.md`.
- Update `docs/MODULES.md`.
- Update `docs/guides/configuration.md`.
- Update `docs/BACKLOG.md` by removing only items actually completed.
- Update `.context/architecture.md`, `.context/decisions.md`, and verified
  discoveries.
- Add or extend documentation contract tests.

### Steps

1. Run paired baselines and dual-model trials over the representative task mix
   with fixed versions, prompts, prices, and seeds where supported.
2. Report success, verification, total cost, cost by role, tokens by role,
   latency, investigation behavior, jobs, fallbacks, and stale reports.
3. Apply the approved release thresholds. Keep collection opt-in if total cost or
   quality does not pass.
4. Run the cross-runtime evaluation matrix without comparing unknown cost to
   zero.
5. Publish adapter conformance reports and exact tested versions.
6. Document trust boundaries, configuration precedence, fallback, classifier
   limits, handoff recovery, and the distinction between guardrails and
   confinement.
7. Update roadmap and backlog claims to match behavior actually shipped.
8. Verify package data includes runtime manifests and any static assets.
9. Run a clean-environment install using the README path to catch dependency and
   packaging failures hidden by constraints.

### Verification

```bash
pytest -q
ruff check garuda tests
```

Also run, when the environment supports them:

- documented CLI single-model and dual-model runs;
- SDK and JSON-RPC runs;
- native-to-fake and supported real-runtime handoffs;
- live browser dashboard checks;
- opt-in Docker and ACP adapter checks;
- a clean `pip install -e ".[dev]"` in a temporary virtual environment.

### Commit

`docs: publish dual-model and runtime routing behavior`

## Pull request grouping

Recommended review units:

1. Tasks 0-1: measurements, decisions, and trusted configuration.
2. Tasks 2-3: model factory and shared setup.
3. Task 4: terminal strategy refactor.
4. Tasks 5-6: collection policy and execution.
5. Task 7: budgets, fallback, and accounting; dual-model milestone.
6. Tasks 8-9: runtime contracts, sessions, and native bridge.
7. Tasks 10-12: workspace truth, context, and handoff transaction.
8. Task 13: ACP client and fake conformance.
9. Task 14: first external manifests.
10. Task 15: deterministic initial router.
11. Task 16: optional classifier.
12. Tasks 17-18: product surfaces, evaluation, and release documentation.

If a grouped pull request becomes difficult to review, split it at task
boundaries. Do not combine collection execution with ACP lifecycle code merely
because both use child processes or nested traces.

## Final acceptance checklist

### Dual-model native runtime

- Different providers can supply reasoning and collection models in one run.
- The reasoning model owns the parent conversation and all mutations.
- Collection accepts only bounded none/brief handoffs and non-mutating tools.
- Reports are structured, evidence-backed, bounded, and traceable.
- Concurrency, cancellation, staleness, and fallback are deterministic.
- Cost and tokens are attributed by role.
- Single-model behavior remains compatible.
- Paired evaluation results are published without hiding total-trajectory cost.

### Cross-runtime routing

- Explicit selection, trusted rules, and classifier fallback obey precedence.
- Every routing decision is explainable and persisted.
- Project configuration cannot authorize a provider or executable.
- Native and supported ACP adapters pass one conformance suite.
- One workspace has at most one potential mutating runtime owner.
- Handoffs checkpoint, compile, validate, acknowledge, and commit transactionally.
- Every injected failure rolls back or enters explicit recovery.
- Existing dirty work and historical sessions remain intact.
- Unknown subscription cost remains unknown.
- At least two supported runtimes complete an explicit real handoff.

### Release quality

- Narrow suites, full `pytest`, and Ruff pass.
- CLI, SDK, server, and dashboard seams are exercised end to end.
- Optional live checks are reported accurately and never implied when not run.
- Architecture, module map, guides, reference, roadmap, backlog, and durable
  context reflect the shipped boundary.
