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
| `tool_runner.py` | Executing one call or a concurrent read batch: permissions, hooks, output shaping/buffering, event ordering. |
| `completion.py` | The `task_complete` gate: acceptance contract, side-effect sweep, verification, and the yield-breaker that stops it livelocking. |
| `modes.py` | Run postures. The presets that map one `mode` onto a coherent gate set. |
| `rigorous.py` | `RigorousAgent`: plan → execute → critic, with repair rounds. `create_agent()` picks between this and `DefaultAgent`. |
| `verifier.py` | The completion gate. Decides whether `task_complete` is accepted. |
| `evidence.py` | Whether verification commands can actually fail — a check that cannot fail is not proof. |
| `contract.py` | Acceptance criteria derived from the task statement, pinned across compaction. |
| `side_effects.py` | Sweeps agent-started background processes before verification. |
| `permissions.py` | `PermissionEngine`: allow/deny/ask per tool, path, and command. Guardrails, not confinement. |
| `sessions.py` | On-disk session store; `meta.json` writes are locked and atomic. |
| `events.py` | Append-only JSONL event log, crash-safe. |
| `buffer.py` | Session buffers holding large tool output and compacted history. |
| `bootstrap.py` | One-shot environment probe folded into the first-turn prompt. |
| `subagent.py` | Forked child runs. |
| `action_memo.py` | Session memory of what has already been asked, so a repeated read is answered rather than re-run. |

## `tools/` — what the agent can do

`protocol.py` defines the contract (`ToolContext` in, `ToolResult` out);
`registry.py` + `__init__.py::build_toolkit` assemble the set a profile asks for.

Execution: `bash.py`, `background.py`, `tmux.py`.
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

## `interfaces/` — entry points

`main.py` (CLI argument surface), `headless.py` (`garuda run`), `cli.py` + `tui.py`
(interactive chat), `server.py` + `jobs.py` (job-queue server: submit/status/
events/result/cancel), `session.py` (multi-turn state shared by CLI and SDK),
`runner.py` (assembles a run and owns workspace teardown).

## `eval/` — measurement, outside the agent

`harbor_adapter.py` (Harbor benchmark integration; pins `mode="eval"`),
`harbor_environment.py`, `ablation.py` (per-variant config matrix),
`atif_export.py`, `dashboard.py`, `costs.py`.

## Everything else

| Package | Owns |
|---|---|
| `config/` | `agent_home.py` — all `.agent/` discovery. `recipes.py` — YAML multi-step workflows. |
| `mcp/` | `config.py` (merge + allowlist), `client.py` (per-run server manager). |
| `skills/` | `loader.py` — progressive disclosure, `allowed-tools` validation. |
| `sdk/` | `software_agent.py`, `conversation.py` — the library surface. |
| `observability/` | `tracing.py` — spans. |
| `plugins/` | `hooks.py` — lifecycle hooks. |

## Working on it

```bash
pip install -e ".[dev]"
pytest tests/ -v
ruff check garuda/

garuda run -t "List files in the current directory"          # interactive posture
garuda run -t "..." --mode eval --model openai/gpt-4o-mini   # full gate stack
```
