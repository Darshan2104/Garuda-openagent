# Garuda — Module Work Breakdown

Build order: **bottom-up**. Each module has a status, dependencies, and exit criteria.

| Status | Meaning |
|--------|---------|
| ✅ | Done |
| 🚧 | In progress |
| ⬜ | Not started |

---

## Dependency Graph

```
types ──┬── model ──┬── core/loop ─── interfaces/cli
        │           │
workspace ──┬── tools ──┘
            │
        core/events
            │
        context (Phase 2)
        permissions (Phase 2)
        verifier (Phase 2)
            │
        agents/loader (Phase 2)
            │
        workspace/docker, tmux (Phase 3)
        tools/mcp (Phase 4)
            │
        eval/harbor (Phase 5)
```

---

## Phase 1 — Foundation (MVP)

| # | Module | Path | Status | Depends On | Exit Criteria |
|---|--------|------|--------|------------|---------------|
| M1 | **Types** | `garuda/types.py` | ✅ | — | Message, ToolCall, AgentConfig dataclasses |
| M2 | **Model protocol** | `garuda/model/` | ✅ | M1 | `Model` protocol + `LitellmModel` + `ScriptModel` (tests) |
| M3 | **Environment** | `garuda/workspace/local.py` | ✅ | M1 | bash exec, read/write files in workspace |
| M4 | **Tools** | `garuda/tools/` | ✅ | M1, M3 | `bash`, `read_file`, `write_file` |
| M5 | **Event store** | `garuda/core/events.py` | ✅ | M1 | Append-only JSONL log |
| M6 | **Agent loop** | `garuda/core/loop.py` | ✅ | M2–M5 | `DefaultAgent` runs multi-turn with tools |
| M7 | **Headless CLI** | `garuda/interfaces/headless.py` | ✅ | M6 | `garuda run -t "task"` works |

**Phase 1 exit:** Run a local task end-to-end with any LiteLLM model (or `ScriptModel` in tests).

---

## Phase 2 — Reliability

| # | Module | Path | Status | Depends On | Exit Criteria |
|---|--------|------|--------|------------|---------------|
| M8 | **Context manager** | `garuda/context/` | ✅ | M2, M6 | Output caps, head/tail shaping |
| M9 | **Permissions** | `garuda/core/permissions.py` | ✅ | M4 | allow/deny/ask per tool/command |
| M10 | **Completion verifier** | `garuda/core/verifier.py` | ✅ | M2, M6 | Checklist gate on `task_complete` |
| M11 | **Patch tool** | `garuda/tools/patch.py` | ✅ | M3 | Unified diff apply |
| M12 | **Agent profiles** | `garuda/agents/` | ✅ | M6, M9 | Load `build`/`plan`/`explore` from YAML |
| M13 | **Interactive CLI** | `garuda/interfaces/cli.py` | ✅ | M6, M9 | TUI with permission prompts |

**Phase 2 exit:** Multi-turn task with permissions and completion verification.

---

## Phase 3 — Terminal Realism

| # | Module | Path | Status | Depends On | Exit Criteria |
|---|--------|------|--------|------------|---------------|
| M14 | **Tmux environment** | `garuda/workspace/tmux.py` | ✅ | M3 | Persistent tmux session |
| M15 | **Tmux tools** | `garuda/tools/tmux.py` | ✅ | M14 | `tmux_exec`, `tmux_capture` |
| M16 | **Marker polling** | `garuda/workspace/tmux.py` | ✅ | M14 | `__CMDEND__` early completion |
| M17 | **Summarizer** | `garuda/context/summarizer.py` | ✅ | M8 | 3-step proactive summarization |
| M18 | **Image read** | `garuda/tools/image_read.py` | ✅ | M2 | Multimodal file analysis |
| M19 | **Docker workspace** | `garuda/workspace/docker.py` | ✅ | M3 | Container-isolated execution |

**Phase 3 exit:** Interactive terminal task (pager, server, menu) via tmux.

---

## Phase 4 — Extensibility

| # | Module | Path | Status | Depends On | Exit Criteria |
|---|--------|------|--------|------------|---------------|
| M20 | **MCP client** | `garuda/mcp/` | ✅ | M4 | Connect stdio MCP servers |
| M21 | **Plugin hooks** | `garuda/plugins/hooks.py` | ✅ | M6 | before/after tool lifecycle |
| M22 | **Subagent handoff** | `garuda/core/subagent.py` | ✅ | M8, M12 | Fork context, return summary |
| M23 | **Task complete tool** | `garuda/tools/task_complete.py` | ✅ | M10 | Triggers verifier |

