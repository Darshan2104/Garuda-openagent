# Garuda — Review Findings & Engineering Plan

**Status:** Draft for brainstorming · **Date:** 2026-07-02
**Input:** Full code review of v1.1.0 (core, tools, context, model, MCP, workspaces, interfaces, eval, tests)

---

## 0. Verdict

The RFC and architecture are genuinely good — the landscape analysis is accurate, the protocol-based
layering is right, and the module boundaries are clean. But **v1.1.0 is not "33/33 modules complete";
it is a well-shaped skeleton with several load-bearing bugs.** The most important one means the
harness almost certainly cannot run a multi-turn tool-use task against a real OpenAI/Anthropic
endpoint today: it has only ever been exercised end-to-end via `ScriptModel` in tests.

Rule for everything below: **fix correctness (P0) before adding anything**, because every new
feature built on the current loop inherits the same failure modes.

---

## 1. P0 — Correctness bugs (the harness doesn't work without these)

| # | Bug | Where | Impact |
|---|-----|-------|--------|
| 1 | **Assistant `tool_calls` never stored in history.** `Message` has no `tool_calls` field; loop stores only `response.content`. `role: tool` messages then reference tool_call_ids that never appear in the payload. | `types.py:14-19`, `core/loop.py:130-131`, `model/litellm_model.py:11-17` | OpenAI/Anthropic reject the message sequence with 400. Multi-turn tool use is broken against real providers. |
| 2 | **No error handling anywhere on the hot path.** No try/except around `model.complete()`, `tool.execute()`, `json.loads(tool_args)`, summarizer calls, MCP calls. Bash timeout raises uncaught **and never kills the child process**. | `core/loop.py:115,252`, `litellm_model.py:26,86`, `workspace/local.py:37-40` (copied into docker/remote) | Any API hiccup, missing file, malformed tool JSON, slow command, or MCP failure crashes the entire run and can leak processes. |
| 3 | **tmux marker polling races.** Marker is part of the typed command line, so it's visible in the pane instantly → polling returns before the command runs. Non-marker `execute()` waits max **1 second**. `exit_code` hardcoded 0, `stderr` always empty. | `workspace/tmux.py:70,82,93-103,122-129` | The tmux workspace — a headline feature — returns wrong/incomplete output and can never report failure. Existing test passes only because the asserted string is in the echoed command itself. |
| 4 | **`apply_patch` silently corrupts files.** Context/removed lines are not validated against the original; empty diff lines are dropped (desyncing the index); malformed hunks skipped; result written unconditionally. No dry-run, no rejection path. | `tools/patch.py:22-38,64-66` | Most real LLM-generated diffs will corrupt files with no error. |
| 5 | **Prose bash fences auto-executed.** If a response has no tool calls, any ```` ```bash ```` block in the text is parsed and executed — including quoted/illustrative/warning snippets. Applied unconditionally even for tool-calling models. | `litellm_model.py:32-46,93-94` | Model says "don't run `rm -rf …`" → harness runs it. |
| 6 | **Permission bypasses.** (a) `tmux_exec` skips `check_command` entirely; (b) verifier's `verification_commands` run with **no permission screening**; (c) no workspace path confinement — absolute paths and `..` read/write anywhere on host; (d) `apply_patch` classified as a *read* op. | `core/permissions.py:112-115`, `core/verifier.py:44-46`, `workspace/local.py:16-20` | The permission system can be walked around by any of three routes in default mode. |
| 7 | **Subagent forks stale context.** Fork snapshot is taken at first-turn construction (`[system, task]` only); a subagent invoked at turn 40 sees none of the conversation. | `core/loop.py:100`, `core/subagent.py` | Subagents can't actually use parent findings. |
| 8 | **Empty error output reported as success.** Shaper turns empty content into "Command ran successfully with no output", applied to error results too. | `context/shaper.py:2-3`, `loop.py:254` | Model believes failed commands succeeded. |
| 9 | Smaller but real: compaction rebuild drops the most recent *user* message instead of the seed task (`context/manager.py:91-108`); token counting is `chars//4` ignoring tool schemas (`litellm_model.py:110-112`); `usage` computed then discarded by the loop; completion detection is keyword-matching (`"done."`, `"finished"`) with a silent no-nudge respin loop (`loop.py:133-138,273-276`); events only persist on explicit `save()` — crash loses the trajectory; hook-blocked calls emit orphan `tool_call_id="blocked"` messages; `RemoteWorkspace` bind-mounts a *local* path on a *remote* daemon (never transfers files); `Conversation` hardcodes `LocalEnvironment` even when the parent `SoftwareAgent` was configured for docker. | — | — |

