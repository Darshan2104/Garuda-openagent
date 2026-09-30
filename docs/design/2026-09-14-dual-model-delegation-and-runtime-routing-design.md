# Dual-model delegation and cross-runtime routing

**Status:** Approved design; awaiting written-spec review

**Date:** 2026-09-14

**Scope:** Native Garuda model delegation, initial harness routing, and explicit
cross-runtime handoff

## Summary

Garuda will add two related but distinct routing layers.

1. Inside the native Garuda runtime, a reasoning model remains the controller
   while an optional cheaper collection model performs bounded, read-only
   investigation jobs.
2. Above all harnesses, a runtime router chooses the initial `AgentRuntime` from
   trusted configuration and may use a model classifier only when deterministic
   rules do not match. Switching harnesses after a run starts is explicit and
   transactional.

Models remain inference transports. External coding harnesses remain agent
runtimes. The two layers share configuration conventions, decision records,
budgets, and observability vocabulary, but they do not share an execution
protocol.

This design extends the approved ACP orchestration roadmap. It does not replace
the `AgentRuntime` boundary or place an external coding harness behind `Model`.

## Motivation

Read-heavy coding tasks often spend expensive reasoning-model tokens on locating
files, extracting relevant snippets, enumerating symbols, reading documents, and
collecting web evidence. A cheaper model with reliable tool calling can perform
those bounded jobs and return compact evidence to the reasoning model. The cost
claim must be measured over the complete trajectory: moving calls to a cheaper
model is useful only if total cost falls without unacceptable quality loss.

Different coding harnesses also have different strengths, native integrations,
permission systems, and model combinations. Garuda should be able to select an
appropriate harness at task start and explicitly hand an in-progress task to a
second harness without losing repository state, evidence, or recovery options.

## Goals

- Configure separate `reasoning` and `collection` model roles for one native
  Garuda run.
- Allow the two roles to use different providers through LiteLLM or custom SDK
  `Model` implementations.
- Keep the reasoning model authoritative for planning, interpretation,
  workspace mutation, verification, and final completion.
- Restrict collection jobs to bounded information gathering and return compact,
  evidence-backed reports.
- Attribute tokens, cost, latency, failures, and fallbacks to the correct model
  role.
- Choose an initial harness using explicit selection, trusted deterministic
  rules, and an optional classifier fallback.
- Support explicit, safe-boundary handoffs between native and ACP runtimes.
- Preserve one mutating owner per workspace and keep the source runtime
  recoverable until a handoff commits.
- Preserve current single-model, native-runtime behavior when new configuration
  is absent.

## Non-goals

- Automatically alternating the reasoning and collection models for ordinary
  controller turns.
- Inferring which portions of a single model response are reasoning versus tool
  calling.
- Allowing a collection worker to edit files, approve permissions, complete the
  parent task, or delegate further work.
- Intercepting or replacing model calls made internally by an external harness.
- Silently switching harnesses in the middle of a turn.
- Automatic mid-run harness switching in the first release.
- Translating hidden reasoning or opaque vendor transcripts between harnesses.
- Reading, copying, or persisting provider credentials or vendor OAuth tokens.
- Treating unknown subscription usage as free.

## Terminology

- **Reasoning model:** the model that owns the native Garuda controller loop.
- **Collection model:** an optional cheaper model used only by constrained
  collection jobs and, by default, the initial-runtime classifier.
- **Model binding:** the resolved mapping from a model role to a model transport.
- **Collection job:** a bounded child investigation requested by the reasoning
  model.
- **Runtime:** a complete coding-agent harness implementing `AgentRuntime`.
- **Runtime segment:** the portion of a unified session owned by one runtime.
- **Initial routing:** choosing a runtime before that runtime receives the task.
- **Handoff:** an explicit transfer between runtimes after a session has started.
- **Execution authority:** the revocable right to mutate a session workspace.

## Architectural invariants

1. The reasoning model owns the primary native conversation and final decisions.
2. A collection model is never substituted for an ordinary reasoning turn.
3. Collection execution is a child operation with a separate context, budget,
   trace, and terminal contract.
4. `Model` remains an inference protocol. `AgentRuntime` remains a harness
   lifecycle protocol.
5. Only the active runtime segment may hold mutating workspace authority.
6. A runtime switch occurs only at a quiescent boundary.
7. Project-controlled configuration cannot authorize a provider, endpoint,
   executable, credential source, or looser policy ceiling.
