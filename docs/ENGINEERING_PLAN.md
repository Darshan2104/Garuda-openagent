# Garuda — Review Findings & Engineering Plan

**Status:** Draft for brainstorming · **Date:** 2026-07-02
**Input:** Full code review of v1.1.0 (core, tools, context, model, MCP, workspaces, interfaces, eval, tests)

---

## Status update 3 (2026-07-02, latest) — Phase E4 + remaining backlog

Landed and committed since update 2:
- **E4** hardened OS sandbox — macOS Seatbelt + Linux bwrap backends, `--clearenv`/
  `--unshare-net`, targeted binds, subprocess env scrubbing, loud failure when no
  backend, docker/remote resource + network limits. **Live-verified on macOS**
  (write confinement, env scrub, network deny). Commit `5fde893`.
- **C4/C5** pluggable condenser interface (microcompact / recent-window / summarizing),
  selectable via `AgentConfig.condenser`. Commit `1aa0a85`.
- **B5** parallel execution of read-only tool calls (concurrent when a response is all
  parallel-safe reads; ordered results, id pairing preserved). Commit `1aa0a85`.
- **F2** OpenInference/OTel tracing (zero hard dependency; events→span tree). Commit `97b328a`.
- **F3** harness ablation runner (variant matrix, ground-truth grading, comparison table).
  **Live-verified on Gemini 2.5 Flash** (6 runs). Commit `97b328a`.
- **D2** streaming model API + rich TUI — see final commit.

Also landed:
- **F1** lossless ATIF — per-step token usage + estimated cost on each model step,
  tool-result attribution by `tool_call_id` (correct when a tool is called twice in one
  turn), auto-aggregated totals + session duration, and real `total_cost_usd` computed via
  litellm pricing (the previously-dead cost path). Validates against the real Harbor schema.
- **F4** cost/latency dashboard (`python -m garuda.eval.dashboard`) over the session store
  and/or ATIF files. **Live-verified** across the persisted Gemini sessions (real tokens,
  cost, duration, aggregated totals).

Test suite: 210 passing, 0 failures (3 tmux skips — tmux not installed locally).
Remaining/optional: full Terminal-Bench-via-Harbor scoring (needs the Harbor + docker
stack and a frontier API key; the ablation runner covers the harness-comparison need
locally), true end-to-end token streaming into the agent loop (D2 ships the streaming
model API + rich rendering; wiring live tokens through `loop.py` is a follow-up).

**Planned next (not started):** §6 RLM-style tool output buffer; §7 MCP JSON/auto-discovery
(Cursor/Claude-style `mcpServers` dict + conventional config paths); §8 tool/verifier robustness
(from multi-harness trace review). See unchecked todos in §6.5, §7.4, and §8.4.

## Status update 4 (2026-07-03) — H1 + G1 + D7a

- **H1** (§8, P0) search/read tool correctness: `grep` now branches file-vs-dir and uses `-R`
  + trailing-slash so single files and symlinked paths match reliably on GNU/BSD (was silent
  "no matches"); honest error when a path is unreadable; `glob` follows symlinks (`find -L`);
  `read_file`/`grep` share one **lexical** confinement resolver (`workspace/paths.py`) so they
  reach the same in-workspace-relative paths as `bash` while still blocking `..`/absolute escapes.
- **G1** (§6) RLM-style tool-output buffer: `core/buffer.py` stores full tool output in the session
  dir and injects a compact stub (preview + pointer); `buffer_grep`/`buffer_slice`/`buffer_list`
  retrieve on demand. Wired into `loop._execute_tool` (buffers above `buffer_threshold_bytes`,
  default 30 KB; shaping now inside the try/except). No more lost middle bytes on large/ephemeral
  output. **Live-verified on Gemini 2.5 Flash**: 58 KB `seq` output buffered, agent retrieved a
  middle line via `buffer_slice` and answered correctly. (Live testing caught a real bug — Gemini's
  ~1KB tool-call ids overflowed the filename limit; fixed with short hashed buffer ids.)
- **D7a** (§7) MCP JSON + auto-discovery: `load_mcp_config` accepts JSON `mcpServers` dict (+ `servers`
  list, `mcp_servers` alias, nested `mcp`), auto-discovers `.garuda/mcp.json` → `.garuda/mcp.yaml`
  → `.cursor/mcp.json` → global; empty/malformed configs no longer crash (fixes a P1). **Live-verified**
  discovery + `${VAR}` interpolation. Also fixed a pre-existing `tools ↔ mcp.client` circular import
  (lazy import + module `__getattr__`), so `import garuda.mcp.client` and `test_mcp_v2` work standalone.

Test suite: **241 passing**, 0 failures (3 tmux skips). Still open from §6/§7/§8: G2/G3 (semantic
buffer retrieval, full RLM mode), D7b/D7c (HTTP/SSE transport, config merge, `garuda mcp list`),
H2/H3/H4 (verifier hardening, tool-failure steering, answer disambiguation).

## Status update 5 (2026-07-04) — review-driven hardening

From an external review of the matured codebase (core, tools, workspace, permissions, verifier,
MCP, subagent):

- **P0 MCP call robustness** — `McpRemoteTool.execute` wraps `call_tool` in a timeout + try/except;
  a hung/failing MCP server returns an error observation instead of hanging/crashing the turn.
- **P0 bash** — `timeout`/`cwd` params exposed; failure-with-no-output no longer says "successfully".
- **P1 verifier fails CLOSED (H2)** — LLM verdict retries once then rejects on error; unparseable
  verdict rejects (was: approve on both); robust markdown-tolerant parse; evidence window 300→1200;
  general (not SWE-only) persona; numeric-contradiction hint; optional `answer_check(env)` domain
  grader hook. **LLM judge is now opt-in** (`enable_llm_verifier` defaults False): it costs a model
  call per completion and fails closed, so it's a deliberate choice; deterministic checks (summary,
  permission-screened commands, `answer_check`) stay on by default.
- **P1 subagents** — inherit the parent's `approval_handler` (ASK no longer auto-denies inside a
  subagent) and `hooks` (lifecycle hooks now fire in subagents).