### P0 exit criteria
- A 20+ turn tool-use task completes against real OpenAI **and** Anthropic endpoints.
- A failing tool call / API 429 / command timeout produces an error observation, not a crash.
- Patch/edit operations either apply cleanly or fail loudly; corruption is impossible.
- All exec paths (bash, tmux, verifier commands, MCP) route through the permission engine; file ops are confined to the workspace unless explicitly allowed.

---

## 2. What's missing entirely (gap list vs Claude Code / OpenCode / Codex / OpenHands)

### 2.1 Tool suite (biggest capability gap)
- **`grep` / `glob` / `ls` tools** — model must shell out today. Ripgrep-backed search with
  bounded output is one of the highest-leverage tools in every SOTA harness.
- **`edit` (string-replace) tool** — the workhorse edit primitive everywhere. Whole-file
  `write_file` + fragile unified diff is the worst of both worlds. Pair with:
- **`read_file` with line numbers + offset/limit** — required for reliable editing and for
  reading large files without blowing context.
- **Background processes** — `bash(run_in_background=true)` + `task_output`/`kill` tools.
  Needed for servers, watchers, long builds. tmux is not a substitute (no job API).
- **Web search / web fetch** — Apex2's Terminal-Bench lesson: research-before-execute is a
  large score lever. Nothing in Garuda can touch the network deliberately.
- **Todo/plan tool** — structured task tracking (Claude Code TodoWrite, OpenHands task tracker).
  Keeps long tasks on-track; also gives the UI something to render.