8. Routing and fallback decisions are persisted with their rationale.
9. Context packs contain semantic task state, not hidden reasoning or raw vendor
   transcripts.
10. Unknown cost remains unknown.

Execution authority is an orchestration invariant, not a sandbox claim. For a
runtime that accesses the filesystem directly, the adapter must establish
quiescence by suspending or terminating the source process before another runtime
is launched against the writable workspace. A runtime that cannot demonstrate
that property cannot participate in an in-place handoff; it requires an isolated
worktree or the handoff is refused.

## High-level architecture

```text
Task request
    |
    v
RuntimeRouter
    |- explicit runtime selection
    |- deterministic trusted rules
    `- optional classifier fallback
    |
    v
Selected AgentRuntime
    |- NativeGarudaRuntime
    |    `- DefaultAgent reasoning controller
    |         `- CollectionCoordinator
    |              `- bounded collection-model workers
    |
    `- AcpRuntime
         `- external harness owns its loop and tools

Explicit switch request
    `- HandoffManager -> another AgentRuntime
```

The native runtime and external runtimes expose the same lifecycle boundary to
the orchestrator. Model-role selection remains internal to
`NativeGarudaRuntime`.

## Part A: dual-model native execution

### Model configuration types

Add model-specific configuration under `garuda/model/` rather than expanding
`AgentConfig`, which should continue to describe loop posture, tools, context,
permissions, and completion gates.

```python
@dataclass(frozen=True)
class ModelSpec:
    transport: str
    model: str
    api_base: str | None = None
    reasoning_effort: str | None = None
    thinking_budget_tokens: int | None = None
    max_tokens: int | None = None
    timeout_sec: float | None = None


@dataclass(frozen=True)
class ModelBindings:
    reasoning: ModelSpec
    collection: ModelSpec | None = None
```

The initial configurable transport is `litellm`, which retains Garuda's current
provider coverage. SDK callers may provide objects satisfying `Model` for either
role. Provider-specific clients do not enter core modules.

`ModelFactory` validates and builds resolved clients once per run. It rejects an
unknown transport, invalid limits, and a collection model that cannot perform
tool calls. Existing reasoning settings on `AgentProfile` and `AgentConfig` are
accepted during a compatibility period and translated into the reasoning
`ModelSpec`; new configuration uses role-specific model settings.

No model spec stores an API key. `api_base` is accepted only from trusted global
configuration or an explicit caller because changing it changes where workspace
content is sent.

### Model role ownership

`DefaultAgent` continues to receive the reasoning `Model`. It owns:

- the primary conversation;
- task planning and decomposition;
- interpretation of collected evidence;
- permission-sensitive operations;
- all workspace mutations;
- acceptance criteria and verification;
- the final `task_complete` submission.

Garuda does not inspect a response and retroactively decide that it was a
collection turn. Collection begins only when the reasoning model invokes the
dedicated `delegate_collection` tool.

### Collection request and report

The controller submits a bounded request:

```python
@dataclass(frozen=True)
class CollectionRequest:
    objective: str
    questions: tuple[str, ...]
    allowed_paths: tuple[str, ...] = ()
    allowed_sources: tuple[str, ...] = ()
    handoff: Literal["none", "brief"] = "brief"
    max_turns: int | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class EvidenceRef:
    kind: Literal["file", "command", "document", "url", "buffer"]
    location: str
    detail: str


@dataclass(frozen=True)
class CollectionReport:
    summary: str
    findings: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    unknowns: tuple[str, ...]
    buffer_ids: tuple[str, ...]
    workspace_revision: str
    stale: bool
```

The public tool schema may use JSON equivalents of these types. The coordinator
adds job identity, attempts, usage, cost, fallback, and timing metadata rather
than trusting the worker to report them.

### Collection coordinator

`CollectionCoordinator`, assembled through `agents/setup.py`, owns:

- request validation and path/source scope;
- collection tool construction;
- child context construction;
- atomic budget reservation;
- collection concurrency;
- model selection and fallback;
- workspace revision checks;
- cancellation and cleanup;
- report validation;
- nested event persistence;
- compact result formatting for the controller.

It may reuse mechanisms from `SubagentRunner`, including brief context forks,
buffer sharing, and sibling event logs. General subagents and collection workers
remain distinct policies: an arbitrary subagent profile must not opt itself into
the collection model or loosen collection restrictions.

### Collection terminal contract

The collection worker terminates through an internal `submit_collection` tool,
not the parent's `task_complete` tool.

The current loop special-cases `task_complete`. Refactor that single concern into
a small terminal-tool strategy used by the existing `CompletionGate` and a new
`CollectionCompletionGate`. Do not duplicate `DefaultAgent` or its turn loop.

The collection gate accepts a report only when it:

- answers the requested questions or explicitly marks them unknown;
- supplies evidence for factual findings;
- stays within response-size limits;
- references accessible buffers;
- makes no workspace-modification claim;
- conforms to the structured schema.

It performs no main-task verification and can never complete the parent run.

### Collection tool and permission ceiling

Introduce an optional tool-effect declaration with a conservative default:

```python
class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    EXTERNAL_READ = "external_read"
    MUTATING = "mutating"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    UNKNOWN = "unknown"
