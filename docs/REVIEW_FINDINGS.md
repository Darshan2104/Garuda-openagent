# Garuda — Self-Review Findings & Fix Backlog

**Date:** 2026-07-03 · **Scope:** all code written during the Phase A–F build (~8,300 lines).
Produced by four adversarial reviewers over disjoint areas; top items verified against the
running code. Severity: **P0** = security/data-loss in normal use · **P1** = security or
wrong behavior · **P2** = robustness/edge case · **nit** = cosmetic.

Verification tags: `[verified-live]` reproduced on this machine · `[verified-code]` confirmed
by reading the exact code path · `[reported]` reviewer finding, not independently reproduced ·
`[plausible]` reviewer flagged as uncertain.

---

## P0 — fix first

1. **SSRF in `web_fetch`.** `[verified-code]` `tools/web.py:110-146`. `validate_http_url` checks
   only scheme+netloc; `urlopen` follows redirects. A prompt-injected page or task can make the
   host-side tool read `http://169.254.169.254/…` (cloud metadata), `http://127.0.0.1:8765/`
   (the local `serve` port), or `http://[::1]`, returning creds into the model/event log.
   `web_fetch` is a default `build` tool and runs on the host, not the sandbox.
   **Fix:** resolve host → reject loopback/private/link-local/reserved ranges via `ipaddress`;
   disable auto-redirects (custom `HTTPRedirectHandler`) or re-validate every hop; cap `max_bytes`
   (currently `read(max_bytes*10)` with no ceiling → ~10 GB memory blowup).

---

## P1 — security

2. **Seatbelt sandbox can read every host file.** `[verified-live]` `workspace/sandbox_policy.py:190`.
   `(allow file-read*)` lets a sandboxed command `cat ~/.ssh/id_rsa`, `~/.aws/credentials`, `.env`
   and copy it into the (host-persisted) workspace — env-scrubbing is moot. bwrap does NOT have this
   gap (it only ro-binds `ro_paths`+workspace). **Fix:** restrict reads to interpreter/lib paths +
   workspace via `(subpath …)`, or at least deny-read the known secret dirs.

3. **Hook RCE from a workspace `.garuda/settings.yaml`.** `[verified-code]` `plugins/hooks.py:266-290`,
   `runner.py` session-start wiring. Project-level shell hooks run on the host with no trust prompt,
   and `session_start` fires before the model runs. Server amplifies it: the JSON-RPC `workspace`
   param is client-controlled → any authenticated client gets host RCE. **Fix:** gate project shell
   hooks behind an explicit trust/allowlist; ignore client-supplied workspace for hook loading in
   server mode.

4. **Resume path traversal.** `[verified-code]` `core/sessions.py:172-187`. `resolve()` returns a
   `../..`-containing ref verbatim if the escaped dir exists; `load_messages` then reads arbitrary
   `…/messages.json` into the LLM context. Exposed to remote clients via the server `resume` param.
   **Fix:** reject refs containing `/`, `os.sep`, or `..`; require a bare id/prefix.

## P1 — correctness

5. **Seatbelt denies the `signal` operation.** `[verified-live]` `sandbox_policy.py:186`. `kill` of a
   child returns "Operation not permitted", breaking `kill_task`, `timeout`, `make -j`, test runners.
   `(allow process*)` does not cover `signal`. **Fix:** add `(allow signal (target self))` and
   likely `(allow file-ioctl)` + pty writes for interactive tools.

6. **Verifier fails open on a formatted rejection.** `[verified-code]` `core/verifier.py:207-224`.
   `text.upper().startswith("REJECTED")` misses `**REJECTED**: …` (bold) → falls through to the
   "treating as approval" branch → a rejected `task_complete` is accepted. **Fix:** strip markdown
   and scan first non-empty line (as `rigorous._parse_critic_verdict` already does); default
   unparseable/error to **rejection**, not approval — this is the completion gate.

7. **Condensation trigger uses a stale token count that lags a full turn.** `[verified-code]`
   `context/manager.py:48-84`. `note_usage` stores the last response's `prompt_tokens`; `append()`
   doesn't invalidate it, so the newly-appended assistant + tool results (up to ~30 KB each, ×N for
   parallel reads) aren't counted until the next response. A big turn can overflow the window before
   microcompaction fires. **Fix:** invalidate `_last_prompt_tokens` in `append()`, or use
   `max(_last_prompt_tokens, count_tokens(messages))`.

8. **Rigorous mode drops the approved plan (and repair feedback) when a context is threaded in.**
   `[verified-code]` `core/loop.py:138-155`, `core/rigorous.py:111-125`. `loop.run` only injects the
   `task` into the context when `context is None`; on resume / multi-turn chat / the rigorous
   executor, `exec_task` (task + plan) and per-round critic feedback never become messages the model
   sees. **Fix:** when a context is supplied, still append the effective task; thread context
   consistently through plan+repair phases (or fork a fresh context per repair round).

