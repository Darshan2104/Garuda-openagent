# Config & Scalability Plan — `.agent/` home + P1 triad

**Goal.** Make Garuda fully configurable per project — *"drop custom tools, MCP servers, and
skills into one folder and the agent uses them"* — and close the three P1 scalability gaps
(global tool registry, one-shot server, no model-concurrency governor) so multiple heterogeneous
runs can share one process safely.

**Guiding constraint.** Everything the agent uses is driven by config/convention, not code
changes. No global mutable state on the run path. Existing single-run behavior and all 380 tests
stay green (back-compat shims where needed).

---

## 0. Current state (so we don't rebuild what exists)

| Asset | Today | Verdict |
|-------|-------|---------|
| **Skills** | `garuda/skills/loader.py` (progressive disclosure); profile `skills`/`skills_dirs`; dirs `.garuda/skills`, `skills`, `.skills` | **80% done** — needs `.agent/` convention + `allowed-tools` enforcement |
| **MCP servers** | `mcp/config.py`: explicit path, `.garuda/mcp.*`, `.cursor/mcp.json`, global, opt-in merge; per-run `McpClientManager` | **80% done** — needs `.agent/` convention + default layering + per-profile server allowlist |
| **Custom tools** | Only `SoftwareAgent.register_tool()` → mutates process-global `_REGISTRY` | **Gap** — no file-based tools; global state |
| **Tool registry** | `tools/registry.py` module-global `_REGISTRY` | **P1 #1** — de-globalize |
| **Server** | `interfaces/server.py` `_run` blocking, returns full event list | **P1 #2** — job queue |
| **Model concurrency** | `litellm_model.py` retry only, no limiter | **P1 #3** — governor |

---

## 1. The unifying idea: the `.agent/` project home

One conventional directory at the workspace root holds everything, discovered automatically:

```
.agent/
  agents/            # profiles: build.yaml, myagent.md, myagent/agent.md
  skills/            # SKILL.md files (flat or one folder each)
  tools/             # *.py modules exporting custom Garuda tools
  mcp.json           # (or mcp.yaml) MCP server definitions
  settings.yaml      # optional: default agent, model, concurrency, trust flags
```

- **`.agent/` is primary; `.garuda/` stays a back-compat alias** (existing repos keep working).
  Precedence when both exist: `.agent/` wins, `.garuda/` merges underneath.
- New module `garuda/config/agent_home.py`: `resolve_agent_home(workspace) -> AgentHome`
  exposing `.agents_dir`, `.skills_dirs`, `.mcp_paths`, `.tools_dir`, `.settings`.
  Every entry point (CLI, serve, SDK, recipes, harbor) resolves the home once and threads its
  subpaths into the loaders that already accept them.
- This makes *"add it to the `.agent` folder and use it"* literally true for all three asset types
  with a single discovery root, and gives us one place to hang a `settings.yaml`.

---

## 2. Configurability (the three asks)

### B1 — Custom tools (file-based, per-run) — NEW
- Discover `.agent/tools/*.py`. Each module contributes tools via either a `TOOLS = [...]`
  list of `Tool` instances **or** a `def register(registry): ...` hook.