```

The collection toolkit is the intersection of:

1. the selected collection profile;
2. the global collection allowlist;
3. the request's path/source scope;
4. the run's permission and network ceilings;
5. tools whose effect is trusted as `READ_ONLY` or an allowed
   `EXTERNAL_READ`.

Allowed by default:

- `read_file`, `grep`, `glob`, and `ls`;
- PDF, spreadsheet, and image readers;
- buffer lookup tools;
- web fetch/search when network policy allows them.

Denied by default:

- writes and edits;
- background processes and interactive terminals;
- goal, todo, or acceptance-contract mutation;
- `task_complete`;
- nested delegation;
- custom or MCP tools with unknown effects;
- MCP tools with side effects.

A globally trusted MCP declaration may mark a specific tool as read-only. Lazy
MCP dispatch must re-check the selected underlying tool's effect; a meta-tool
cannot bypass the collection ceiling.

Screened read-only shell execution is optional and off by default. When enabled,
it uses the readonly command classifier and disallows command composition that
cannot be classified safely.

These controls are guardrails on a local workspace. Strict write confinement
requires a read-only Docker mount or an isolated snapshot. Documentation and UI
must preserve that distinction.

### Collection context

Each job has its own `ContextManager` configured with the collection model's
token counter and context window. A `brief` handoff contains only:

- the collection objective and questions;
- original task summary and acceptance criteria;
- current working-state card;
- relevant changed-file names;
- bounded buffer references;
- path and source restrictions.

It does not copy the full primary transcript. A `none` handoff starts from the
request alone. A collection job cannot request a `full` handoff.

The validated report is appended as the parent tool result. The child transcript
is persisted beside the parent event log and linked by job and session IDs.

### Collection job state machine

```text
requested -> validated -> budget_reserved -> running
                                      |-> completed
                                      |-> completed_stale
                                      |-> failed
                                      `-> cancelled
```

Invalid requests fail before budget reservation. Every terminal state releases
unused reserved budget and provider-concurrency slots.

Several adjacent `delegate_collection` calls may run concurrently. The
coordinator has a separate `max_parallel_jobs` ceiling in addition to the
process-wide provider governor. Budget reservation is atomic so concurrent jobs
cannot collectively exceed a run limit. Results are returned to the primary
conversation in original tool-call order.

### Workspace consistency

A collection job records a bounded workspace revision before and after work:

- Git HEAD when available;
- dirty-state fingerprint;
- scoped file fingerprints where practical;
- whether Garuda knows a background writer is active.

If the revision changes, the report is returned as `completed_stale` with an
explicit warning. The reasoning controller decides whether to repeat it. A
strict policy blocks live-workspace collection while a known untracked writer is
active or runs the worker against an isolated snapshot.

### Budgets

`CollectionPolicy` includes:

- `max_jobs_per_run`;
- `max_parallel_jobs`;
- `max_turns_per_job`;
- `max_tokens_per_job`;
- `max_total_tokens_per_run`;
- `max_cost_usd_per_run` when every selected model attempt is priceable;
- `deadline_fraction` limiting collection's share of remaining wall time.

Unknown model cost cannot satisfy a hard dollar ceiling. Configuration must
either use a token ceiling for that model or reject the job. Both failed and
fallback attempts count toward totals.

### Fallback behavior

Fallback is profile-configurable with these defaults:

- `interactive`: retry the bounded collection job with the reasoning model;
- `readonly`: retry the bounded collection job with the reasoning model;
- `eval`: return a structured collection failure;
- `rigorous`: return a structured collection failure.

