# Garuda — Architecture

Garuda is a coding-agent harness: it puts a model in a loop with tools, an
environment, and a completion gate, and runs a task to done. This document is the
orientation for someone about to change the code. For the file-by-file map see
[MODULES.md](MODULES.md); for what is knowingly unfinished see
[BACKLOG.md](BACKLOG.md).

Historical design documents and closed review ledgers live in
[archive](archive/index.md). They are kept for provenance and are **not** maintained —
do not read them as current behavior.

## The two things to understand first

**1. One selector decides run posture.** `AgentConfig` has dozens of independent
switches, which is right for ablation and wrong as an interface. `mode` implies a
coherent set of them (`garuda/core/modes.py`):

| Mode | Gates | Use |
|---|---|---|
| `interactive` (default) | none that cost a model call | day-to-day runs |
| `eval` | LLM judge, acceptance contract, discriminating + stable evidence, side-effect sweep | graded benchmark runs |
| `rigorous` | `eval` gates plus a plan → execute → critic agent | maximum scrutiny |
| `readonly` | `interactive` gates, permissions forced read-only — over the profile too, and `bash` limited to screened inspection commands | inspection |

`standard` is a back-compat alias for `interactive`. The default is deliberately
cheap: a bare `garuda run` pays for the work and the local checks, nothing else.
`eval` costs roughly two extra model calls per completion attempt plus a re-run of
each verification command — that is the posture benchmark numbers come from, and
it is opt-in.

Precedence, widest to narrowest: `AgentConfig` defaults → mode preset → fields the
profile YAML declared explicitly → explicit CLI flags.

**2. Trust boundaries are not all equal.** Three mechanisms limit what a run can
do, and only one is a real boundary:

| Mechanism | What it is |
|---|---|
| Docker workspace | **A confinement boundary.** The real answer for untrusted work. |
| macOS Seatbelt | A blast-radius reducer. Writes are confined to the workspace; **reads are not** — Seatbelt has no working allow-then-deny-subpath override for `file-read*`, so a sandboxed command can read host files. Documented in `workspace/sandbox_policy.py` and surfaced at runtime. |
| Permission rules | Guardrails. Path and command rules screen a call's *arguments*, never its results, so a broad read can still surface denied content. Deny regexes lose to shell expansion and indirection. |

If a change depends on one of these holding, check which one you actually have.

## The run path

A task flows through these in order. Following this list top to bottom is the
fastest way to learn the system.

1. **Entry** — `interfaces/main.py` (CLI), `interfaces/server.py` + `jobs.py`
   (job-queue server), `sdk/software_agent.py` (library). Task-running entries
   resolve a task, a model, and a workspace. The `eval dual-model report` CLI
   is deliberately different: it is a read-only consumer of completed native
   session evidence and never constructs a model or provider client.
2. **Profile + posture** — `agents/setup.py::prepare_agent_run` is the shared
   chokepoint: it loads the profile (`agents/loader.py`), turns it into an
   `AgentConfig`, applies the mode preset, builds the toolkit and the permission
   engine, and resolves the reasoning and optional collection clients. New wiring
   belongs here, not in each entry point.
3. **Environment** — `workspace/factory.py` picks local, Docker, remote, or tmux.
   Everything the agent runs goes through the `Environment` protocol
   (`workspace/protocol.py`), which is what makes the same loop work in a
   container and on the host.
4. **Loop** — `core/loop.py::DefaultAgent.run` drives turns: call the model,
   execute tool calls, feed results back. It delegates the rest to four
   collaborators assembled by `core/run_state.py::prepare_run` — `run_state.py`
   (setup and carried state), `steering.py` (what the harness tells the model
   between turns), `tool_runner.py` (executing calls), `completion.py` (the gate).
   `core/rigorous.py` wraps the whole thing with plan/execute/critic when the mode
   asks for it.
   An enabled collection client does not own this loop: the reasoning model must
   call `delegate_collection`. `prepare_run` then constructs a bounded
   `CollectionCoordinator` from the live environment, parent context, buffer,
   permissions, events, and deadline. Its child reuses `DefaultAgent` with a
   separate model context, read-only toolkit, sibling trace, and
   `submit_collection` terminal strategy; that terminal can never complete the
   parent run.
   Native sequential and parallel flow activation consumes the role’s admitted
   immutable AgentSpec through the shared role source selector, retaining source
   identity across named definition changes and snapshot workspace execution.
   Existing flow lease, no-edits, model and completion owners remain in control.
   Native consult quiescence normalizes ordinary synchronous/asynchronous
   failures to a typed snapshot refusal before capture; existing predispatch
   settlement refunds admission and releases that reservation. Typed refusals,
   cancellation and unknown-live-child retention keep their existing owners.
   Native consult receipts count structured permission refusals and explicit
   file-access denial results, not error prose. Tool results persist only the
   denial boolean from metadata, and denied file accesses bypass memo storage
   so each attempt retains its own evidence.