9. **Malformed MCP config crashes the whole run.** `[verified-code]` `mcp/config.py:40-49`. Empty
   file → `yaml.safe_load` returns `None` → `None.get` AttributeError; a server entry missing `name`
   → KeyError. Both abort before any server is tried, defeating the per-server fault-isolation goal.
   **Fix:** `data = safe_load(...) or {}`; wrap per-entry parsing in try/except + `entry.get("name")`.

10. **`bash_background` is broken under the bwrap backend.** `[reported]` `tools/background.py` +
    `sandbox_policy.py` bwrap (`--unshare-pid`, `--die-with-parent`). The nohup'd process is PID-1 of a
    per-exec namespace that's torn down when the launcher returns; the captured PID is namespace-local
    and meaningless to later `task_output`/`kill_task`. **Fix:** run background tasks via a persistent
    namespace, or document `bash_background` as unsupported with bwrap.

11. **Docker/remote timeout kills the client, not the in-container process.** `[reported]`
    `workspace/docker.py:112-117`, `remote.py:134-139`. `process.kill()` stops the local `docker exec`
    client; the workload keeps running in the container (holding its memory/pids limits) while the tool
    reports exit 124. **Fix:** on timeout run `docker exec <c> pkill -f <marker>`, or wrap the command
    with an in-container `timeout`.

12. **Permission path rules are bypassed by command & search tools.** `[reported]`
    `core/permissions.py:107-160`. Path deny/ask rules apply only to `read_file`/`write_file`/`edit`;
    `bash`, `tmux_exec`, and `grep`/`glob`/`ls` (which call `env.execute` directly) are never path-
    checked, so `cat .env` / `grep '' secrets/*` / `ls secrets/` evade a `**/*.env` deny rule.
    **Fix:** apply path screening to command/search arguments too.

13. **`allow_prefixes` bypasses ASK/deny on compound commands.** `[reported]`
    `core/permissions.py:126-148`. `allow_prefixes=["ls"]` makes `ls; sudo reboot` allowed (matches the
    `ls` prefix, skips the `sudo` ASK pattern). **Fix:** only apply prefix-allow when the command has
    no shell operators (`;`, `&&`, `||`, `|`, `$(`, backticks, newline), or screen each segment.

14. **`chat --json` corrupts stdout with human text.** `[reported]` `interfaces/cli.py`. Header,
    `task>` prompt, "thinking" status, `on_done`, and "Bye." all `print` to stdout, interleaving with
    the JSONL events. **Fix:** in JSON mode suppress human output / route prompt+status to stderr.

15. **Harbor adapter leaks an MCP manager and lacks try/finally.** `[reported]`
    `eval/harbor_adapter.py:124,148-181`. When a profile sets `mcp_config_path` AND harbor supplies
    `mcp_servers`, the first `mcp_manager` is overwritten without `close()`; any exception in the run
    skips `mcp_manager.close()` + `events.save` + temp-file cleanup. **Fix:** close the first manager
    before rebuilding; wrap the run body in try/finally.

---

## P2 — robustness & correctness edge cases

**Core loop / model:**
- Local exec timeout leaks grandchildren (no `start_new_session`/process group) and discards partial
  output. `workspace/local.py:55-60`. `[reported]`
- `verification_commands` run with no timeout → a hanging test blocks the run forever.
  `core/verifier.py:126`. `[reported]`
- Post-exec `shape_observation` runs outside `_execute_tool`'s try/except and `gather` uses
  `return_exceptions=False`, so a non-str `ToolResult.content` aborts a whole parallel batch and
  orphans tool_calls. `core/loop.py`. `[reported]`
- `count_tokens` fallback ignores tool_calls args + tool schemas → severe undercount when
  `token_counter` throws. `model/litellm_model.py:330`. `[reported]`
- Streaming (D2): tool-call fragment merge collapses calls when `index` is None; `complete_streaming`
  returns empty `usage`; `max_retries<=0` raises `None`. `model/litellm_model.py`. `[reported]`
- Microcompaction never prunes large tool-call *arguments* or assistant content (only tool results).
  `context/condenser.py:85-98`. `[reported]`
- Summarizer prompt (200×2000 chars) can itself exceed context at the moment it's needed.
  `context/summarizer.py`. `[reported]`
- `tool_rules: {x: ask}` is silently downgraded to allow (only DENY is early-returned).
  `core/permissions.py:150-160`. `[reported]`

**Tools / workspace:**
- `glob` single-`*` over-matches across directories (`src/*.ts` returns nested files).
  `tools/search.py:113-125`. `[reported]`
- `edit` snippet offset math is wrong for `\r\n` and pure deletions (cosmetic message only).
  `tools/edit.py`. `[reported]`