Fallback restarts the child job rather than handing collection back to the full
parent conversation. It retains the collection tool, permission, context, and
budget ceilings. A job falls back at most once. The original and fallback
attempts are linked, separately metered, and visible to the controller.

### Feature boundary for external runtimes

Dual-model delegation is initially available only inside
`NativeGarudaRuntime`. Garuda does not intercept an external runtime's model
calls or override its internal delegation. A later ACP capability may expose
Garuda collection as an orchestrator-provided service, but that requires
execution-authority negotiation and is outside this design's first release.

## Part B: runtime routing and handoff

### `AgentRuntime` boundary

Add a versioned runtime protocol covering:

- identity, implementation version, and native session ID;
- discovery, health, and authentication status;
- capability negotiation;
- start and resume;
- prompt delivery and normalized event streaming;
- pause at a safe boundary;
- permission replies;
- cancellation and close;
- explicit typed failures.

The existing native loop becomes `NativeGarudaRuntime`. ACP-compatible external
harnesses use `AcpRuntime`. The orchestrator never passes an external runtime to
the `Model` protocol.

### Runtime registry

`RuntimeRegistry` resolves trusted manifests and exposes immutable runtime
descriptors. A descriptor includes:

- stable runtime ID and runtime kind;
- configured command for ACP runtimes;
- version and compatibility information;
- health and authentication state;
- declared and negotiated capabilities;
- supported workspace and permission behavior;
- optional cost-availability metadata;
- warnings and actionable setup guidance.

Discovery never installs software, initiates login, reads vendor tokens, or
imports credentials. Project configuration may reference a trusted runtime ID
but cannot define its command.

### Initial runtime router

`RuntimeRouter` receives a bounded `RunRequest` containing task text, explicit
tags, agent profile, mode, workspace kind, permission ceiling, and explicit
overrides.

Runtime selection precedence is:

1. explicit SDK, API, or CLI runtime;
2. explicit profile runtime pin;
3. the first matching trusted deterministic rule, ordered by descending priority
   and then declaration order;
4. optional classifier result;
5. configured default runtime;
6. built-in `native`.

Deterministic rules may match:

- agent profile and run mode;
- explicit task tags;
- required capabilities;
- workspace kind and permission ceiling;
- languages detected through bounded file inspection;
- marker files such as `pyproject.toml` or `package.json`;
- task substrings or glob patterns;
- global-only task regular expressions.

Repository traits are detected with direct filesystem inspection, not shell
commands. Project-owned regular expressions are excluded because a pathological
pattern could block routing.

### Classifier fallback

The classifier runs only when no deterministic rule matched. It uses the
collection model by default and falls back to the reasoning model only when
explicitly configured.

It receives:

- task text;
- agent profile and mode;
- bounded repository traits;
- candidate runtime IDs and capabilities;
- coarse health, availability, and cost-known status.

It has no tools and returns one structured recommendation:

```json
{
  "runtime": "codex",
  "confidence": 0.88,
  "required_capabilities": ["file-edit", "terminal"],
  "reason": "Python implementation task requiring repository edits"
}
```

The result is untrusted. Garuda validates the runtime ID against the configured
candidate set, re-checks health and capabilities, enforces workspace and
permission policy, and applies the confidence threshold. Invalid, timed-out, or
low-confidence results select the configured default. The classifier runs at
most once before session start and cannot initiate a handoff.

### Routing decision record

Every selection produces a `RoutingDecision` containing:

- selection source: explicit, profile, rule, classifier, default, or fallback;
- candidates considered;
- rule matches and candidate rejection reasons;
- classifier model, confidence, rationale, latency, usage, and cost when used;
- selected runtime and capability snapshot;
- startup fallback policy.

Persist the decision before starting the runtime so failed starts remain
explainable.

### Unified session

A versioned unified session stores:

- Garuda session ID and lifecycle state;
- original task and resolved run configuration;
- workspace/worktree identity and baseline;
- initial dirty-state fingerprint;
- routing decision;
- ordered runtime segments;
- active runtime and native session ID;
- capability and permission snapshots;
- execution-authority generation;
- context revision and normalized event cursor;
- handoff attempts and recovery target;
- verification and final repository delta.

Old native sessions migrate to one native runtime segment. Migration remains
atomic and locked. A failed migration leaves the original readable; an unknown
future schema version fails with an actionable error.

### Explicit handoff transaction

A handoff can be requested through CLI, SDK, JSON-RPC, or dashboard controls. It
may begin only when the source runtime is not streaming a model response,
executing a tool, awaiting approval, or mutating the workspace.