5. **Tools** — `tools/` registered through `tools/registry.py` and assembled by
   `build_toolkit`. Every tool takes a `ToolContext` and returns a `ToolResult`.
6. **Context** — `context/manager.py` holds the conversation; `shaper.py` caps
   output, `condenser.py` compacts when the window fills, `summarizer.py` produces
   the summaries. Compacted history is archived to session buffers
   (`core/buffer.py`), not destroyed.
7. **Completion gate** — `task_complete` does not end a run by itself.
   `core/verifier.py` decides, consulting `core/evidence.py`,
   `core/contract.py`, and `core/side_effects.py` per the mode's gates. A
   rejection returns to the loop with what was not shown.
8. **Record** — `core/events.py` appends an event log; `core/sessions.py`
   persists `meta.json` / `messages.json` / `events.jsonl` so a killed run is
   resumable. `observability/tracing.py` emits spans.

## Where to change what

| Goal | Start here |
|---|---|
| Add a tool | `tools/`, register in `tools/__init__.py`, add to profile YAML |
| Change what a profile can do | `garuda/agents/defaults/*.yaml` |
| Change gate posture | `core/modes.py` (a preset), not the individual booleans |
| Add a config field | `types.py`, then decide whether a mode should own it |
| Support a new environment | implement `workspace/protocol.py`, register in `factory.py` |
| Change eval behavior | `eval/harbor_adapter.py` — note it pins `mode="eval"` on purpose |
| Wire something for every entry point | `agents/setup.py` |

Setup resolves unsupported definitions for structural field names/provenance
only. It serves fixed diagnostic codes/messages and omits values and prompt
measurements; unsupported definitions are never activated for inspection.

Native role binding retains the immutable AgentSpec it resolved after fallback.
Shared role selection returns that snapshot to CLI execution and consult
narrowing; conflicting explicit agent files refuse before activation. The spec
is runtime-only, excluded from role records/repr. Consult tool ceilings remain.

Native role admission compares an explicitly selected model id with the
agent’s declared/inherited binding through the trusted global alias table.
Contradictory reasoning model ids or missing aliases refuse before activation;
ordinary model-factory precedence and ACP native-only refusals are unchanged.

ACP role projection reads structural instruction replacement intent from the
shared agent resolver. Explicit/inherited replacement refuses before launch,
regardless of text equality with the native base. Native assembly stays unchanged;
ACP accepts appended instructions only as labelled task context.

## Conventions

- **`.agent/` is the project home.** Custom tools, MCP servers, skills, agent
  profiles, and `AGENTS.md` memory are discovered there (`.garuda/` still works).
  All discovery goes through `config/agent_home.py` — do not re-derive paths.
- **Fail closed on anything security-shaped.** An unresolvable host, an
  unparseable address, an unclear verifier verdict: refuse. Several existing
  comments explain a specific fail-closed choice; keep that habit.
- **Agent HTTP diagnostics exclude source text.** Setup projects definition and
  prompt-inspection failures to fixed messages and registry-validated codes.
  Legacy warnings also use source-free messages. Detailed errors/warnings remain
  available through authorized local agent inspection.
- **Prompt attribution follows recorded executions.** Native run preparation
  snapshots the compiled agent identity and a fresh execution id; the actual
  outbound system-message measurement includes that binding. Dashboard grouping
  uses recorded bindings, including inner runs, and leaves historical unbound
  measurements unattributed. Current session metadata is not historical evidence.
  ACP adapters similarly record a sending execution id and the optional bound
  role definition passed through the common catalog factories. ACP measurements
  cover attempted host request text, while the external internal system prompt
  remains unknown. Both kinds use source-free metadata; their execution ids do
  not replace runtime handoff segments. Shared observability helpers snapshot the
  active neutral tenure only when it matches the sender, then validate the full
  recorded index/reference during projection. Missing or inconsistent references
  stay unassociated; a tenure without measurements has unknown prompt provenance.
  This metadata is observational evidence, not launch or ownership authority.