- `bash_background` `_TASKS` grows unbounded (never pruned on self-exit); `tail_bytes` unclamped.
  `tools/background.py`. `[reported]`
- docker/remote `read_file` via login shell (`bash -lc cat`) prepends profile banners to content.
  `workspace/docker.py:133`, `remote.py:155`. `[reported]`
- tmux marker breaks on a trailing `&` / unbalanced quote; `pane_delta` falls back to the full
  scrollback when the prefix doesn't match. `workspace/tmux.py`. `[reported]`
- `DockerLimits` emits bare `no-new-privileges` (some daemons need `:true`). `sandbox_policy.py:87`.
- Regex deny/ask lists are evadable (`rm -fr /`, `rm --recursive`, `dd of=`); treat as best-effort
  on top of the sandbox. `core/permissions.py:16-28`. `[reported]`

**Interfaces / sessions:**
- Bearer token compared with non-constant-time `==`; scheme is case/space-sensitive (spurious 401s).
  `interfaces/server.py:59`. `[reported]`
- Hand-rolled HTTP: no Content-Length cap or read timeout (slowloris/OOM); non-dict JSON drops the
  connection with no `-32600`. `interfaces/server.py:178-211`. `[reported]`
- `meta.json` writes are non-atomic (truncate-then-write) and read-modify-write without a lock →
  corruption / lost updates. `core/sessions.py`, `runner.py`. **Fix:** temp file + `os.replace`.
- Runner teardown failure (`cleanup_workspace`/`mcp.close`) skips `store.finish` + `on_session_end`.
  `interfaces/runner.py:164-179`. `[reported]`
- A normal per-turn `Exception` in chat propagates and kills the session instead of being reported.
  `interfaces/cli.py:153-164`. `[reported]`
- Resume seeds the *old* system prompt; freshly resolved skills/AGENTS.md are discarded.
  `interfaces/runner.py:90-101`. `[reported]`
- Relative skill dirs resolve from CWD, not workspace (wrong dir under serve/SDK).
  `agents/loader.py:176`. `[reported]`

**Eval / observability:**
- ATIF attribution tries name-match before index, so two same-named calls with no ids both bind to
  the first; success/`turns` "last-wins" drops turns in rigorous mode. `eval/atif_export.py`.
- MCP tool-name 64-char truncation can collide two tools to one name. `mcp/client.py:27`. `[reported]`
- `configure_tracing` is not idempotent (leaks the first provider; `_PROVIDER` desyncs from global).
  `observability/tracing.py`. `[reported]`
- `emit_spans_from_events` body isn't fully wrapped (a malformed event could raise once configured).
  `observability/tracing.py`. `[plausible]`
- `dashboard.collect_rows` catches a narrow exception set; a non-numeric metric crashes the table.
  `eval/dashboard.py:88`. `[reported]`
- `estimate_cost` doesn't apply the cache-read discount (overestimates cache-heavy runs).
  `eval/costs.py`. `[plausible]`
- `harbor_environment`: `int(timeout)` truncates sub-second to 0; `write_file` temp leaks on upload
  failure; `duration_ms` hardcoded 0. `eval/harbor_environment.py`. `[reported]`
- ablation: task `setup` outside try aborts the whole matrix on one failure; rigorous variant
  under-reports tokens (only build sub-agent usage); `_base_config` setattr accepts typo'd keys.
  `eval/ablation.py`. `[reported]`
- MCP client start/close cross-task cancel-scope hazard (anyio). `mcp/client.py`. `[plausible]`

---

## nits
- Possible consecutive USER messages (budget notice + 80% warning after a tool result; task+summary
  after rebuild) — verify Anthropic doesn't 400 on non-alternating roles. `[plausible]`
- `cache_control` on a trailing tool-role message may not survive litellm's Anthropic translation.
  `[plausible]`
- Parallel path logs `TOOL_CALL` after execution (event ordering only).
- Subagent fork inherits the parent's task string for summarization framing.
- `task_complete` alongside sibling tool calls returns immediately, leaving unanswered tool_calls in
  the transcript (not re-feedable).
- Retry log denominator/naming is off by one.

---

## Suggested fix order
1. **P0 #1 (SSRF)** + **P1 #4 (resume traversal)** + **P1 #3 (hook RCE)** — the security cluster,
   all small, all remotely reachable via `serve`.
2. **P1 #2/#5 (Seatbelt read + signal)** — the sandbox's security and usability are both broken;
   one profile edit fixes most of it. Add a Linux bwrap live test when a box is available.
3. **P1 #6/#7/#8 (verifier fail-open, stale-token trigger, rigorous plan-drop)** — correctness of the
   agent's own guarantees.
4. **P1 #9/#15 (MCP config crash, harbor leak)** + the eval/interface P2 cluster.
5. P2/nits opportunistically.
