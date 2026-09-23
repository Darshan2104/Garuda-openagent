# Garuda — Module Map

What each package owns and the file to open first. Read
[ARCHITECTURE.md](ARCHITECTURE.md) for how these fit together at run time.

The historical build-order plan (39 modules across 8 phases, all complete) is in
[archive/2026-07-17-MODULES-build-phases.md](archive/2026-07-17-MODULES-build-phases.md).
It is provenance, not a map.

## `garuda/types.py`

The shared vocabulary: `Message`, `ToolCall`, `ToolResult`, `ExecResult`,
`AgentConfig`, `AgentResult`, and the default system prompt. Everything imports
from here; it imports from nothing in the package. Keep it dependency-free — a
module-level import in `types.py` becomes a cycle everywhere.

## `core/` — the loop and its gates

`DefaultAgent` used to be one ~500-line `run()` doing setup, turns, and completion.
It is now four collaborators; read `loop.py` for control flow, the others to change
a behaviour.

| File | Owns |
|---|---|
| `loop.py` | `DefaultAgent`: the turn loop, and nothing else. Model call → tool step → repeat. Re-exports the constants callers import from here. |
| `run_state.py` | `prepare_run` (assembly: tool filtering, buffer, context bootstrap, subagent wiring, deadline) and `RunState` (what the loop reads and writes, plus result building). |
| `steering.py` | Every message the harness injects between turns: budget notices, the budget-review and final-turn nudges, repetition and failure-streak detection. Notes are *queued*, never appended mid-turn — see its docstring for why. |
| `tool_runner.py` | Executing one call or a concurrent read batch: permissions, hooks, output shaping/buffering, event ordering. `PARALLEL_SAFE_TOOLS` lives here and the fan-out is bounded by `AgentConfig.max_parallel_reads`. |
| `metrics.py` | Per-turn model latency, tool latency and wall-clock, compaction and checkpoint time, cache-hit rate. Rolls up onto `AgentResult.metadata["metrics"]` and emits one `turn_metrics` event per turn. |
| `completion.py` | The `task_complete` gate: acceptance contract, side-effect sweep, verification, and the yield-breaker that stops it livelocking. |
| `modes.py` | Run postures. The presets that map one `mode` onto a coherent gate set. |
| `rigorous.py` | `RigorousAgent`: plan → execute → critic, with repair rounds. `create_agent()` picks between this and `DefaultAgent`. |
| `verifier.py` | The completion gate. Decides whether `task_complete` is accepted. |
| `evidence.py` | Two independent classifications of a shell command: whether it can actually fail (a check that cannot fail is not proof), and whether it writes anything (`is_side_effect_free`, which gates concurrency at the gate). Close to opposites in practice — the commands that prove the most are the ones that write. |
| `contract.py` | Acceptance criteria derived from the task statement, pinned across compaction. |
| `side_effects.py` | Sweeps agent-started background processes before verification. Tracks each launch by its process group — the command text is a fallback, since a process can rename itself — and re-probes after every signal so absence is confirmed rather than assumed. |
| `permissions.py` | `PermissionEngine`: allow/deny/ask per tool, path, and command. Guardrails, not confinement. |
| `sessions.py` | On-disk session store; `meta.json` writes are locked and atomic. |
| `events.py` | Append-only JSONL event log, crash-safe. |
| `buffer.py` | Session buffers holding large tool output and compacted history. |
| `bootstrap.py` | One-shot environment probe folded into the first-turn prompt. |
| `subagent.py` | Forked child runs. |
| `action_memo.py` | Session memory of what has already been asked, so a repeated read is answered rather than re-run. Filesystem reads stop being memoized while a background task is live — it writes between calls, and no call marks that. `bash_background` reports its own exits so caching resumes; a raw `cmd &` reports nothing, so it suspends caching for the session. |

## `tools/` — what the agent can do

`protocol.py` defines the contract (`ToolContext` in, `ToolResult` out);
`registry.py` + `__init__.py::build_toolkit` assemble the set a profile asks for.

Execution: `bash.py` (a command that backgrounds something also records the
process group it leaves behind, so the pre-completion sweep has an exact handle
on it), `background.py`, `tmux.py`.
Files: `files.py`, `edit.py`, `multi_edit.py`, `search.py` (ripgrep-backed),
`diagnostics.py` (post-edit syntax + lint).
Reading: `documents.py`, `image_read.py`.
Network: `web.py` — SSRF guard, redirect re-validation, and pinned-IP connect.
Agent-facing state: `todo.py`, `goal.py`, `contract.py`, `task_complete.py`.
Large output: `buffer_tools.py`. Discovery: `discovery.py` (token-lean MCP),
`project_loader.py` (opt-in `.agent/tools/*.py`). Delegation: `subagent.py`.

## `workspace/` — where commands run

`protocol.py` is the `Environment` interface every tool talks to. Implementations:
`local.py`, `docker.py`, `remote.py`, `tmux.py` (persistent panes with marker
polling), selected by `factory.py`.

`sandbox.py` / `sandbox_policy.py` build the OS sandbox (bubblewrap on Linux,
Seatbelt on macOS) — read the `sandbox_policy.py` docstring before touching it, it
records which confinement actually holds. `shell.py` is the opt-in persistent
shell; `paths.py` and `health.py` are path safety and liveness.