- **Session names do not confer ownership.** Shared capacity reservations are
  bound to process identity and epoch. Reusing a holder id cannot replace a
  live or unknown owner; exact live-owner retries are idempotent. Ordinary
  slots remain allocated after parent death until explicit matched release.
  Queue-bound slots stay protected until the coordinator durably releases them.
  Workspace lease acquisition also refuses an existing session id. Mutation
  requires the issuing store instance/process and complete retained owner,
  workspace and mode, not a copied epoch or an inspected record. Unknown creator
  identity refuses acquisition; forked and ambiguous handles refuse mutation.
  Ordinary workspace holders remain recorded after parent death/expiry without
  descendant-cleanup receipts. Read-only registration preserves those bindings;
  expired mutating holders continue to block new mutation. Recovery-facing
  inspection also includes expired/dead-parent holders, so session and project-key
  recovery refuse before changes while descendant cleanup is unproved. Borrowed capability
  creation/use revalidates the parent's complete issuing-store authority under
  the lease lock without renewal; copied flags cannot authorize released,
  missing, changed, unknown or fork-inherited parent bindings.
- **Quarantine retains ownership through teardown.** The shared run guard
  stops renewal but retains its workspace lease and runtime reservation. Later
  release/finalizer calls cannot undo quarantine. A borrowed flow step also
  quarantines its parent and retains its own slot; revocation gives no cleanup
  authority. Ordinary reaped completion still releases normally. Persisted
  descendant supervision and recovery receipts remain separate work.
- **Queue ids bind admitted work.** Enqueue retries match the exact scope,
  user, harness, session and configuration digest of a waiting entry or claim.
  Exact retries preserve the durable sequence; conflicts refuse under the
  queue lock before any write.
  Dispatch-ready entries also match the scope formed from their declared user
  and harness. Enqueue and ticket creation both enforce this, so a scope alias
  cannot create another FIFO lane for the same pair. Preexisting mismatches
  refuse selection without rebinding or reserving capacity.
  Direct dispatch also checks full committed adoption; a failed release intent
  cannot resurrect an old claimant handle. Ordinary capacity activation checks
  adoption under its lock and after publication before granting launch authority,
  retaining the protected slot if adoption was revoked during publication.
  Ordinary reservations also remain counted after owner death: their records
  lack descendant cleanup evidence. Neither foreground callers nor queue
  selection delete them to admit replacement work. Explicit matched releases
  remain coordinator cleanup assertions; receipt-based recovery is separate work.
  Background admission resolves runtime aliases through the shared trusted
  catalog and queues under the canonical runtime id, retaining the original
  launch reference for downstream capability resolution. Shared setup resolves
  effective roles before admission and preserves default roles explicitly in
  launch arguments. Workers recheck the effective garuda.yaml digest before
  selection; background dynamic role fallback refuses until its runtime
  decision can be persisted. Referenced runtime/agent/MCP files and later concurrent
  configuration changes are outside this check. A worker verifies the lane,
  session and serialized launch digest before publishing identity or selecting
  work; alias retargeting or changed launch arguments refuse without rebinding.
- **Queue mutations require claimant authority.** Heartbeat, release and
  workspace requeue compare the complete persisted process identity and epoch.
  Implicit mutations use only the claiming instance's retained owner in the
  claiming process; opening a store or forking does not grant that authority.
- **Queue inspection is read-only from construction onward.** `entries()` and
  `snapshot()` read the atomically published document without a writer lock,
  initialization, permission changes, migration publication or backup files.
  Historical records are projected in memory; unsupported records refuse.
- **Queue selection precedes activation.** Version 3 journals queue intent,
  protected shared-capacity reservation and selection commit. Only the successful
  claiming process can activate a fully bound ticket, after publishing activation
  in capacity version 2. Release fences captured adoption tickets in capacity
  before publishing claim removal, then removes the slot. The fence retains
  whether the reservation was activated.
  Dead owners permit reconciliation before activation; activated or ambiguous
  dispatch remains quarantined and is never automatically replayed.
- **Legacy queue work cannot supply admission authority.** Version 1 jobs lack
  user/session/configuration bindings and refuse mutation, even for dead owners.
  Empty records and fully bound, ordered version 2 waiters migrate after an
  exact durable source backup. Version 2 claims lack activation evidence and
  refuse migration. Nonempty capacity version 1 records lack reservation origin
  evidence and also refuse mutation; empty ones archive before upgrading.
  Ambiguous backups remain intact and refuse. Legacy queue ceilings are discarded.
- **Comments explain *why*, especially the non-obvious.** The codebase leans on
  this heavily — a fix whose reason isn't recorded gets re-broken.
- **Tests are per mechanism.** `tests/` mirrors the module under test; live
  Docker and Seatbelt checks are opt-in so CI stays deterministic.