```text
requested
  -> target_validated
  -> source_pausing
  -> source_checkpointed
  -> handoff_compiled
  -> target_starting
  -> target_acknowledged
  -> committed
```

The transaction performs these steps:

1. Acquire the unified session switch lock.
2. Validate target health, authentication, version, capabilities, permission
   ceiling, and workspace support.
3. Pause the source at a safe boundary.
4. Revoke the source execution-authority generation and establish source-process
   quiescence.
5. Checkpoint its native session and normalized event cursor.
6. Capture authoritative repository delta and verification evidence.
7. Compile versioned `current-task.md` and `handoff.md`.
8. Redact and validate generated context.
9. Start the target provisionally as the only process with potential writable
   workspace access.
10. Require acknowledgement of context revision and workspace identity.
11. Atomically set the target as the active runtime segment.
12. Grant the target a new execution-authority generation.
13. Retain the source as suspended and resumable.

When the adapter supports mediated workspace access, the target receives no
mutation authority before acknowledgement. When an external runtime requires
direct writable access at launch, Garuda treats it as the provisional authority
holder from that moment and keeps the source process suspended or terminated.
The target receives only the handoff acknowledgement prompt until it confirms the
context revision. Unexpected mutation during this phase enters
`recovery_required`.

### Context pack

`ContextPackManager` remains the only writer of generated context files. The
handoff contains:

- original task and acceptance criteria;
- current status and next action;
- decisions and verified discoveries;
- added, changed, renamed, and deleted files;
- separation of pre-existing user changes from runtime changes;
- verification commands and observed results;
- unresolved questions, failures, and blockers;
- bounded buffer references;
- source runtime, version, native session ID, and context revision;
- capability and execution-authority snapshots;
- redaction metadata.

It excludes hidden reasoning, vendor-internal transcripts, credentials, cookies,
tokens, and large raw tool output. Paths and buffer references must validate
against the workspace and session store. Unknown schema versions are rejected.

The target is instructed to read repository guidance and the generated context
pack. Acknowledgement records the exact context revision it accepted.

### Rollback and recovery

- If target validation or startup fails, close the target and restore source
  authority.
- If the source cannot pause, do not create or start the target.
- If context validation fails, keep the source active.
- If the target fails before acknowledgement, close it and resume the source.
- If the target mutates before acknowledgement or outside its provisional
  authority, enter `recovery_required`; do not resume either runtime
  automatically.
- If the workspace changes during context compilation, invalidate and regenerate
  the handoff.
- Cancellation before commit rolls back to the source.
- Cancellation after commit cancels the target while the source remains
  suspended until explicit recovery.

If an initially selected runtime fails before receiving the task, Garuda may try
the configured startup fallback after confirming that the workspace still
matches its baseline. An unexplained delta blocks automatic fallback.

## Configuration contract

### Trusted global example

The following values are illustrative aliases, not built-in provider choices.

```yaml
models:
  strong:
    transport: litellm
    model: provider-a/reasoning-model
    reasoning_effort: high
    max_tokens: 16000

  collector:
    transport: litellm
    model: provider-b/collection-model
    max_tokens: 6000
    timeout_sec: 90

model_bindings:
  default:
    reasoning: strong
    collection: collector

collection:
  enabled: true
  profile: explore
  handoff: brief
  budget:
    max_jobs_per_run: 8
    max_parallel_jobs: 3
    max_turns_per_job: 12
    max_tokens_per_job: 30000
    max_cost_usd_per_run: 0.50
  fallback:
    interactive: reasoning
    readonly: reasoning
    eval: fail
    rigorous: fail

runtimes:
  native:
    type: native
    model_bindings: default
  codex:
    type: acp
    command: ["codex", "acp"]
    enabled: true
  claude:
    type: acp
    command: ["claude", "acp"]
    enabled: true

routing:
  default_runtime: native
  rules:
    - id: planning-with-claude
      priority: 100
      when:
        agents: [plan]
        task_tags: [architecture]
      runtime: claude
    - id: python-build-with-codex
      priority: 90
      when:
        agents: [build]
        languages: [python]
      runtime: codex
  classifier:
    enabled: true
    model_role: collection
    minimum_confidence: 0.75
    candidates: [native, codex, claude]
    on_failure: default
```

Credentials continue to use provider-supported environment variables, official
CLI sessions, or documented credential stores.