- **P1 streaming usage** — `stream()` requests `stream_options={"include_usage": True}` and
  `complete_streaming` captures it, so streamed/TUI sessions get token/cost accounting.
- **P2** — `finish_reason == "length"` truncation surfaced to the loop as a note; MCP tool-name
  collisions de-duplicated (no silent shadowing). Verified all interfaces attach event persistence.

Test suite: **257 passing**, 0 failures (3 tmux skips). Live-verified end-to-end on Gemini 2.5 Flash.
Not changed (by design): `bash` in smart mode still default-allows non-denylisted commands — that
confinement is delegated to the OS sandbox (E4) + `allow_prefixes` (E5), not the regex denylist.

## Status update 6 (2026-07-04) — H3 + H4

- **H3 tool-failure steering** — the loop tracks a consecutive-failure streak across tool steps
  (sequential and parallel paths); after 3 all-error steps it injects a steering nudge ("try a
  different tool/path, e.g. bash with an explicit path") and emits a `failure_streak`/`steered`
  event. A step with any success resets the streak. Complements E3 repetition detection (identical
  calls) — H3 catches "different args, all failing". Build profile now documents the bash fallback.
- **H4 answer disambiguation** — `task_complete` gains an optional `answer_rationale` field; it's
  threaded into the verifier and surfaced in the LLM-judge prompt. When the summary shows numbers
  differing by >10x, the judge is told to REJECT-and-disambiguate if no rationale was given, or to
  accept only if the rationale justifies the choice. (Kept as a judge signal, not a brittle hard
  gate, to avoid false-rejecting cases like "1000 files in 5 seconds".)

Test suite: **263 passing**, 0 failures (3 tmux skips). Live-verified recovery-after-failure on
Gemini 2.5 Flash. Remaining: G2/G3 (semantic buffer retrieval, full RLM mode), D7b/D7c (HTTP/SSE
MCP transport, config merge, `garuda mcp list`); H4's structured-JSON verifier verdict (§2.3) left
as a follow-up since robust markdown-prefix parsing already handles it.

## Status update 7 (2026-07-04) — D7b/D7c + structured verdict + G2 + F3 (offline)

Follow-on batch after the H-track close-out (T1 real-provider validation deliberately deferred —
still the top open item; see §4 north-star):

- **D7b MCP HTTP/SSE transport** (§7.4) — `mcp/client.py` now opens `streamablehttp_client` /
  `sse_client` (both present in the `mcp` pkg) in addition to stdio; `_open_transport` selects by
  transport. Config (`mcp/config.py`) gains `headers`, infers transport from `url`/`type`
  (Cursor/Claude convention: a `url` entry ⇒ `http`), and accepts `auth`/`token`/`bearer`
  shorthand → `Authorization: Bearer …`. Per-server connect timeout (30s). **Live-caught bug**
  (mocks missed it): a failed HTTP connect surfaces `CancelledError` from the transport's inner
  anyio task group — a `BaseException` that escaped `start()`'s `except Exception` and would crash
  the whole run. Fixed with a guarded `except asyncio.CancelledError` (re-raises only on genuine
  self-cancellation via `task.cancelling()`); regression test connects real transports to a dead
  port and asserts survival.
- **D7c MCP polish** (§7.4) — `resolve_mcp_config_paths` + `load_and_merge_mcp_configs`: with
  `GARUDA_MCP_MERGE=1`, project and global configs merge (union by name, **project wins**); default
  stays first-project-wins single path. `build_toolkit` accepts a path or a list. New `garuda mcp
  list` CLI shows the resolved path(s), configured servers, and (unless `--no-connect`) the tools
  each server exposes. All shared setup paths (run/chat/serve/SDK/recipes) route through the plural
  resolver; `resolve_mcp_config` kept as a back-compat wrapper.
- **Structured verifier verdict** (§2.3 / H4 leftover) — the LLM judge is now asked for
  `{"verdict": "APPROVED"|"REJECTED", "reason": "…"}`; `parse_verdict` reads JSON (raw, fenced, or
  embedded in prose) and **falls back** to the APPROVED/REJECTED prefix so plain replies still work.
  Still fails closed on unparseable/errored replies.
- **G2 buffer_query** (§6.5) — `buffer_query(buffer_id, question)`: a helper model map-reduces over
  line-numbered buffer chunks (capped: 8 chunks / 6 KB excerpts) and returns relevant lines + a
  synthesized answer with line refs. Registered + added to the build profile; uses `ctx.model`
  (already wired by the loop). **Live-verified on Gemini 2.5 Flash**: found the one FATAL line in a
  120-line log and answered with the timestamp + cited line.
- **F3 (offline slice)** — the headline Terminal-Bench number needs the Harbor+Docker stack and a
  frontier key (same gate as T1), so the offline proxy — the ablation runner — was hardened: added
  buried-value **search**, in-place **edit**, and integer **sum** tasks (ground-truth graded) so it
  exercises the tool families TB stresses. Terminal-Bench README documents the ablation runner as
  the recommended docker-free pre-flight.

Test suite: **300 passing**, 0 failures (3 tmux skips); +37 new tests. Still open: G3 (full RLM
REPL mode) and the T1 real-provider (Anthropic/OpenAI) live validation — the top remaining item.

## Status update 8 (2026-07-04) — agent-quality track begins: reasoning / extended thinking

New direction (recorded): the goal is the **agent's intrinsic capability** to win on any benchmark,
not eval infrastructure. Audit found the biggest untapped lever was **reasoning/extended-thinking
support — previously zero**. Landed:

- **Model layer** (`litellm_model.py`): `reasoning_effort` (minimal|low|medium|high, cross-provider)
  and `thinking_budget_tokens` (explicit Anthropic budget) knobs on `LitellmModel`; `_apply_reasoning`
  attaches `reasoning_effort` or `thinking={type:enabled,budget_tokens}` (with max_tokens headroom +
  temperature stripped for Anthropic) and sets `drop_params=True` so a shared profile is safe on a
  non-reasoning model. Captures `reasoning_content` + `thinking_blocks` from responses
  (`_normalize_thinking_blocks` coerces pydantic→dict).
- **Cross-turn preservation** (the subtle part): the loop stashes `thinking_blocks` on the assistant
  `Message.metadata`; `_message_to_litellm(..., include_thinking=True)` echoes them back **only for
  Anthropic + reasoning-on**, so interleaved thinking survives across tool-call rounds (required by
  Anthropic) without sending blocks to providers that reject them.
- **Config/CLI**: `AgentConfig.reasoning_effort` / `thinking_budget_tokens`, profile YAML fields,
  `garuda run --reasoning-effort/--thinking-budget`; threaded through run/chat/SDK model construction.
- **Streaming**: `StreamDelta` carries `reasoning_delta`/`thinking_blocks`; `complete_streaming`
  accumulates them (best-effort — loop uses `complete()`).

Verified: **live on Gemini 2.5 Flash** (2622 chars reasoning_content + 1 thinking block captured,
multi-step problem solved correctly); `drop_params` deterministically strips `reasoning_effort` for
`gpt-4o-mini` (confirmed via `get_optional_params`). Test suite **311 passing**, +11 reasoning tests.

Remaining agent-quality levers (all requested): behavior + system-prompt tuning, tool
reliability/coverage, context/cache quality. (Note: the OpenAI key in the dev env is invalid — an
env issue, not code; Anthropic thinking round-trip still wants a live key to confirm end-to-end.)

## Status update 9 (2026-07-04) — agent-quality: behavior & prompts

Rewrote the core operating prompts from terse SWE-only blurbs into a tight, general-agent operating
manual (`build.yaml` + `DEFAULT_SYSTEM_PROMPT`) encoding the behaviors that separate strong agents on
any benchmark: **understand-first, read-before-edit, plan multi-step work, verify-before-finish, be
persistent/adaptive on failure, work efficiently with context (buffer tools), follow the task's exact
output format (exact-match matters), and ground every claim in tool evidence.** Kept general across
coding/research/QA/ops rather than SWE-specific.

The loop's behavioral machinery was already solid and left intact: verifier repair loop (rejection
feedback re-injected, agent retries), last text answer preserved as the final message before nudging,
repetition detection (E3), failure steering (H3), turn/context budget reminders (B6). No test asserts
on prompt text, so this is a pure quality change. **Live-verified on Gemini 2.5 Flash**: a
search→read→exact-write task completed correctly in 5 turns (exact-match answer written). Suite: 311
passing.