- Loaded into the **per-run** registry (see C1), never the global one.
- **Trust boundary (decision below):** importing project `.py` executes code. Gate behind an
  explicit opt-in — `settings.yaml: load_project_tools: true`, CLI `--load-project-tools`, or
  SDK flag — off by default, with a one-line warning when enabled. (MCP stdio servers already
  launch processes, so there's precedent, but tool import is more implicit — hence opt-in.)
- Profiles can still restrict via their `tools:` allowlist; a custom tool must be both loaded
  *and* listed (or the profile omits `tools:` = allow all discovered).

### B2 — Custom MCP servers — wiring delta
- Add `.agent/mcp.json` / `.agent/mcp.yaml` to `resolve_mcp_config_paths` conventions (before
  `.garuda/*`).
- **Default to project→global layering** (currently opt-in via `GARUDA_MCP_MERGE`): a project
  server list should merge with the global one out of the box, project winning on name clash.
  Keep an env/setting to force single-file (old behavior) for anyone who relied on it.
- **Per-profile server allowlist:** optional `mcp_servers: [name, ...]` on a profile to select a
  subset of the resolved servers (parallels how `tools:` selects builtins). Omitted = all.

### B3 — Skills — wiring delta
- Add `.agent/skills` to the discovery dirs in `resolve_system_prompt` (before `.garuda/skills`).
- **Enforce `allowed-tools` frontmatter:** today it's parsed (`Skill.allowed_tools`) but ignored.
  When a skill declares `allowed-tools`, surface/enforce it so invoking that skill's workflow is
  scoped. (Low-risk: at minimum record it; ideally the permission engine consults it.)

---

## 3. P1 scalability triad

### C1 — Scoped tool registry (foundation)
- Introduce a `ToolRegistry` **instance** class: `register/get/names/select(names)`.
- One process-wide read-only `builtin_registry()` seeded once with builtins.
- Per run build a **layered** registry: builtins (shared, stateless) + custom (`.agent/tools`,
  SDK-registered) — selection resolves against the layered view; MCP tools stay per-manager.
- `build_toolkit` / `prepare_agent_run` gain an optional `registry` / `extra_tools` param;
  default = builtins so every existing caller is unchanged.
- Keep module-level `register_tool`/`tools_for_names` as a **thin shim** over a default registry
  so current tests and the SDK static method keep working, but the run path no longer mutates
  global state. `SoftwareAgent.register_tool` becomes per-instance.
- **This unblocks B1 and C2** (heterogeneous concurrent runs).

### C2 — Job-queue server
- New `garuda/interfaces/jobs.py`: `JobManager` holding `dict[job_id, Job]`; each `Job` has a
  state (`queued|running|succeeded|failed|cancelled`), an `EventStore`, an `asyncio.Task`, result.
- New JSON-RPC methods (keep blocking `run` for back-compat):
  - `submit(params)` → `{job_id}` immediately (spawns task).
  - `status(job_id)` → state + turn count.
  - `events(job_id, cursor)` → events since `cursor` (needs `EventStore.get_since(n)` — small add)
    for polling/streaming.
  - `result(job_id)` → final result once done.
  - `cancel(job_id)` → `task.cancel()`.
- **Concurrency cap** via a semaphore (configurable `max_jobs`); excess jobs stay `queued`.
- Depends on C1 (concurrent heterogeneous tool sets in one process).

### C3 — Model-concurrency governor
- New `garuda/model/governor.py`: process-wide async governor with a configurable limit
  (`GARUDA_MODEL_MAX_CONCURRENCY` / `settings.yaml`), optionally per-provider buckets keyed off
  the model prefix.
- `LitellmModel.complete` / `complete_streaming` acquire a slot **per attempt** (so a request
  sleeping on Retry-After backoff releases its slot instead of starving others).
- Default limit high/unlimited → single-run behavior identical; matters only when N runs share a
  process (job queue, batch eval).

---

## 4. Sequencing & dependencies

```
C1 (scoped registry) ──┬──> B1 (file tools)
                       └──> C2 (job server)
B2 (MCP wiring)  ── independent
B3 (skills)      ── independent
C3 (governor)    ── independent (pairs with C2)
§1 (.agent home) ── foundation; do first, small
```

**Recommended order (highest leverage → most infra):**
1. **§1 `.agent/` home resolver** — small, unlocks the "one folder" UX for everything.
2. **C1 scoped registry** — foundation; removes global-state hazard.
3. **B1 file-based custom tools** — the missing capability, needs C1.
4. **B2 MCP wiring + B3 skills** — small deltas over existing code.
5. **C3 governor** — independent, cheap, protects batch/eval.
6. **C2 job-queue server** — largest; most "service infra", least benchmark-relevant.

Each step ships with tests and keeps the suite green. Docs (`ENGINEERING_PLAN.md`) updated per step.

---

## 5. Open decision

**Custom-tool trust model** — auto-importing `.agent/tools/*.py` runs repo code at startup.
Options: (a) opt-in flag, off by default (recommended); (b) always on (matches MCP's implicit
process launch, but riskier on a dev host); (c) explicit per-file allowlist in `settings.yaml`.
Recommendation: **(a)** — safe default, one flag to enable.