## `context/` — fitting the conversation in the window

`manager.py` holds history and decides when to act. `shaper.py` caps and shapes
tool output, `condenser.py` compacts (`microcompact` by default) and demotes
pruned history into buffers, `summarizer.py` produces the summaries.
`schemas.py` validates the generated `current-task.md`/`handoff.md` files
(versioned frontmatter, bounded fields, unknown fields round-trip, unknown
versions rejected).

## `agents/` — profiles

`setup.py::prepare_agent_run` is the shared chokepoint for every entry point.
`loader.py` reads YAML profiles (and records which fields were declared, so mode
presets don't override authored intent); `md_loader.py` + `frontmatter.py` read
OpenCode-style `agent.md`. Built-ins in `defaults/`: `build`, `plan`, `explore`,
`harbor`, `reviewer`.

## `model/` — provider access

`protocol.py` is the `Model` interface. `litellm_model.py` is the real
implementation (streaming, tool calls, reasoning effort, prompt caching, retries);
`governor.py` caps per-provider concurrency; `script_model.py` is the deterministic
test double — prefer it over mocks.

## `runtime/` — harness boundary

`protocol.py` is the `AgentRuntime` interface every harness implements (native or
external — never place a harness behind `Model`). `events.py` is the normalized
event vocabulary with session/turn correlation. `fake.py` is the deterministic
test double with scripted scenarios; every adapter must pass the shared suite in
`tests/test_runtime_conformance.py`. `registry.py` resolves trusted global
harness manifests (the only place that authorizes an executable) plus
project-level aliases that reference and narrow but never self-authorize; one
instance per job, never process-global. `session.py` is the unified session
schema (runtime segments, baseline, handoff, cursors) plus legacy migration;
`SessionStore` publishes through the atomic locked meta path, so a failed
migration or write leaves the original readable. `native.py` adapts the native
loop behind the boundary without changing task semantics — imported directly,
never re-exported, so the product boundary pulls no `core` imports into
`garuda.runtime` itself.

## `interfaces/` — entry points

`main.py` (CLI argument surface), `headless.py` (`garuda run`), `cli.py` + `tui.py`
(interactive chat), `server.py` + `jobs.py` (job-queue server: submit/status/
events/result/cancel), `session.py` (multi-turn state shared by CLI and SDK),
`runner.py` (assembles a run and owns workspace teardown).

## `eval/` — measurement, outside the agent

`harbor_adapter.py` (Harbor benchmark integration; pins `mode="eval"`),
`harbor_environment.py`, `ablation.py` (per-variant config matrix),
`atif_export.py`, `dashboard.py`, `costs.py` (four-tier cost resolution),
`pricing.py` (the versioned price snapshot `costs.py` resolves against, so a
reported cost does not move when an upstream table does).

## Everything else

| Package | Owns |
|---|---|
| `config/` | `agent_home.py` — all `.agent/` discovery. `recipes.py` — YAML multi-step workflows. |
| `mcp/` | `config.py` (merge + allowlist), `client.py` (per-run server manager). |
| `skills/` | `loader.py` — progressive disclosure, `allowed-tools` validation. |
| `sdk/` | `software_agent.py`, `conversation.py` — the library surface. |
| `interfaces/web/` | The `garuda web` dashboard: read past runs, and talk to an agent. `security.py` — Host/Origin/token gate (its own, because `interfaces/server.py` blanket-refuses browsers). `http.py` — threaded stdlib server + static. `routes.py` — pure `dispatch`, so routes test without a socket. `reads.py` — the read model over `SessionStore` + the trajectory reader, plus the `ReaderCache` whose lock spans `refresh()` (a shared reader appends its tail twice otherwise). `tail.py` — byte-offset tailing; the offset only advances past the last newline, because `EventStore.append` is not atomic and consuming a torn line desyncs the cursor permanently. `live.py` — conversations: the workspace allowlist, the permission ceiling, the turn lifecycle and cancellation. `grounding.py` — an uploaded file or fetched page becomes a workspace file the agent reads with its own tools; URLs go through `tools/web.py`'s SSRF-vetted fetcher, never a second one. `approvals.py` — parked approvals; the only thread-and-loop code here, so read its docstring before touching it. `static/` — no-build frontend; `views_trajectory.js` is the trace view, where a turn renders as in/thinking/says/does, tool colour carries the *family* rather than the outcome, and the turn timeline and gate lane are one CSS grid so alignment cannot drift. Checked by executing it in Chrome — see `tests/browser/`. |
| `observability/` | `trajectory.py` — rebuilds turn structure from an `events.jsonl` (one reader for local sessions and Harbor trials alike); `tracing.py` — spans. |
| `plugins/` | `hooks.py` — lifecycle hooks. |

## Working on it

```bash
pip install -e ".[dev]"
pytest tests/ -v
ruff check garuda/

garuda run -t "List files in the current directory"          # interactive posture
garuda run -t "..." --mode eval --model openrouter/deepseek/deepseek-v4-flash-0731   # full gate stack
```