## Status update 10 (2026-07-06) — agent-quality: tool reliability

Audited the workhorse tools; `read_file` (cat -n, offset/limit, truncation, EOF guards) and `edit`
(exact-match, uniqueness, snippet feedback) were already strong. Closed the remaining reliability
gaps on `edit` — the tool the agent leans on most:

- **Near-miss diagnostics** — the #1 edit failure is an `old_string` that differs only in
  indentation/whitespace or line endings, which previously gave a bare "not found" and sent the
  model in circles. `edit` now diagnoses it: detects a whitespace-normalized or CRLF/LF near-match
  and tells the model exactly what to fix (re-read and copy the exact bytes/indentation).
- **Empty `old_string` guard** — was silently treated as "occurs N times"; now a clear error
  pointing at write_file.
- **`write_file`** confirmation now reports line count alongside bytes (better observation signal).

Suite: **316 passing**, +6 tool tests.

## Status update 11 (2026-07-06) — agent-quality: context/cache quality

Audited the context stack (usage-driven trigger via provider tokens, pluggable condensers,
microcompaction). It was already strong; fixed a real quality bug against §6.6 and clarified the
cache tradeoff:

- **Microcompaction now preserves buffer retrieval pointers.** Previously, pruning a bulky *old*
  tool output replaced it with a generic "re-run the tool" stub — which **destroyed the
  `buffer:<id>` pointer** for outputs captured by the G1 tool-output buffer, stranding data that
  was still on disk. `microcompact_messages` now detects a buffer id (in metadata or the stub text)
  and keeps a retrieval pointer ("...retained in buffer:<id> — retrieve with
  buffer_grep/buffer_slice"), so pruned-but-buffered output stays reachable. This is the §6.6
  "keep `metadata.buffer_id` so the agent can still retrieve" item, done properly.
- **Honest cache accounting.** Corrected the "cache-friendly / stable prefix" claim: pruning in
  place does reset the prompt cache from the first pruned message. The real benefit is that it is
  cheaper than a full summarize (no LLM call; message structure + tool_call ids stay valid), and
  prunes are batched (all eligible at once) and triggered only at high usage, so cache-miss events
  stay rare.

Suite: **318 passing**, +3 context tests. This closes the four agent-quality levers requested
(reasoning, behavior/prompts, tool reliability, context/cache).

## Status update 12 (2026-07-06) — Tier-1 correctness fixes (from full-codebase review)

A fresh four-way parallel review (core/context, tools/workspace, model/verifier, MCP/interfaces)
surfaced a cluster of run-ending bugs. Fixed the verified Tier-1 set:

- **Loop message-sequence (severe):** the `finish_reason=="length"` truncation note and the
  repetition/failure steering nudges were appended *between* an assistant `tool_calls` message and
  its tool results — an invalid sequence providers reject with a 400 that aborts the whole run. All
  such harness notes are now **queued and flushed at the top of the next turn** (`pending_notes`),
  keeping every tool-call/tool-result block contiguous. `_record_failure_streak` queues instead of
  appending inline.
- **Permissions (two real holes):** (a) a configured tool-level `ask` (`tool_rules={bash: ask}`) was
  silently overwritten by the command screen — now the tool and command/path decisions are combined
  with the **stricter** winning; (b) `allow_prefixes` fast-path accepted shell-chained tails
  (`git status && curl … | bash`) — now a chained/redirected/substituted remainder falls through to
  the deny/ask patterns. Removed a dead `task_complete` branch.
- **Verifier:** agent-supplied `verification_commands` now run with a 300s timeout inside try/except
  (was unbounded — a test entering watch mode hung the completion gate forever).
- **Events:** `save()` now uses `default=str` (matched `append()`; snapshot previously crashed on
  datetime/Path payloads); `load()` tolerates a torn final line (crash-safety); a swallowed persist
  `OSError` is now logged.
- **Sessions (crash-resumability):** `messages.json` is now checkpointed **atomically every turn**
  via a `checkpoint` hook threaded through `run()`/rigorous/runner — a crashed/killed run is
  resumable from its last completed turn (previously only `finish()` wrote it, so any interrupted run
  was unresumable despite the crash-safe event log). `finish()`/meta writes are atomic too.
- **Pins:** `litellm>=1.63` (reasoning would silently no-op below it) and `mcp>=1.9` (HTTP/SSE
  transports).

Suite: **326 passing**, +8 tests. Remaining from the review (Tier 2, not yet done): model-layer retry
resilience (429/Retry-After, APIError, jitter, max_retries=0 raises None), process-group kill on
timeout (bash/docker orphans), background-task reaping, `image_read`/`web_fetch` confinement + SSRF,
sandbox `cwd` write-escape, `serve` loopback-RCE hardening, resume buffer re-pointing, serve/recipe
dropping reasoning+hooks+persistence, and the capability upgrades (multimodal content blocks,
persistent shell, ripgrep context lines, post-edit diagnostics).

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
  bounded output is one of the highest-leverage tools in every SOTA harness. **B1 landed** but
  **H1 (§8)** remains: `-r` on single files + symlinked paths can false-negative on macOS;
  must match `bash` reliability before declaring search done.
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
  → **Expanded in §6** (RLM-style buffer + retrieval tools; replaces blind truncation).

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
  *harness-chosen* checks (tests/build), uses structured output. **E1 landed** but **H2 (§8)**
  remains: trivial verify commands, truncated verdict context, approve-on-LLM-error, and no
  detection of contradictory numeric candidates in the transcript.
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
  initialize/list_tools; tool-name sanitization (OpenAI 64-char cap). **JSON `mcpServers`
  dict + auto-discovery** → expanded in §7.

---

## 3. Engineering plan (phased, brainstorm-ready)

> **Status update 2 (2026-07-02, later):** Build continued. Now landed: **B2** background
> bash (bash_background/task_output/kill_task), **B4** web_fetch + web_search, **B6** turn/
> context budget reminders, **C3** microcompaction (in-place tool-output pruning before full
> summarize), **D1** session persistence + `--resume` + `garuda sessions`, **D3** hooks from
> `.garuda/settings.yaml` with lifecycle events, **D4** AGENTS.md project memory, **D5** skills
> progressive disclosure, **D6** server bearer auth + Conversation env inheritance + chat
> workspace reuse, **D7** MCP fault isolation/timeouts/name sanitization (echo test fixed),
> **E1** evidence-based LLM verifier with git evidence, **E2** rigorous repair loop, **E3**
> repetition detection, **E5** command allow-prefix rules. Test suite: **157 passing, 0
> failures** (3 tmux skips — tmux not installed locally). **Verified live end-to-end** against
> Gemini 2.5 Flash: multi-turn tool use, LLM-verified completion, session persisted, resumed
> session continued with full context; web_fetch verified against a live URL. Remaining
> backlog: D2 streaming TUI, C4/C5 condenser interface, E4 sandbox hardening, B5 parallel
> tool calls, F2/F3 observability + Terminal-Bench baseline.
>
> **Status update 1 (2026-07-02, branch `fix/phase-a-correctness`):** Phase A landed in full
> (A1–A8), plus quick wins B1 (grep/glob/ls), B3 (todo tool), C1 (usage-driven compaction),
> and C2 (Anthropic cache_control). Decisions taken: string-replace `edit` is the only edit
> primitive (`apply_patch` deleted); the text-parser/bash-fence fallback was cut entirely.

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
| D7. MCP ergonomics + transport — see §7: `.mcp.json` / `mcpServers` dict compat, auto-discovery, then streamable-HTTP + auth | M |

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

### Phase G — RLM-style tool output buffer (see §6 for full spec)
| Item | Effort |
|------|--------|
| G1. `ToolOutputBuffer` + stub injection in loop + `buffer_grep` / `buffer_slice` / `buffer_list` | M |
| G2. `buffer_query` sub-LLM chunk retrieval + optional auto-inject | M |
| G3. Full RLM REPL mode + long-context benchmark | L |

### Suggested sequencing
A (all) → B1–B3 + C1–C2 (cheap, high leverage) → D1–D3 → C3–C5 → **H1** → **G1** → E + **H2–H3** → B4–B6 + D4–D7 → F → **H4** → G2 → G3 (optional).
Phases B/C and D can run in parallel tracks once A lands. **G1** pairs with C3 microcompact (stub + buffer pointer).
**H1** (§8) should land before or with G1 — buffer retrieval and corpus search both depend on reliable `grep`/`read_file`.

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
8. **Tool-output strategy**: keep head/tail truncation as default safety valve, or flip to buffer-first (stub in context + retrieval tools) for all outputs over N bytes? Recommendation: buffer-first above threshold; truncation only for preview lines. See §6.

---

## 6. RLM-style tool output buffer (planned — pick up next)

**Status:** Not started · **Added:** 2026-07-03
**Motivation:** Brainstorm on `ContextManager` / tool-level shaping (2026-07-03).

### 6.1 Problem — truncation is lossy

Today Garuda shrinks context at **two** layers:

| Layer | Where | What happens | Information loss |
|-------|-------|--------------|------------------|
| **Per-tool shaping** | `context/shaper.py` → `loop.py:_execute_tool` | Every tool result truncated to ~30KB (head + tail) before append | **Middle bytes gone forever** — especially bad for ephemeral `bash` output |
| **Microcompact pruning** | `context/condenser.py` | Old tool outputs outside recent window stubbed with "re-run the tool" | Full text gone from history; re-run may not reproduce same output |

**Partial mitigations today (not a buffer):**
- `grep` / `read_file` offset/limit re-fetch from **filesystem** — works for files, not for one-shot bash logs
- 3-step LLM summarization — lossy compression, not retrieval
- Condenser rebuild keeps last ~12 turns — middle history dropped or summarized

**Verdict:** For large or ephemeral tool output, head/tail truncation is the wrong default. The harness should **retain full output externally** and inject only **stubs + retrieval affordances** into the token window.

### 6.2 Reference — Recursive Language Models (RLM)

[RLM paper](https://arxiv.org/abs/2512.24601) · [reference impl](https://github.com/alexzhang13/rlm)

Core idea (different from Garuda's current linear loop):

```
Traditional:  [system + task + FULL/truncated tool output + history] → LLM every turn

RLM-style:    tool output → external buffer (potentially huge)
              LLM uses code / sub-calls to grep, slice, filter buffer
              Only relevant chunks enter context per turn
```

Garuda is well-positioned: already has `bash`, `grep`, subagents, event store, session dirs, harness-first design. Buffer belongs in the **harness**, not the model.

### 6.3 Proposed architecture

```text
Tool execute → full output → ToolOutputBuffer.store(tool_call_id, content)
                          → session dir: ~/.garuda/sessions/<id>/buffers/<tool_call_id>.txt

Context message (instead of truncated body):
  [buffer:abc123 | 84,291 chars | bash exit=1]
  Preview (first N lines): ...
  Use buffer_grep(id, pattern) or buffer_slice(id, start, end) to inspect.

LLM turn → optional retrieval tools → read from buffer → inject only needed slices
```

**Design principles:**
- **Buffer-first above threshold** — store full output; context gets stub + pointer, not head/tail chop
- **Explicit retrieval tools** — auditable, no magic auto-inject in v1 (optional in Phase 2)
- **Session-scoped storage** — align with existing `~/.garuda/sessions/<uuid>/` layout
- **Events JSONL** — log `buffer_id`, `size_bytes`, `truncated_in_context: false` for ATIF/debugging
- **Replace, don't duplicate** — `shape_observation` becomes preview-only or bypassed when buffered

### 6.4 Comparison vs current & vs other agents

| Approach | Info fidelity | Context tokens | Agent effort | Garuda today |
|----------|---------------|----------------|--------------|--------------|
| Head/tail truncate | Low | Fixed cap | Passive | **Default** |
| Microcompact stub | Medium | Lower | Must re-run tool | After 75% usage |
| LLM 3-step summarize | Medium (lossy) | Lower | Passive | Last resort |
| **RLM buffer + retrieve** | **High** | Low in history; pay on pull | Active (grep/slice) | **Not built** |
| Re-read from disk | High (files only) | On demand | Active | Via `read_file`/`grep` |

Other agents: Cursor/OpenHands condense lossily; SWE-agent re-reads files; **no mainstream coding harness fully implements RLM yet** — opportunity for Garuda differentiator.

### 6.5 Phased implementation

Effort scale unchanged: S = ≤1 day, M = 2–4 days, L = 1–2 weeks.

#### Phase G1 — Tool output buffer + retrieval tools (M)

Goal: Stop losing middle bytes on large/ephemeral tool output.

- [ ] **`ToolOutputBuffer` module** (`garuda/context/buffer.py` or `garuda/core/buffer.py`)
  - [ ] `store(session_id, tool_call_id, content) -> BufferRef` (path + size + preview)
  - [ ] `read`, `grep`, `slice` by `tool_call_id`
  - [ ] Session-scoped paths under `~/.garuda/sessions/<id>/buffers/`
  - [ ] Config: `buffer_threshold_bytes` (default: same as current `max_output_bytes` or higher)

- [ ] **Wire into `loop.py:_execute_tool`**
  - [ ] If `len(content) > threshold`: store full body in buffer; append stub message to context
  - [ ] If under threshold: keep inline (no buffer overhead)
  - [ ] Pass `session_id` / buffer root through `ToolContext`

- [ ] **New tools**
  - [ ] `buffer_grep(buffer_id, pattern, ...)` — ripgrep over stored output
  - [ ] `buffer_slice(buffer_id, start_line, end_line)` — line-range read
  - [ ] `buffer_list` — list buffers for current session (id, tool name, size, preview)

- [ ] **Update `shape_observation` / `ContextManager`**
  - [ ] Preview-only mode: first N lines in stub, not head/tail truncation of what enters history
  - [ ] Document interaction with microcompact (pruned stubs should retain `buffer_id` pointer)

- [ ] **Agent profiles** — add buffer tools to `build.yaml` (and optionally `plan`/`explore`)

- [ ] **Tests**
  - [ ] Large bash output: middle line recoverable via `buffer_grep`
  - [ ] Small output: no buffer file created
  - [ ] Resume session: buffers still addressable by id

- [ ] **Docs** — README + overview HTML section on buffer vs truncation

#### Phase G2 — RLM-lite: semantic chunk retrieval (M)

Goal: Agent doesn't have to know grep patterns; harness helps pull relevant excerpts.

- [ ] **`buffer_query(buffer_id, question)`** — sub-LLM scans chunks (map-reduce over buffer), returns relevant excerpts + line refs
- [ ] **Chunking strategy** — split buffer into ~2–4K char chunks with overlap; index by line range
- [ ] **Optional auto-inject (feature-flagged)** — before `model.complete`, if last assistant message references a `buffer_id`, inject top-k chunks (embedding or keyword match). Default off.
- [ ] **Cost guard** — max sub-calls per turn; cap total retrieved bytes per model call
- [ ] **Tests** — "find the AssertionError in pytest log" without agent writing grep

#### Phase G3 — Full RLM mode (L, optional)

Goal: Align with RLM paper REPL instantiation for extreme long-context tasks.

- [ ] **`--mode rlm` or `condenser: rlm`** — REPL-first loop variant
- [ ] **Buffers as REPL variables** — e.g. Python REPL in sandbox with `context_abc123` str + helper `llm_query(text) -> str`
- [ ] **Recursive sub-calls** — map to existing `invoke_subagent` or dedicated `llm_query` primitive
- [ ] **Benchmark** — OOLONG-style or internal long-log task; compare vs truncate-default and vs G1-only

### 6.6 Integration with existing context stack

| Existing piece | Change when G1 lands |
|----------------|----------------------|
| `shape_observation` | Preview in stub only; full body in buffer |
| `MicrocompactCondenser` | Stub replaces content but **keep `metadata.buffer_id`** so agent can still retrieve |
| `summarize_three_step` | Summarizer input can reference buffer previews; optional: summarize from buffer file not truncated history |
| `EventStore` | New fields: `buffer_id`, `buffer_bytes`, `buffered: true` on `tool_result` events |
| ATIF export | Attribute full output path for eval/debug (not necessarily inline in trajectory) |

### 6.7 Open questions (resolve before G1)

1. **Threshold**: buffer everything >30KB (current cap) or lower (e.g. 8KB) to save tokens earlier?
2. **Retention**: delete buffers on session end, or keep for `--resume` indefinitely?
3. **Security**: buffer files may contain secrets from bash — same permission rules as workspace? scrub on disk?
4. **MCP tool outputs**: buffer MCP results the same as built-in tools?
5. **G1 vs C3 item**: C3 planned "on-disk full output pointer" — G1 **is** that item, done properly with retrieval tools.

### 6.8 Suggested sequencing (add to §3)

Insert after Phase C context work (C3 microcompact is complementary):

**C3 (stub pruning) + G1 (buffer + retrieve)** → C4/C5 → … → G2 → G3 optional.

G1 is **P1 product quality** for any task with large test logs, build output, or `grep` with many matches — not blocked on P0 if harness already runs on real providers.

### 6.9 North-star metrics (buffer track)

| Metric | Today | Target (after G1) |
|--------|-------|-------------------|
| Recoverable bytes from large bash output | 0% (middle truncated) | 100% via buffer tools |
| Context tokens per 50KB tool result | ~30KB inline | ~500 char stub + on-demand retrieval |
| Agent re-runs due to "lost" log lines | Unknown | −80% on log-heavy tasks |
| RLM-mode long-context benchmark | N/A | Baseline after G3 |

---

## 7. MCP config ergonomics — JSON dict + auto-discovery (planned — pick up next)

**Status:** Partially done (stdio client, fault isolation, name sanitization, `${ENV}` in YAML) · **Added:** 2026-07-03
**Motivation:** MCP setup should feel as easy as Cursor / Claude Desktop — drop a JSON dict in a
conventional file and have the harness load it without extra flags.

### 7.1 Current state

| Aspect | Garuda today | Cursor / Claude Desktop / VS Code |
|--------|--------------|-----------------------------------|
| Format | **YAML only** (`yaml.safe_load` in `mcp/config.py`) | **JSON** (`mcpServers` dict) |
| Structure | `servers:` **list** with explicit `name` | `mcpServers:` **dict** keyed by server name |
| Discovery | **Manual** — `--mcp-config` or `mcp_config_path` in agent profile | Auto-reads `.cursor/mcp.json`, `claude_desktop_config.json`, etc. |
| Transport | **stdio only** (non-stdio entries logged and skipped) | stdio + HTTP/SSE in many clients |
| Already works | `${VAR}` env interpolation, per-server fault isolation, `mcp__server__tool` namespacing | — |

**Loader today** (`garuda/mcp/config.py`):

```python
data = yaml.safe_load(Path(path).read_text())
servers = data.get("servers", [])  # list only; no mcpServers dict
```

**Wiring:** `build_toolkit(..., mcp_config_path)` → `McpClientManager.from_config(path)` — only runs when a path is explicitly provided.

### 7.2 Target formats (all normalize to `list[McpServerConfig]`)

**Garuda YAML (existing):**

```yaml
servers:
  - name: github
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: ${GITHUB_TOKEN}
```

**Cursor / Claude Desktop JSON (to support):**

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "${GITHUB_TOKEN}" }
    }
  }
}
```

**Conversion rule:** dict key → `McpServerConfig.name`; omit `transport` → default `stdio`.

Optional top-level aliases to accept: `mcp_servers` (snake_case), nested under `mcp` key (some editors).

### 7.3 Auto-discovery (conventional paths)

When `--mcp-config` is **not** passed and agent profile has `mcp_config_path: null`, resolve in order (first file wins):

1. `{workspace}/.garuda/mcp.json`
2. `{workspace}/.garuda/mcp.yaml`
3. `{workspace}/.cursor/mcp.json` (drop-in compat for repos already using Cursor)
4. `{GARUDA_GLOBAL_SETTINGS dir}/mcp.json` or `~/.garuda/mcp.json` (global fallback)

Log at INFO which file was loaded. If none found, MCP disabled (current behavior).

**Do not** auto-read `claude_desktop_config.json` from macOS app support — wrong scope (user-global, mixed with unrelated keys); document manual copy or symlink instead.

### 7.4 Phased todos

#### D7a — JSON dict compat + auto-discovery (S–M, do first)

- [ ] **`load_mcp_config(path)`** — branch on extension: `.json` → `json.load`, `.yaml`/`.yml` → `yaml.safe_load`
- [ ] **`_parse_mcp_servers(data) -> list[dict]`** — accept:
  - [ ] `servers` (list, current YAML)
  - [ ] `mcpServers` (dict, Cursor/Claude style)
  - [ ] `mcp_servers` (dict, snake_case alias)
- [ ] **`_dict_to_server_configs(entries)`** — normalize dict entries: `name` from key or `name` field; default `transport: stdio`
- [ ] **`resolve_mcp_config(workspace, explicit_path)`** — explicit path wins; else run discovery list from §7.3
- [ ] **Wire discovery** into `prepare_agent_run`, `garuda run` / `chat` / `serve`, `SoftwareAgent`, `Conversation` (single helper, no duplicated logic)
- [ ] **Agent profile** — `mcp_config_path` still overrides discovery when set
- [ ] **Tests** (`tests/test_mcp_v2.py` or new `test_mcp_config.py`):
  - [ ] JSON `mcpServers` dict → same tools as equivalent YAML list
  - [ ] Auto-discovery picks `.garuda/mcp.json` when present
  - [ ] `.cursor/mcp.json` discovery in workspace
  - [ ] Malformed JSON / empty dict → clear error or skip with log (no crash)
- [ ] **Fixtures** — `tests/fixtures/mcp_echo.json` mirroring `mcp_echo.yaml`
- [ ] **README** — document JSON format, discovery order, Cursor config copy-paste example

#### D7b — HTTP/SSE transport (M, after D7a)

- [ ] Implement streamable-HTTP / SSE client in `mcp/client.py` (today `url` field parsed but skipped)
- [ ] Config fields: `url`, `headers`, `auth` / bearer token via `${VAR}`
- [ ] Per-server connect timeout (handshake timeout exists for stdio)
- [ ] Tests with mock HTTP MCP server or recorded fixture

#### D7c — Polish (S)

- [ ] Merge project + global configs (union servers by name; project overrides global) — optional, flag-gated
- [ ] `garuda mcp list` CLI — show discovered config path + registered tool names (debug UX)
- [ ] Document env-var interpolation parity for JSON string values

### 7.5 Code touch points

| File | Change |
|------|--------|
| `garuda/mcp/config.py` | JSON load, `mcpServers` dict parse, `resolve_mcp_config()` |
| `garuda/agents/setup.py` | Use `resolve_mcp_config(workspace, mcp_config_path)` |
| `garuda/interfaces/main.py` | Pass workspace into MCP resolution |
| `garuda/sdk/software_agent.py` | Same |
| `garuda/interfaces/server.py` | Optional `mcp_config` param unchanged; discovery when omitted |
| `README.md` | JSON + auto-discovery section |

### 7.6 Open questions

1. **Merge vs first-wins:** discovery stops at first file, or merge `.garuda/mcp.json` + `~/.garuda/mcp.json`?
2. **`.cursor/mcp.json`:** always try, or opt-in via `GARUDA_CURSOR_MCP_COMPAT=1`?
3. **Invalid server entry:** skip entry vs fail entire config load?
4. **JSON comments:** strict `json.load` only, or allow JSONC (trailing commas) for hand-edited files?

### 7.7 Suggested sequencing

**D7a** (JSON + auto-discovery) is independent of §6 G1 — good quick win for onboarding.
Ship before **D7b** (HTTP). Fits in track **D4–D7** alongside product surfaces.

### 7.8 North-star metrics (MCP ergonomics)

| Metric | Today | Target (after D7a) |
|--------|-------|-------------------|
| Config formats supported | YAML list only | YAML list + JSON `mcpServers` dict |
| Steps to enable MCP | Create YAML + pass `--mcp-config` | Drop `.garuda/mcp.json` in repo, run `garuda run` |
| Cursor config reuse | Manual rewrite | Copy `.cursor/mcp.json` as-is |
| Time to first MCP tool call | ~5 min (read docs) | ~1 min (copy known JSON) |

---

## 8. Tool & verifier robustness (general — from harness trace review)

**Status:** Not started · **Added:** 2026-07-03
**Motivation:** Side-by-side trace review (Garuda vs Goose vs OpenCode) on research-heavy
tasks surfaced **harness bugs that are not benchmark-specific**. They affect any run where:

- Data lives under workspace-relative **symlinks** (common for large corpora, monorepo fixtures,
  shared asset dirs).
- The model uses the **`grep` tool** on a single file or symlinked tree.
- **`read_file`** and **`bash`** are both available but enforce different path rules.
- **`task_complete`** verification must catch plausible-but-wrong final answers.

Goose/OpenCode avoid several of these by shelling out exclusively; Garuda's dedicated tools
must be at least as reliable or the agent wastes turns recovering via `bash`.

### 8.1 Confirmed failure modes (general)

| # | Failure | Where | Symptom |
|---|---------|-------|---------|
| H-a | **`grep -rn` on files / symlinked paths** | `tools/search.py` | BSD/macOS `grep -r` returns **zero matches** on symlinked files while `grep -n` and `bash grep` succeed → model sees false "No matches found" |
| H-b | **`read_file` vs `bash` path policy mismatch** | `workspace/local.py:_resolve_path` | `read_file` resolves symlinks and rejects targets outside workspace root; `bash` reads through the link → two tools, two realities |
| H-c | **Verifier approves wrong answers** | `core/verifier.py`, `tools/task_complete.py` | Agent computes multiple interpretations, picks the wrong one; `verification_commands` only `cat answer.txt`; LLM verifier is SWE-tuned, sees truncated transcript, **defaults to approve on LLM error** |
| H-d | **No steering after repeated tool failures** | `core/loop.py` (missing) | Agent burns turns on dead-end `grep`/`read_file` before discovering `bash` works — recovery depends on model luck |
| H-e | **Ambiguous numeric completion** | verifier + loop | Agent explores competing formulas/interpretations, commits without disambiguation; no harness signal that magnitudes disagree |

**Out of scope here (benchmark/eval harness only):** copying/symlinking eval corpora into `app/`,
running domain-specific graders (e.g. OfficeQA `score_answer`) inside the agent loop, or
per-benchmark workspace layout — those belong in the eval runner, not core Garuda.

### 8.2 Design principles

1. **Tool parity** — if `bash` can read/search a workspace-relative path, `read_file` / `grep`
   must succeed on the same path (or fail with an actionable, consistent error).
2. **Fail loud, recover fast** — false "no matches" is worse than an error; repeated failures
   should trigger harness steering (system reminders), not silent churn.
3. **Verifier is skeptical** — completion checks validate *correctness evidence*, not just
   "summary present + file exists". Fail closed when evidence is missing or contradictory.
4. **Domain-agnostic** — improvements apply to coding, research, and ops tasks; domain graders
   plug in via profile hooks, not hardcoded benchmark logic.

### 8.3 Phased implementation

Effort scale unchanged: S = ≤1 day, M = 2–4 days, L = 1–2 weeks.

#### Phase H1 — Search & read tool correctness (P0, M)

Goal: `grep` and `read_file` are trustworthy on symlinks and single files.

- [ ] **GrepTool: drop `-r` for file targets** — when `path` is a file, use `grep -nH -E` (or
  ripgrep equivalent); reserve `-r`/`-R` for directories only
- [ ] **GrepTool: symlink policy** — for directory search, use `rg --follow` or `grep -R` with
  documented behavior; add `follow_symlinks: bool` config (default: follow within workspace)
- [ ] **GrepTool: honest empty results** — when exit 1 + empty stdout, message should distinguish
  "pattern not found" vs "path unreadable / skipped (symlink?)"; never silently equate the two
- [ ] **`_resolve_path` logical confinement** — confine on the **workspace-relative path** before
  symlink resolution, or maintain an allowlist of in-workspace symlink prefixes (e.g. `corpus/`);
  align `read_file`, `write_file`, `grep`, `glob` on one resolver
- [ ] **`additional_dirs` escape hatch** — document and test explicit extra roots for symlink
  targets that must remain outside the workspace tree
- [ ] **Tests** (`tests/test_tools_v2.py` or new `test_workspace_symlinks.py`):
  - [ ] symlinked file inside workspace → `grep` + `read_file` both succeed
  - [ ] single-file `grep` returns matches (regression for `-r` on file)
  - [ ] absolute path outside workspace still rejected

#### Phase H2 — Completion verifier hardening (P1, M)

Goal: `task_complete` rejects incomplete, unverified, or internally contradictory answers.

Builds on E1 (evidence-based verifier) but closes gaps seen in trace review.

- [ ] **Artifact checks** — if the task instruction names an output file (e.g. `answer.txt`),
  verifier fails when missing/empty before LLM verdict
- [ ] **Verification command quality** — reject `task_complete` when `verification_commands` are
  trivially non-validating (e.g. only `cat <file>` with no recompute); inject checklist feedback
  asking for a check that exercises the claimed result
- [ ] **Transcript contradiction detection** — scan recent assistant/tool messages for multiple
  final numeric candidates differing by >10× (or configurable ratio); reject with feedback to
  disambiguate before resubmitting
- [ ] **Richer LLM verdict context** — raise `EVIDENCE_CONTENT_CHARS`, include `answer.txt`
  body (or tail of transcript where answer was derived) in verifier prompt; keep within token budget
- [ ] **General verifier persona** — system prompt is "task completion verifier", not
  "software engineering agent only"; checklist covers unit/scale plausibility and formula consistency
- [ ] **Fail closed on verifier LLM error** — replace approve-on-exception default with
  `approved=False` + `checklist["llm_verdict_error"]=True` (or single retry, then reject)
- [ ] **Profile hook: `answer_check`** — optional `AgentConfig.answer_check: Callable[[Environment], VerificationResult]`
  so profiles (research, coding, eval) can plug domain validation without core knowing the benchmark
- [ ] **Tests**:
  - [ ] wrong answer with only `cat answer.txt` → rejected
  - [ ] summary mentions two conflicting magnitudes → rejected
  - [ ] LLM verifier exception → rejected (not auto-approved)

#### Phase H3 — Tool-failure steering & observability (P1, S–M)

Goal: harness helps the model recover when structured tools lie or error.

Extends B6 (budget reminders) and E3 (paralysis detection).

- [ ] **Consecutive-failure counter** — track per-tool streak of `is_error` or grep empty-results;
  after N (default 3), inject system reminder: "structured tool failing — try `bash` equivalent
  or alternate path"
- [ ] **Paralysis detector extension (E3)** — hash `(tool_name, arguments)` for repeated identical
  failing calls; escalate nudge text
- [ ] **Event / ATIF fields** — `tool_failure_streak`, `recovery_tool` when next turn switches
  tool family after failures (e.g. `grep` → `bash`); surfaces in dashboard for regression tracking
- [ ] **Agent profile system guidance** — document in default profiles: prefer `grep`/`read_file`
  for bounded search; on confinement or empty-search errors, fall back to `bash` with explicit path
- [ ] **Tests** — simulated 3× grep miss → reminder injected; identical failing call 4× → E3 nudge

#### Phase H4 — Answer disambiguation & completion hygiene (P2, M)

Goal: reduce premature commit when the agent explored multiple interpretations.

- [ ] **`task_complete` schema extension** — optional `answer_rationale` / `rejected_alternatives`
  fields; verifier checks rationale present when transcript shows >1 candidate
- [ ] **Loop guard** — if agent calls `write_file` on answer artifact then `task_complete` in same
  turn with no intervening validation command, verifier suggests adding a recompute step
- [ ] **Structured output for verifier verdict** — replace `APPROVED`/`REJECTED` prefix parsing
  with JSON schema (ties to §2.3 structured output item)
- [ ] **Tests** — agent summary lists two formulas; missing rationale → reject

### 8.4 Suggested sequencing

**H1** (tool correctness) is **P0** and unblocks trustworthy search/read on symlinks — do before or
in parallel with **G1** (buffer), since buffer retrieval also depends on `grep` working.

Recommended order:

**H1 → H2 → H3 → H4**, interleaved with existing **E1/E3** items where noted.

H1 does not depend on §6 or §7. H2's `answer_check` hook is the general extension point for
eval runners that want to call external graders without baking benchmark logic into core.

### 8.5 North-star metrics (robustness track)

| Metric | Today | Target (after H1–H3) |
|--------|-------|----------------------|
| `grep` false-negative rate on symlinked in-workspace files | observed non-zero | 0% |
| `read_file` / `bash` disagree on same workspace-relative path | yes (symlinks) | never |
| Wrong answer approved by `task_complete` when contradictory evidence in transcript | observed | 0% (H2) |
| Turns wasted on recovery after structured tool failure | untracked | −50% via H3 reminders |
| Runs depending on model learning to ignore `grep` tool | common | unnecessary after H1 |
