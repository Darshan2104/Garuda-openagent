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

## Conventions

- **`.agent/` is the project home.** Custom tools, MCP servers, skills, agent
  profiles, and `AGENTS.md` memory are discovered there (`.garuda/` still works).
  All discovery goes through `config/agent_home.py` — do not re-derive paths.
- **Fail closed on anything security-shaped.** An unresolvable host, an
  unparseable address, an unclear verifier verdict: refuse. Several existing
  comments explain a specific fail-closed choice; keep that habit.
- **Session names do not confer ownership.** Shared capacity reservations are
  bound to process identity and epoch. Reusing a holder id cannot replace a
  live or unknown owner; exact-owner retries are idempotent, and a different
  owner can take over ordinary slots only after confirmed death. Queue-bound
  slots stay protected until the queue coordinator durably releases them.
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
  launch reference for downstream capability resolution. A worker verifies the lane,
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