- **Multi-file patch** — can't apply a normal `git diff` today (single file + separate `path` arg).
- Lower priority: notebook editing, LSP diagnostics-after-edit (OpenCode's edge), image resize
  before base64.

### 2.2 Project memory & steering (completely absent, cheap, high impact)
- **`AGENTS.md` / `GARUDA.md` project-instructions loading** — every serious harness injects a
  repo-level instructions file (Claude Code CLAUDE.md, Codex AGENTS.md, OpenCode rules). Nothing
  in Garuda reads project conventions. Support the emerging `AGENTS.md` standard + hierarchical
  discovery (repo root → subdir).
- **System-reminder–style injections** — a channel for the harness to steer mid-run (turn budget
  warnings, "context is 80% full", permission-mode notes) without polluting user messages.
- **Turn/budget awareness** — tell the model "N turns / K tokens remaining"; measurable
  benchmark lift, trivial to add.

### 2.3 Model layer
- **Streaming** (`stream()` on the protocol, delta events) — prerequisite for a usable TUI/IDE.
- **Retries/backoff + request timeouts** — bare `litellm.acompletion` today.
- **Reasoning/thinking support** — `reasoning_effort`/`thinking` params, preserving thinking
  blocks across tool turns (required for Anthropic interleaved thinking). Zero support today.
- **Prompt caching** — listed as KIRA's secret sauce in your own RFC, never implemented. No
  `cache_control` breakpoints, and compaction rewrites the whole prefix (worst case for cache).
- **Structured output** (JSON mode / schema) — verifier, summarizer, critic all parse free text
  today (`text.upper().startswith("APPROVED")`).
- **finish_reason handling** — truncated-by-max_tokens responses are currently indistinguishable
  from complete ones (`raw` is `{"model": name}`).
- **Cost accounting** — accumulate `usage` per run/session, `litellm.completion_cost`, cache-token
  breakdown, surfaced in events/result/ATIF.

### 2.4 Context management
- **Usage-driven compaction trigger** — use provider-reported `usage` from the last response, not
  `chars//4`. Current trigger also fires on turn count alone (~every 12 turns at 5% usage,
  3 LLM calls each).
- **Cache-aware microcompaction** — prune old tool *results* in place (replace with stubs,
  keep prefix stable) before resorting to full summarize-and-rewrite. This is the single biggest
  cost/latency lever combined with cache_control.
- **Fix the 3-step summarizer** — step 2 (questions) never sees the history it's critiquing;
  history is `[-60:]` messages at 800 chars (older content silently absent); no error handling
  (summarizer exception kills the run); commands invisible because tool_calls aren't stored.
- **Pluggable condenser interface** (OpenHands-style) instead of one hardcoded strategy.
- Persist full tool outputs to disk on truncation and tell the model where to find them.

### 2.5 Product surfaces
- **Session persistence + `--resume`** — `EventStore.load` exists but nothing uses it. No session
  directory, no conversation on disk. This is the biggest missing *product* feature.
- **Streaming TUI** — chat is a raw `print`/`input()` loop; nothing renders between task submit
  and final answer. (rich/textual; render todos, tool calls, diffs.)
- **Hooks are dead code** — registry exists but no config loading and every entry point passes
  `hooks=None`. Need: settings-file hook config, lifecycle events (session start/end,
  user-prompt-submit, pre/post tool, permission-decision, stop), command hooks.
- **Slash commands** — none.
- **Skills: progressive disclosure** — today every SKILL.md body is fully injected into the
  system prompt (linear context cost). SOTA: inject name+description index, load body on demand;
  frontmatter `allowed-tools`, arguments, bundled `scripts/`.
- **Server: auth + streaming** — JSON-RPC server has no auth with client-controlled workspace/
  workspace_kind (RCE one `--host 0.0.0.0` away) and blocks until the run finishes (unusable for
  IDEs). Needs bearer token, SSE/WS streaming, session registry.
- **Parallel tool execution** (Codex-style, safe ops only) and **cancellation** (Ctrl-C → abort
  signal threaded through loop/tools/subprocesses; no orphaned containers/tmux sessions).
- **Workspace reuse in chat** — currently a fresh container/tmux session *per user turn*.

### 2.6 Verification & rigorous mode (currently theater)
- Verifier is **self-graded**: agent picks its own `verification_commands` (or none → a 10-char
  summary passes). Make it evidence-based: verifier sees `git diff` + transcript tail, runs
  *harness-chosen* checks (tests/build), uses structured output.
- Critic sees only the agent's self-report text — give it the diff and transcript.
- **No repair loop**: critic rejection just marks failure. Add bounded feedback→retry cycles.
- Plan phase has no reliable termination (keyword heuristic); "Max turns exceeded" can get pasted
  in as the "approved plan".
- Add **loop/paralysis detection** (repeated identical actions → inject a nudge) — cheap
  Terminal-Bench points, called out in your own RFC (LucidCoder anti-paralysis) but not built.

### 2.7 Safety/sandbox
- Sandbox **silently no-ops without bwrap** (always on macOS — add Seatbelt) and the bwrap profile
  is weak: whole host readable (`--ro-bind / /`), env inherited (no `--clearenv`), **no
  `--unshare-net`**.
- Command rules: regex denylist on raw strings is trivially bypassed. Move to prefix/argv
  allowlist rules (`bash: allow: ["git status", "npm test:*"]`) with deny-by-default in smart mode
  for unmatched destructive classes.
- Container hardening: `--memory`, `--cpus`, `--network`, non-root user.

### 2.8 Observability & eval
- `TOOL_RESULT` events drop `tool_call_id` → ATIF attribution guesses by tool name (wrong when
  the same tool is called twice in a turn).
- No per-step tokens/duration/cost in ATIF; `cost_usd` path is dead code.
- Incremental event persistence (append JSONL as you go — crash-safe trajectories).
- OpenTelemetry/OpenInference spans (model call, tool call, compaction) — makes Garuda debuggable
  in Phoenix/Langfuse etc., and is a differentiator few open harnesses do well.
- MCP: only stdio (config parses `url` but nothing uses it) — add streamable-HTTP/SSE + auth
  headers; per-server error isolation (one bad server currently aborts all); timeouts on
  initialize/list_tools; tool-name sanitization (OpenAI 64-char cap).

---

## 3. Engineering plan (phased, brainstorm-ready)

> **Status update (2026-07-02, branch `fix/phase-a-correctness`):** Phase A landed in full
> (A1–A8), plus quick wins B1 (grep/glob/ls), B3 (todo tool), C1 (usage-driven compaction),
> and C2 (Anthropic cache_control). Decisions taken: string-replace `edit` is the only edit
> primitive (`apply_patch` deleted); the text-parser/bash-fence fallback was cut entirely.
> Test suite: 93 passing, 9 new tmux-logic tests, 10 provider-conformance tests,
> 10 permission-closure tests. Known remaining failure: `test_mcp_client_loads_echo_tool`
> (pre-existing MCP client initialize timeout — tracked under D7).

Effort scale: S = ≤1 day, M = 2–4 days, L = 1–2 weeks.

### Phase A — Make it actually work (correctness sprint)
> Goal: real-provider, multi-turn, crash-free. Everything else waits.

| Item | Effort | Notes |
|------|--------|-------|
| A1. `Message.tool_calls` field + round-trip through litellm serialization; loop stores assistant tool-call turns | M | Fixes P0 #1. Touch types, loop, litellm_model, context rebuild. |
| A2. Error containment: try/except around model call (retry w/ backoff + timeout), tool execute (→ `is_error` ToolResult), tool-arg JSON parse, MCP calls; kill subprocess on timeout | M | Fixes P0 #2. |
| A3. Replace `apply_patch` with `edit` (string-replace, uniqueness-checked) + `read_file` with line numbers/offset/limit; keep patch only if validated w/ dry-run + reject | M | Fixes P0 #4, unlocks reliable editing. |
| A4. Remove bash-fence auto-exec for tool-calling models (keep as explicit opt-in TextParserModel only) | S | Fixes P0 #5. |
| A5. tmux: split marker (`'__CMD''END__'`), poll only after-send pane delta, real exit-code capture (`echo marker $?`), honor timeout | M | Fixes P0 #3. |
| A6. Permission engine: route tmux_exec + verifier commands through `check_command`; classify patch/edit as write; workspace path confinement with explicit `additional_dirs` escape hatch | M | Fixes P0 #6. |
| A7. Subagent live-context fork; usage accumulation into events + `AgentResult`; incremental JSONL event persistence; fix empty-error shaping, compaction seed-skip bug, completion nudge | M | Cleans up P0 #7–9. |
| A8. **Provider conformance tests**: recorded (VCR-style) OpenAI + Anthropic multi-turn tool-use fixtures; CI runs them without keys | M | Prevents regression of A1 forever. This is the missing test category — everything today is `ScriptModel` happy paths. |

**Exit:** 20-turn real-model task, induced failures (timeout, bad JSON, missing file, MCP death) all survive, permission bypass tests pass.

### Phase B — Tool-suite parity
| Item | Effort |
|------|--------|
| B1. `grep` (ripgrep), `glob`, `ls` tools with bounded output | M |
| B2. Background bash: `run_in_background`, `task_output`, `kill_task` | M |
| B3. `todo` tool (structured plan state, rendered in events/TUI) | S |
| B4. `web_fetch` + `web_search` (pluggable search backend) | M |
| B5. Parallel execution of read-only tool calls with ordered results | M |
| B6. Turn/token budget reminders injected as system messages | S |

### Phase C — Context & cost excellence
| Item | Effort |
|------|--------|
| C1. Real token accounting: provider `usage` drives compaction trigger (~85–90% of window); drop turn-count trigger | S |
| C2. Prompt caching: `cache_control` breakpoints (system, tools, last message); cache-token reporting | M |
| C3. Microcompaction: prune old tool results in place (stub + on-disk full output pointer), stable prefix; full summarize only as last resort | L |
| C4. Summarizer fixes: step 2 sees history; full-history coverage via chunked map-reduce; structured output; error-contained (fallback keeps run alive) | M |
| C5. Pluggable condenser interface (recent-window / summarizing / cache-aware strategies) | M |

### Phase D — Product surfaces
| Item | Effort |
|------|--------|
| D1. Session store (`~/.garuda/sessions/<id>/` events + messages) + `--resume` / `garuda sessions list` | M |
| D2. Model streaming API + streamed rich TUI (tool-call rendering, diff preview, todo panel, Ctrl-C cancel) | L |
| D3. Hooks v1: load from `.garuda/settings.yaml`, lifecycle events (session/prompt/pre-post-tool/permission/stop), command hooks with allow/deny/mutate | M |
| D4. `AGENTS.md` project-memory loading (hierarchical) + `# remember`-style append | S |
| D5. Skills v2: progressive disclosure (index in prompt, body on demand), `allowed-tools`, arguments, bundled scripts; slash commands | M |
| D6. Server v2: bearer auth, SSE streaming, session registry; fix chat workspace reuse; fix `Conversation` env inheritance | M |
| D7. MCP: streamable-HTTP + auth headers, per-server fault isolation + timeouts, `.mcp.json` compat, name sanitization | M |

### Phase E — Verification & safety that actually verify
| Item | Effort |
|------|--------|
| E1. Evidence-based verifier: harness-collected `git diff` + transcript tail + permission-screened checks, structured verdict | M |
| E2. Rigorous mode: critic sees diff/transcript; bounded repair loop (reject → feedback → re-execute ≤N) ; plan-phase structured termination | M |
| E3. Loop/paralysis detector (repeated action hash → escalating nudges) | S |
| E4. Sandbox hardening: bwrap `--unshare-net`/`--clearenv`/targeted ro-binds, macOS Seatbelt, loud failure when unavailable; container resource limits | L |
| E5. Prefix/argv-based command rules replacing regex denylist | M |

### Phase F — Benchmark & observability push
| Item | Effort |
|------|--------|
| F1. `tool_call_id` + per-step tokens/duration/cost in events → lossless ATIF | S |
| F2. OpenTelemetry/OpenInference instrumentation (model/tool/compaction spans) | M |
| F3. Terminal-Bench 2.0 baseline run + ablation harness (verifier on/off, rigorous on/off, caching on/off, budget hints on/off) — publish the table | L |
| F4. Cost/latency dashboard from trajectories | S |

### Suggested sequencing
A (all) → B1–B3 + C1–C2 (cheap, high leverage) → D1–D3 → C3–C5 → E → B4–B6 + D4–D7 → F.
Phases B/C and D can run in parallel tracks once A lands.

---

## 4. North-star metrics
| Metric | Today | Target |
|--------|-------|--------|
| Multi-turn tool-use vs real providers | broken | works, 0 crash on induced failures |
| Terminal-Bench 2.0 (fixed model) | unmeasured | baseline + ablation table |
| Cache read ratio on long tasks | 0% | >70% after C2/C3 |
| Cost per 50-turn task | untracked | tracked, −40% after C |
| Crash rate under fault injection | ~100% | 0 |
| Session resume | none | full |

## 5. Open questions for brainstorming
1. **Edit primitive**: string-replace only (Claude Code style) vs keep a hardened patch tool too (Codex `apply_patch` style)? Recommendation: string-replace primary, patch secondary behind validation.
2. **TUI stack**: `rich` incremental rendering vs full `textual` app? (textual = better long-term, more work.)
3. **Streaming protocol for `serve`**: SSE vs WebSocket vs ACP (Agent Client Protocol — would give Zed/JetBrains integration for free)?
4. **Compaction strategy default**: microcompaction-first (cache-friendly) vs summarize-first (smaller context)? Recommendation: microcompact until ~90%, then summarize.
5. **Do we keep `rigorous` mode** as a separate agent class, or decompose into composable hooks (planner hook + critic hook + repair loop) so any profile can opt in?
6. **Text-parser fallback**: keep supporting non-tool-calling models at all, or cut scope? (It caused the bash-fence footgun.)
7. **Language**: any appetite for a Rust core later (Codex path), or commit to Python and optimize (uvloop, msgspec)?