### Project configuration

A repository `.agent/settings.yaml` may:

- provide task tags and repository traits;
- reference globally authorized model-binding and runtime aliases;
- add routing preferences and narrower capability requirements;
- lower budgets and permission ceilings;
- disable collection or classification.

It may not:

- define or replace an executable command;
- define an API base or credential source;
- authorize a new provider or runtime;
- raise global budget, concurrency, tool, network, or permission ceilings;
- enable automatic project routing unless global configuration permits it.

Without global project-routing trust, project rules are displayed as
recommendations and require explicit selection.

### Profile configuration

An agent profile may reference approved aliases and narrow collection behavior:

```yaml
name: build
model_bindings: default
preferred_runtime: codex
collection:
  enabled: true
  profile: explore
  max_jobs_per_run: 5
```

Profile configuration cannot define runtime commands, provider endpoints, or
loosen the resolved policy ceiling.

### Model precedence

Model-role resolution, from highest to lowest precedence:

1. explicit SDK, API, or CLI role override;
2. role-specific environment variable;
3. legacy `GARUDA_MODEL` for the reasoning role;
4. selected route's approved binding;
5. agent profile binding;
6. project default referencing a trusted alias;
7. global default binding;
8. built-in reasoning model.

If no collection model resolves, `delegate_collection` is not exposed and the
run behaves as a current single-model run.

### CLI and environment compatibility

Add:

```text
--runtime
--reasoning-model
--collection-model
--no-collection
--route-explain
GARUDA_RUNTIME
GARUDA_REASONING_MODEL
GARUDA_COLLECTION_MODEL
```

Keep `--model` as an alias for `--reasoning-model` and preserve
`GARUDA_MODEL` as its legacy environment fallback.

Argument parsing must retain the provenance of a value instead of eagerly
turning every omitted flag into the built-in default. Otherwise an implicit CLI
default would incorrectly override profile, project, and global bindings.

Add runtime inspection and handoff commands:

```bash
garuda runtime list
garuda runtime inspect codex
garuda session handoff SESSION --to claude
garuda session recover SESSION
```

The same fields and operations must be exposed through the SDK, JSON-RPC
service, recipes where applicable, and dashboard.

## Persistence and observability

A unified session contains ordered runtime segments and nested collection jobs:

```text
Garuda session
|- runtime segment 1: native / native session ID
|- collection job 1 / child session ID
|- handoff 1 / context revision / repository delta
`- runtime segment 2: codex / native session ID
```

Add normalized event types:

- `routing_decision`;
- `runtime_start`, `runtime_ready`, and `runtime_failure`;
- `collection_requested`, `collection_started`, and `collection_completed`;
- `model_fallback`;
- `handoff_requested`, `handoff_checkpointed`, and `handoff_committed`;
- `handoff_rolled_back` and `recovery_required`;
- `workspace_authority_changed`.

Every native model-response event includes `model_role`, `model_name`, and
attempt identity. Every runtime event includes runtime ID, adapter version, and
native session identity when known. Handoff events include source and target
segments and context revision.

Session summaries group tokens, cost, and latency by:

- reasoning model;
- collection model;
- routing classifier;
- verifier and summarizer calls where those are separately attributable;
- external runtime when the runtime exposes trustworthy usage.

Provider-reported cost remains preferred, followed by configured reproducible
pricing. Missing external subscription cost is represented as `null` with a
reason, never zero.

Event logs remain append-only. Session metadata and context revisions remain
atomic. Normal event views and support bundles exclude raw hidden reasoning and
secrets.

## Module changes

### New model and collection modules

- `garuda/model/config.py`: model specs, bindings, validation, and compatibility
  translation.
- `garuda/model/factory.py`: provider-agnostic model construction.
- `garuda/core/collection.py`: coordinator, jobs, reports, budget ledger,
  fallback, and cancellation.
- `garuda/tools/collection.py`: public delegation and internal collection
  submission tools.

### New runtime package

```text
garuda/runtime/
|- protocol.py
|- registry.py
|- router.py
|- native.py
|- session.py
|- authority.py
|- diff.py
|- context_pack.py
|- handoff.py
`- acp/
   |- client.py
   |- process.py
   |- normalizer.py
   `- fake_agent.py