**Phase 4 exit:** Custom YAML agent profile + MCP tool in Docker.

---

## Phase 5 — Evaluation

| # | Module | Path | Status | Depends On | Exit Criteria |
|---|--------|------|--------|------------|---------------|
| M24 | **ATIF export** | `garuda/eval/atif_export.py` | ✅ | M5 | EventStore → ATIF JSON |
| M25 | **Harbor adapter** | `garuda/eval/harbor_adapter.py` | ✅ | M6, M19 | `BaseAgent` implementation |
| M26 | **TB benchmarks** | `garuda/eval/benchmarks/` | ✅ | M25 | Harbor run configs |
| M27 | **Spreadsheet eval** | `garuda/eval/benchmarks/spreadsheet/` | ✅ | M25 | SpreadsheetBench adapter (eval only) |
| M28 | **PDF eval** | `garuda/eval/benchmarks/pdf/` | ✅ | M25 | OfficeQA adapter (eval only) |

**Phase 5 exit:** Score on Terminal-Bench 2.0 via Harbor with ATIF logs.

---

## Phase 6 — Production (v1.5+)

| # | Module | Path | Status | Depends On |
|---|--------|------|--------|------------|
| M29 | Recipes | `garuda/config/recipes.py` | ✅ | M12 |
| M30 | RigorousAgent | `garuda/core/rigorous.py` | ✅ | M10, M17 |
| M31 | IDE server | `garuda/interfaces/server.py` | ✅ | M6 |
| M32 | OS sandbox | `garuda/workspace/sandbox.py` | ✅ | M3 |
| M33 | Remote workspace | `garuda/workspace/remote.py` | ✅ | M19 |

---

## Phase 7 — Edit/Search Reliability (grok-build-informed)

Capability upgrades from a review of xAI's `grok-build` harness, targeting wasted turns on
mechanical edit/search friction. See ENGINEERING_PLAN.md status update 22.

| # | Module | Path | Status | Depends On | Exit Criteria |
|---|--------|------|--------|------------|---------------|
| M34 | **Anchored edits** | `garuda/tools/edit.py` | ✅ | M11 | `resolve_edit` layered matcher: line-number-prefix / CRLF / indentation recovery, unique-match-only |
| M35 | **Multi-edit tool** | `garuda/tools/multi_edit.py` | ✅ | M34 | `multi_edit` applies N atomic, sequential edits to one file |
| M36 | **Ripgrep-backed grep** | `garuda/tools/search.py` | ✅ | M4 | prefer `rg` (gitignore-aware) with `grep -E` fallback; `no_ignore` escape hatch |
| M37 | **Semantic post-edit lint** | `garuda/tools/diagnostics.py` | ✅ | M11 | `check_lint` (ruff, undefined-name class) + unified `post_edit_report` |

**Phase 7 exit:** edit/search friction reduced; 504 tests passing, ruff-clean.

---

## Phase 8 — Token Efficiency & Context Management (grok-build-informed)

Reduce token usage and keep long tasks on-track. See ENGINEERING_PLAN.md status update 23.

| # | Module | Path | Status | Depends On | Exit Criteria |
|---|--------|------|--------|------------|---------------|
| M38 | **Lazy tool discovery** | `garuda/tools/discovery.py` | ✅ | M20 | `search_tool`/`use_tool` replace raw MCP schemas above `GARUDA_MCP_MAX_DIRECT_TOOLS` (default 10) |
| M39 | **Goal orchestration + state re-pinning** | `garuda/tools/goal.py` | ✅ | M8 | `update_goal` tool; loop re-pins goal + todos after compaction so they survive summarization |

**Phase 8 exit:** leaner prompts with many MCP tools; goal/todos persist across compaction; 524 tests passing, ruff-clean.

---

## Current Sprint

**Completed:** Phase 1 (M1–M7), Phase 2 (M8–M13), Phase 3 (M14–M19), Phase 4 (M20–M23), Phase 5 (M24–M28), Phase 6 (M29–M33), Phase 7 (M34–M37), Phase 8 (M38–M39)

**Status:** All 39 modules complete — Garuda v1.1.0

---

## How to Work Module-by-Module

1. Pick the next ⬜ module whose dependencies are ✅
2. Implement module + unit tests in `tests/`
3. Update status in this file
4. PR per phase (or per module for large phases)

```bash
# Install
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run agent (needs API key)
garuda run -t "List files in the current directory"

# Run with explicit model
garuda run -t "..." --model openai/gpt-4o-mini
```
