# Garuda — Architecture

Garuda is a coding-agent harness: it puts a model in a loop with tools, an
environment, and a completion gate, and runs a task to done. This document is the
orientation for someone about to change the code. For the file-by-file map see
[MODULES.md](MODULES.md); for what is knowingly unfinished see
[BACKLOG.md](BACKLOG.md).

Historical design documents and closed review ledgers live in
[archive/](archive/). They are kept for provenance and are **not** maintained —
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
| `readonly` | `interactive` gates, permissions forced read-only | inspection |

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
   (job-queue server), `sdk/software_agent.py` (library). Each resolves a task,
   a model, and a workspace.
2. **Profile + posture** — `agents/setup.py::prepare_agent_run` is the shared
   chokepoint: it loads the profile (`agents/loader.py`), turns it into an
   `AgentConfig`, applies the mode preset, builds the toolkit and the permission
   engine. New wiring belongs here, not in each entry point.
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
- **Comments explain *why*, especially the non-obvious.** The codebase leans on
  this heavily — a fix whose reason isn't recorded gets re-broken.
- **Tests are per mechanism.** `tests/` mirrors the module under test; live
  Docker and Seatbelt checks are opt-in so CI stays deterministic.