```

### Existing modules

- `garuda/agents/loader.py`: profile alias fields and collection narrowing.
- `garuda/agents/setup.py`: shared model, collection, runtime, and routing
  assembly.
- `garuda/core/loop.py`: terminal-tool strategy instead of one hard-coded
  terminal name.
- `garuda/core/run_state.py`: inject the collection coordinator and role-aware
  metrics.
- `garuda/core/subagent.py`: share safe child-trace and brief-handoff mechanics;
  do not absorb collection policy.
- `garuda/core/sessions.py`: versioned unified-session migration and runtime
  segments.
- `garuda/core/events.py`: normalized collection, routing, runtime, authority,
  and handoff events.
- `garuda/tools/protocol.py`: optional conservative tool-effect metadata and
  coordinator reference.
- `garuda/interfaces/runner.py`: compatibility facade over
  `NativeGarudaRuntime`.
- CLI, server, SDK, recipes, and dashboard: common model/runtime parameters and
  routing/handoff operations.
- observability and ATIF export: role-aware usage and runtime segments.
- evaluation: paired cost/quality comparisons and cross-runtime matrices.

New cross-entry-point wiring belongs in `agents/setup.py` or the new runtime
orchestrator, not copied into each interface.

## Implementation sequence

Each step should be a focused pull request with its own tests and documentation.

1. **Measurement and decisions**
   - Establish paired cost and quality baselines.
   - Record durable boundary and trust decisions.
   - Split initial routing from future automatic mid-run routing in the roadmap.
2. **Model specs and bindings**
   - Add trusted configuration, validation, factory, and compatibility mapping.
   - Wire identical resolution through every native entry point.
3. **Collection terminal contract**
   - Parameterize loop terminal handling.
   - Add request/report schemas and the collection completion gate.
4. **Collection coordinator**
   - Add safe tools, contexts, nested traces, workspace revisions, and compact
     parent reports.
5. **Collection budgets and fallback**
   - Add atomic reservations, concurrency, cancellation, role metrics, and
     mode-specific fallback.
6. **Runtime protocol and native bridge**
   - Implement `AgentRuntime`, registry contracts, unified sessions, and the
     native adapter while preserving old APIs.
7. **Context, diff, authority, and handoff**
   - Implement generated context schemas and the transactional switch state
     machine with fault injection.
8. **ACP client and fake conformance runtime**
   - Implement subprocess lifecycle, normalization, and deterministic fixtures.
9. **First external adapters and product controls**
   - Support two authenticated ACP harnesses and explicit handoff across CLI,
     SDK, server, and dashboard.
10. **Deterministic initial router**
    - Add trusted rules, repository traits, capability filters, explanation, and
      startup fallback.
11. **Optional classifier fallback**
    - Add one structured, tool-free classification call and post-validation.
12. **Evaluation and staged rollout**
    - Run paired ablations, adapter matrices, live opt-in checks, and update
      architecture, reference, guide, roadmap, backlog, and durable context docs.

The dual-model path can ship after step 5 without waiting for external adapters.
Cross-runtime routing depends on the runtime, session, authority, and handoff
foundation and must not bypass that order.

## Test plan

### Model binding and configuration

- Global, project, profile, environment, and explicit precedence.
- Project configuration cannot authorize endpoints, providers, commands, or
  higher ceilings.
- Legacy `--model` and `GARUDA_MODEL` behavior.
- Two LiteLLM providers in one run.
- Two programmatic SDK `Model` objects in one run.
- Missing, invalid, and non-tool-calling collection models.
- No collection configuration preserves current behavior.

### Collection unit and integration tests

- Reasoning model delegates, receives evidence, edits itself, and completes.
- Collection model uses its own context window and token counter.
- Brief and none handoffs contain only allowed state.
- Worker attempts to write, spawn, approve, complete, or delegate are denied.
- Unknown and side-effecting custom/MCP tools are unavailable.
- Lazy MCP dispatch re-checks the underlying tool effect.
- Malformed, oversized, unsupported, and inaccessible report evidence.
- Atomic budget behavior under concurrent jobs.
- Result ordering under concurrent completion.
- Provider governor interaction across model roles.
- Collection failure before and after partial reads.
- Exactly one fallback attempt with the same safety ceiling.
- Eval and rigorous modes do not fall back by default.
- Parent cancellation closes all workers.
- Workspace revision changes mark reports stale.
- Known background writers block or isolate strict collection.
- Parent context size is materially smaller than a full child transcript.
- Child traces and buffer links survive persistence and resume.

### Runtime, routing, and handoff tests

- Common lifecycle conformance for native and fake ACP runtimes.
- Malformed frames, hangs, exits, reconnects, cancellation, and cleanup.
- Session migration, atomicity, and future-version refusal.
- Runtime discovery performs no installation, login, or secret inspection.
- Dirty baseline, renames, deletions, untracked files, and large diffs.
- One active execution-authority generation under concurrency.
- Failure injection at every handoff transition.
- Cancellation before and after handoff commit.
- Target mutation before acknowledgement requires recovery.
- Workspace change during context compilation forces regeneration.
- Deterministic rule ordering and route explanations.
- Explicit runtime selection overrides rules and classifier.
- Project route trust behavior.
- Classifier invalid ID, malformed schema, timeout, low confidence, and
  capability mismatch.
- Initial-runtime startup fallback with unchanged and unexpectedly changed
  workspaces.
- End-to-end task across two fake runtimes.
- Historical trajectory and session readers remain compatible.
- Optional live adapter checks never run in default CI or consume quota without
  explicit opt-in.

### Repository checks

Run the narrowest affected tests first, followed by:

```bash
pytest -q
ruff check garuda tests
```

At milestone boundaries, drive documented CLI, SDK, JSON-RPC, and dashboard
flows end to end and inspect their trajectories. Unit coverage alone is not
sufficient for cross-entry-point wiring.

## Rollout

1. Land contracts and metrics with collection and runtime routing disabled.
2. Offer opt-in `--collection-model` while keeping the runtime native.
3. Offer opt-in deterministic routing and explicit fake-runtime handoffs.
4. Enable individually supported ACP adapters behind explicit configuration.
5. Offer the classifier as a separate opt-in after deterministic routing is
   stable.
6. Consider enabling collection by default only after paired cost and quality
   gates pass.

Native single-model execution remains the compatibility default throughout the
rollout. Project-controlled automatic provider or runtime selection remains off
unless trusted globally.

## Success metrics

The first paired evaluation for dual-model delegation targets:

- at least 20% lower median total cost on read-heavy tasks;
- at least 30% lower reasoning-model input tokens on read-heavy tasks;
- no more than a two-percentage-point completion-rate regression;
- no material decline in evidence quality or repository investigation;
- complete attribution of native model calls to a model role;
- zero collection-authorized workspace mutations;
- visible fallback and stale-report rates.

These are release decision thresholds, not architectural guarantees. If total
trajectory cost does not fall, collection remains opt-in while the measurements
are analyzed.

Cross-runtime routing and handoff targets:

- every supported adapter passes the full runtime conformance suite;
- every routing decision includes a persisted rationale;
- zero dual-owner mutations in concurrency and fault-injection tests;
- zero silent context loss in injected handoff failures;
- historical native sessions remain readable;
- unknown external cost is never reported as zero;
- at least two supported runtimes complete a real explicit handoff.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Cheap workers produce shallow summaries | Structured evidence, report validation, controller retry, and investigation-quality benchmarks. |
| Delegation costs more than direct reading | Whole-trajectory measurement, job-size guidance, and strict token/cost ceilings. |
| Controller over-delegates | Job caps, repetition detection, and visible per-job cost. |
| Worker reads changing files | Revision stamps, background-writer detection, and optional snapshots. |
| A project reroutes private code | Trusted global aliases, explicit project-routing authorization, and route previews. |
| Classifier is unstable or prompt-injected | Deterministic rules first, fixed candidates, structured output, confidence threshold, and policy revalidation. |
| External runtime cannot honor policy | Capability negotiation and refusal instead of silent downgrade. |
| Handoff loses or corrupts work | Source checkpoint, authoritative diff, context validation, acknowledgement, and rollback. |
| Two harnesses mutate one workspace | Revocable execution-authority generations and one session switch lock. |
| Core loop forks into special implementations | Extract one terminal strategy and reuse the existing loop. |

## Acceptance criteria

The dual-model feature is complete when a native run can use independently
configured reasoning and collection models from different providers, delegate a
bounded read-only job, receive a validated evidence report, attribute all usage,
apply the configured fallback, and complete with unchanged single-model behavior
when collection is absent.

The runtime-routing feature is complete when Garuda can choose an initial runtime
through explicit selection, a trusted rule, or a validated classifier fallback;
explain that decision; start the selected runtime under one workspace lease; and
explicitly hand a live task between two supported runtimes without dual mutation,
silent context loss, credential persistence, or loss of source-session recovery.
