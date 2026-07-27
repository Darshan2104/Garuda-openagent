# Garuda — Self-Review Findings & Fix Backlog

**Date:** 2026-07-03 · **Scope:** all code written during the Phase A–F build (~8,300 lines).
Produced by four adversarial reviewers over disjoint areas; top items verified against the
running code. Severity: **P0** = security/data-loss in normal use · **P1** = security or
wrong behavior · **P2** = robustness/edge case · **nit** = cosmetic.

Verification tags: `[verified-live]` reproduced on this machine · `[verified-code]` confirmed
by reading the exact code path · `[reported]` reviewer finding, not independently reproduced ·
`[plausible]` reviewer flagged as uncertain.

Status tags: an item marked **[FIXED]** has a fix plus a regression test in the suite. Untagged
items are still open. Keep this ledger current when closing an item — an unmarked backlog can't
tell resolved from open without re-auditing the code.

---

## P0 — fix first

1. **SSRF in `web_fetch`.** **[FIXED]** `[verified-live]` `tools/web.py`. `validate_http_url`
   checked only scheme+netloc and `urlopen` follows redirects, so a prompt-injected page could make
   the host-side tool read `http://169.254.169.254/…` (cloud metadata), `http://127.0.0.1:8765/`
   (the local `serve` port) or `http://[::1]` and return it into the model/event log.
   **Fixed in two stages.** First: `_ssrf_error` resolves the host and rejects private, loopback,
   link-local, reserved, multicast and unspecified addresses; `MAX_FETCH_BYTES_CAP` and
   `TOTAL_FETCH_DEADLINE` closed the memory/hang gaps. Then (2026-07-27) the redirect hole, which
   made the rest largely decorative: `_ValidatingRedirectHandler` re-runs the guard on **every hop**
   and refuses the fetch if any hop resolves non-publicly, with hops capped at `MAX_REDIRECTS`.
   Unresolvable hosts now fail **closed**, and IPv4-in-IPv6 forms (`::ffff:169.254.169.254`,
   6to4, Teredo) are unwrapped before the check. Tests in
   `tests/test_security_correctness_backlog.py` (includes a live redirect-to-metadata server).
   **Residual, documented:** the guard resolves the host and `urlopen` resolves it again, so a
   short-TTL record can differ between the two (DNS rebinding). Closing that needs connect-to-pinned-IP
   with the original Host header/SNI. Proxy handling is left at urllib's default deliberately — a
   proxy is operator configuration, not model-controlled, so it is not an SSRF vector.

---

## P1 — security

2. **Seatbelt sandbox can read every host file.** **[ACCEPTED — documented limitation]**
   `[verified-live]` `workspace/sandbox_policy.py`. `(allow file-read*)` lets a sandboxed command
   `cat ~/.ssh/id_rsa`, `~/.aws/credentials` or `.env` and copy it into the host-persisted workspace,
   so env-scrubbing buys nothing against a determined prompt injection. bwrap does NOT have this gap
   (it ro-binds only `ro_paths` + workspace).
   **Why not fixed:** Seatbelt has no working allow-then-deny-subpath override for `file-read*` — an
   unfiltered `(allow file-read*)` beats any later, more specific deny (verified empirically) — and a
   read allowlist tight enough to deny `~/.ssh` breaks basic exec, because dyld needs broad library
   access in ways that vary by macOS version. A partial deny-list would be cheap but must be
   re-tested per OS release; that spike is not done.
   **What changed instead (2026-07-27):** the limitation is now surfaced at *runtime*, not only in
   source — selecting the Seatbelt backend logs `READ_GAP_WARNING` once per process, and the README
   states the boundary. Treat macOS Seatbelt as a blast-radius reducer, not a confinement boundary;
   use `docker` (or `readonly` permission mode) when read confinement matters.

3. **Hook RCE from a workspace `.garuda/settings.yaml`.** **[FIXED]** `[verified-code]`
   `plugins/hooks.py`. Project-level shell hooks ran on the host with no trust prompt, and
   `session_start` fires before the model does — so cloning a repo was enough to get host RCE.
   **Fixed:** project shell hooks are opt-in via `trust_project_hooks: true` in *global* settings
   (a repo cannot grant itself trust), documented as a trust boundary in `config.agent_home` and
   the README.

4. **Resume path traversal.** **[FIXED]** `[verified-live]` `core/sessions.py`. `resolve()` returned a
   `../..`-containing ref verbatim if the escaped dir existed; `load_messages` then read arbitrary
   `…/messages.json` into the LLM context. Exposed to remote clients via the server `resume` param.
   **Fixed:** `validate_session_ref` enforces a bare single path component, called from `session_dir`
   (the chokepoint every on-disk path derives from) as well as `resolve`. Tests in
   `tests/test_sessions_resume.py`.

## P1 — correctness

5. **Seatbelt denies the `signal` operation.** **[FIXED]** `[verified-live]` `sandbox_policy.py`.
   `kill` of a child returned "Operation not permitted", breaking `kill_task`, `timeout`, `make -j`
   and most test runners; `(allow process*)` does not cover `signal`.
   **Fixed:** `(allow signal (target self) (target pgrp))` plus `(allow file-ioctl)`.
   **Correction to this finding's suggested fix:** `(target self)` alone is a *no-op* here — verified
   empirically on macOS 25.5, `sleep 30 & kill $!` still fails with only `self`; `(target pgrp)` is
   the operative clause. `(target others)` is deliberately withheld, so a sandboxed command can
   signal its own process group but never the agent or the host (asserted live: `kill -TERM 1` is
   still refused).

6. **Verifier fails open on a formatted rejection.** **[FIXED]** `[verified-code]`
   `core/verifier.py`. `text.upper().startswith("REJECTED")` missed `**REJECTED**: …` (bold) and fell
   through to the "treating as approval" branch, accepting a rejected `task_complete`.
   **Fixed:** `parse_verdict` prefers a structured `{"verdict": …}` object, falls back to the first
   non-empty markdown-stripped line (`strip("#*_`> ")`), and returns `(None, "")` when neither shape
   is present — the caller then logs and **rejects**, so the gate fails closed.

7. **Condensation trigger uses a stale token count that lags a full turn.** **[FIXED]**
   `[verified-code]` `context/manager.py`. `note_usage` stored the last response's `prompt_tokens`
   and `append()` did not invalidate it, so newly-appended assistant + tool results (up to ~30 KB
   each, ×N for parallel reads) went uncounted until the next response — a big turn could overflow
   the window before microcompaction fired.
   **Fixed:** `append()` accumulates `_pending_tokens` per message and the estimate is
   `_last_prompt_tokens + _pending_tokens`, so the trigger reflects the current turn.

8. **Rigorous mode drops the approved plan (and repair feedback) when a context is threaded in.**
   **[FIXED]** `[verified-code]` fix is in **`core/rigorous.py`** (guard at the round loop); the cause
   is `core/loop.py`'s `if context is None:` construction branch, which seeds the task *only* when it
   builds the context itself. On resume / multi-turn chat / the rigorous executor, `exec_task`
   (task + plan) and per-round critic feedback never became messages the model saw. A partial fix
   covered repair rounds but kept an `attempt > 0` guard, so **round 0 still discarded the approved
   plan** — the plan phase was paid for and thrown away.
   **Fixed:** `rigorous.py` appends `current_task` under `if context is not None`, so every round —
   including round 0 — sees its effective task, and repair rounds see the critic feedback appended to
   it. `loop.py` is unchanged; looking for the guard there is a dead end (it cost one re-audit).
   Regression test in `tests/test_rigorous_repair.py` (verified to fail against the old guard).

9. **Malformed MCP config crashes the whole run.** **[FIXED]** `[verified-live]` `mcp/config.py`.
   An empty file or an entry missing `name` aborted loading before any server was tried.
   **Fixed:** the file parse is guarded (unparseable YAML → `[]` + warning, nothing salvageable), and
   `_dict_to_server_configs` parses **each entry under its own try/except**, so one bad definition
   skips only itself — verified: 3 valid + 1 malformed loads the 3.
   **Also fixed 2026-07-27**, found while testing the above: `args: hello` (the natural typo for
   `args: [hello]`) was iterated as a *string* into `['h','e','l','l','o']`, launching the server with
   per-character arguments and failing far from the cause. `_string_list`/`_string_map` now coerce a
   scalar to a one-element list with a warning and reject genuinely wrong types by name.

10. **`bash_background` is broken under the bwrap backend.** **[FIXED — refuses]** `[reported]`
    `tools/background.py`. With bwrap's `--unshare-pid --die-with-parent`, a backgrounded process is
    PID 1 of a per-exec namespace torn down when the launcher returns; the captured PID is
    namespace-local and meaningless to a later `task_output`/`kill_task`.
    **Fixed by refusing, not by working around it:** `_unsupported_backend` returns an error naming the
    cause and the alternatives (foreground `timeout`, or `--workspace-kind docker`) before anything is
    spawned. A clear refusal is recoverable; a phantom task id that the agent then polls is not.
    **Still unverified on Linux** — the underlying breakage was never reproduced (no bwrap host
    available), so the refusal is based on the namespace semantics rather than an observed failure.
    If a Linux box shows background tasks surviving, relax the guard rather than keeping a needless
    refusal.

11. **Docker/remote timeout kills the client, not the in-container process.** **[FIXED]**
    `[verified-code]` `workspace/docker.py`, `remote.py`. `process.kill()` stopped the local
    `docker exec` client while the workload kept running in the container, holding its memory/pid
    limits, and the tool reported exit 124.
    **Fixed:** the command is wrapped container-side with
    `timeout --kill-after=5s <n>s bash -lc …`, so it dies inside the container even if the client is
    torn down; the client timeout is set to `timeout + 15` so the container-side kill wins the race.

12. **Permission path rules are bypassed by command & search tools.** **[FIXED]** `[verified-live]`
    `core/permissions.py`. Path deny/ask rules applied only to `read_file`/`write_file`/`edit`;
    `bash`, `tmux_exec`, and `grep`/`glob`/`ls` (which call `env.execute` directly) were never path-
    checked, so `cat .env` / `ls secrets/` evaded a `**/*.env` deny rule.
    **Fixed:** `SEARCH_TOOLS` screens the search tools' path/pattern arguments, and
    `command_path_tokens` screens path-like operands of shell commands (falling back to a whitespace
    split rather than failing open on unparseable quoting). Tests in `tests/test_permissions_v2.py`.
    **Residual gap (documented on `PermissionEngine`):** argument screening cannot see a command's
    *results*, so a broad `grep '' .` still surfaces denied content. Path rules are a guardrail
    against casual access, not a confinement boundary — that's `readonly` mode or the OS sandbox.

13. **`allow_prefixes` bypasses ASK/deny on compound commands.** **[FIXED]** `[verified-code]`
    `core/permissions.py`. `allow_prefixes=["ls"]` made `ls; sudo reboot` allowed — it matched the
    `ls` prefix and skipped the `sudo` ASK pattern.
    **Fixed:** `_matches_allow_prefix` fast-paths a prefix only when the remainder contains no shell
    operators (`_SHELL_CHAIN_RE` covers `; | & newline < >`, `$(`, backticks); anything chained,
    redirected or substituted falls through to the deny/ask patterns.

14. **`chat --json` corrupts stdout with human text.** **[FIXED]** `[verified-live]`
    `interfaces/cli.py`, `interfaces/tui.py`. Header, `task>` prompt, "thinking" status, `on_done` and
    "Bye." all printed to stdout, interleaving with the JSONL events and breaking any consumer piping
    stdout to a parser — which is the interface's whole purpose.
    **Fixed:** `ChatRenderer` takes a `stream` (default stdout) and every plain-backend write goes
    through `_out`; `chat_loop` picks `stderr` in JSON mode and routes the prompt, blank lines,
    `Bye.` and the approval prompt there too. stdout is now one JSON object per line and nothing else.
    Asserted end-to-end by parsing every stdout line.

15. **Harbor adapter leaks an MCP manager and lacks try/finally.** **[FIXED]** `[verified-code]`
    `eval/harbor_adapter.py`. When a profile set `mcp_config_path` AND harbor supplied `mcp_servers`,
    the first `mcp_manager` was overwritten without `close()`; any exception in the run skipped
    `mcp_manager.close()`, the trajectory write, `events.save`, and temp-file cleanup — so a crashed
    eval task produced **no trajectory at all**, exactly when the transcript matters most.
    **Fixed:** the first manager is closed before rebuilding; the run body is wrapped in try/finally
    that closes the manager, unlinks the temp MCP yaml, and persists trajectory + events via
    `_persist_trajectory` before re-raising. The `--model` guard also moved ahead of
    `prepare_agent_run` so a misconfigured run can't leak a manager. Tests in `tests/test_phase5.py`.

---

## P2 — robustness & correctness edge cases

**Core loop / model:**
- Local exec timeout leaks grandchildren and discards partial output. `workspace/local.py`.
  **[FIXED]** Grandchild reaping was already done (`start_new_session=True` + `killpg`). The output
  loss was real and is now fixed: `wait_for(process.communicate())` *cancels* the readers on timeout,
  so everything the command had written was thrown away. The pipes are now read in their own tasks
  and drained after the kill, so a build that burned its budget and died still returns its error.
- `verification_commands` run with no timeout. `core/verifier.py`. **[FIXED — already]**
  `VERIFICATION_COMMAND_TIMEOUT` bounds both the first run and the stability re-run;
  `EVIDENCE_COMMAND_TIMEOUT` bounds the git evidence probes.
- Parallel batch aborted by one bad `ToolResult`. `core/loop.py`. **[FIXED — already]**
  `gather(..., return_exceptions=True)`, so one tool's failure no longer orphans the batch's
  tool_calls.
- `count_tokens` fallback ignores tool_calls args → severe undercount. `model/litellm_model.py`.
  **[FIXED]** The fallback now includes each tool call's name and arguments (JSON-encoded when not
  already a string). A full-file `write_file` used to estimate ~0 tokens; this estimate gates
  compaction, so undercounting meant the window overflowed instead of compacting. Tool *schemas* are
  still excluded — they are constant per run and cheap to approximate.
- Streaming: tool-call fragment merge collapsed calls when `index` is None.
  `model/litellm_model.py`. **[FIXED]** `_stream_deltas_from_chunk` coerced a missing index to 0 and
  `_merge_tool_fragment` keyed on it, so every index-less parallel call merged into one slot — the
  2nd and 3rd calls vanished and their arguments were concatenated onto the first. The index is now
  preserved as None and the slot is keyed by call id, falling back to the open call for a bare
  argument fragment. `_ordered_slots` avoids sorting a mixed int/str key set (which would raise).
  `max_retries<=0` was already handled (`attempts = max(1, ...)`).
- Microcompaction never prunes large tool-call *arguments*. `context/condenser.py`. **[FIXED]**
  A whole-file `write_file` carries the entire file in its arguments and outlived every prune, since
  only `Role.TOOL` contents were stubbed. `_prune_tool_call_arguments` now stubs oversized string
  *values* on assistant turns outside the recent window — keys, call id and name are kept, so the
  arguments object still validates against the tool schema on replay. Measured 73% reduction in
  tool-argument characters on an 8-turn write-heavy conversation; idempotent.
- Summarizer prompt can itself exceed context at the moment it's needed.
  `context/summarizer.py`. **[FIXED]** The per-message caps allowed 200 x 2000 = 400 KB (~100k
  tokens), so the request meant to recover from overflow could overflow. `MAX_TRANSCRIPT_CHARS`
  (60 KB) now bounds the whole rendered transcript, trimming oldest-first and noting how many
  messages were omitted so the model doesn't read it as complete history.
- `tool_rules: {x: ask}` silently downgraded to allow. `core/permissions.py`.
  **[FIXED — already]** `check_tool` returns the configured decision verbatim and
  `evaluate_tool_call` combines it with the command/path screen via `_strictest`, so a configured ASK
  reaches the approval handler and cannot be downgraded.

**Tools / workspace:**
- `glob` single-`*` over-matches across directories. `tools/search.py`. **[FIXED]** `find -path`
  treats `*` as matching `/`, so `src/*.ts` returned `src/nested/b.ts`. `find` stays the broad
  prefilter and `_glob_regex` applies shell semantics on the result (`*` -> `[^/]*`, `**/` -> any
  number of segments). Verified: `src/*.ts` -> one file, `src/**/*.ts` -> all depths including direct
  children, bare `*.ts` still matches anywhere (documented behaviour, unchanged).
  **Caught in self-review:** the first version escaped `[`, making the post-filter *stricter* than
  the `find` prefilter it refines — `src/[ab].ts` then matched nothing at all. Character classes
  (including `[!abc]` negation) are now passed through, and an unterminated `[` is treated as a
  literal, as the shell does.
- **[DEFERRED — cosmetic]** `edit` snippet offset math is wrong for `\r\n` and pure deletions.
  Affects only the confirmation message's line numbers, not the edit applied.
  `tools/edit.py`. `[reported]`
- `bash_background` `_TASKS` grows unbounded; `tail_bytes` unclamped. `tools/background.py`.
  **[FIXED]** `task_output` now drops a task's registry entry once the process reports EXITED
  (`reap_session` covers the normal end-of-run path, but a long-lived `serve` process never reaches
  it and accumulated dead entries for its lifetime; the log file stays on disk). `tail_bytes` is
  clamped to `[1, MAX_OUTPUT_BYTES]` and falls back on a non-numeric value — a model-supplied 1e9
  would otherwise pull an entire log into context.
- docker/remote `read_file` via login shell prepends profile banners to content.
  `workspace/docker.py`, `remote.py`. **[FIXED]** Both now `docker exec … cat <path>` through a new
  `_exec_argv` with no shell at all, so nvm/pyenv/MOTD chatter can't corrupt file contents and there
  is no quoting round-trip. **Also fixed alongside:** `timeout` was passed as `int(timeout)`, and
  `int(0.5)` is 0 — coreutils reads `timeout 0s` as *no limit*, so a sub-second budget silently
  became unbounded. Now `max(1, math.ceil(timeout))`.
- tmux marker breaks on a trailing `&` / unbalanced quote. `workspace/tmux.py`. **[FIXED]**
  `cmd &` is already a complete statement, so appending `; printf` produced `cmd &; printf` — a bash
  syntax error, meaning the marker never printed and the poller waited out its whole timeout.
  `build_marker_payload` now picks the separator from the command's tail (`&`, `;`, or neither), and
  `unbalanced_quotes()` lets a caller reject an unterminated quote up front rather than typing a
  command that can only hang. Verified against five command shapes.
  **Not addressed:** `pane_delta`'s full-scrollback fallback — it is a fidelity trade-off, not a
  defect (a wrong prefix yields more output, never less).
- `DockerLimits` emits bare `no-new-privileges`. `sandbox_policy.py`. **[FIXED]** Now
  `no-new-privileges:true`, the documented form. Some daemons accept the bare flag and others ignore
  it, so setuid escalation could remain available while the config claimed otherwise — a security
  control that fails quietly is worse than one that is absent.
- Regex deny/ask lists are evadable. `core/permissions.py`. **[FIXED — narrowed]** Recursive+force
  is now matched in any order and spelling (`-rf`, `-fr`, `-Rf`, `-f -r`, `--recursive --force`), and
  `dd` is matched on a `/dev/` *target* regardless of operand order, so `dd of=/dev/sda if=…` no
  longer slips past. Two false positives were removed while doing it: the blanket `dd if=` deny
  blocked ordinary file copies, and `git rm -r` (which stages a deletion rather than unlinking a
  tree) is now excluded. `chown` and `wget|sh` were added to the ask list.
  **Still best-effort by design** — a determined evasion (shell expansion, indirection) beats any
  regex. The OS sandbox and `readonly` mode are the boundary; this list is a guardrail.

**Interfaces / sessions:**
- Bearer token compared with non-constant-time `==`. `interfaces/server.py`.
  **[FIXED — already]** Uses `hmac.compare_digest`. The scheme is still matched exactly
  (`Bearer <token>`), so an unusual capitalisation still 401s — cosmetic, left as is.
- Hand-rolled HTTP: no Content-Length cap or read timeout; non-dict JSON dropped the connection.
  `interfaces/server.py`. **[FIXED]** The read timeout and the JSON-RPC parse-error reply were
  already in place. The body ceiling was not, and mattered most: a client-declared
  `Content-Length: 5000000000` had the server allocating toward 5 GB **before the auth check runs**,
  so it was reachable unauthenticated. Now capped at `MAX_REQUEST_BODY_BYTES` (32 MB) with a 413, and
  a non-numeric header returns 400 instead of raising out of the connection handler.
- `meta.json` writes are non-atomic. `core/sessions.py`, `runner.py`. **[FIXED]** `sessions.py`
  already used `os.replace`; `runner.update_session_meta` still did truncate-then-write and is the
  path that records the *failed* status, so a crash there left an unparseable session index. Now
  temp file + `os.replace`, and an already-corrupt meta.json is logged rather than raised.
  **Residual:** still read-modify-write without a lock, so two concurrent writers can lose an
  update. Single-writer-per-session in practice; a lock is the fix if that stops holding.
- Runner teardown failure skips `store.finish` + `on_session_end`. `interfaces/runner.py`.
  **[FIXED]** `cleanup_workspace` and `mcp_manager.close()` are each guarded, so a container that
  won't stop no longer prevents the session being marked finished or the session-end hook firing —
  the state you most need after a crash.
- A normal per-turn `Exception` in chat kills the session. `interfaces/cli.py`. **[FIXED]** The
  handler caught `BaseException` and re-raised everything. An ordinary error (provider 500, bad tool
  argument) is now rendered via `ChatRenderer.on_error` and the prompt returns; `KeyboardInterrupt`
  and `CancelledError` still propagate, because the user meant those. Losing a whole conversation to
  one bad turn is worse than the error.
- Resume seeds the *old* system prompt. `interfaces/runner.py`. **[FIXED]**
  `_with_current_system_prompt` swaps the replayed history's leading system message for the freshly
  resolved one (prepending if the history had none), so skills added since, an edited `AGENTS.md` or
  a changed profile take effect on resume. The conversation itself — including tool_call pairing — is
  preserved, and the caller's list is not mutated.
- Relative skill dirs resolve from CWD, not workspace. `agents/loader.py`. **[FIXED]** A relative
  `skills_dirs` entry now resolves against the workspace root when one is known. Under `serve` or the
  SDK the process cwd is not the workspace, so configured skills were looked for in the wrong place
  and silently found nothing. Absolute paths are untouched.

**Eval / observability:**
- **[DEFERRED — eval-only]** ATIF attribution tries name-match before index, so two same-named calls with no ids both bind to
  the first; success/`turns` "last-wins" drops turns in rigorous mode. `eval/atif_export.py`.
- MCP tool-name 64-char truncation can collide two tools to one name. `mcp/client.py`.
  **[FIXED — already]** Registration tracks exposed names and suffixes a collision with a counter,
  logging a warning, so a later tool cannot silently shadow an earlier one.
- `configure_tracing` is not idempotent. `observability/tracing.py`. **[FIXED]** A second call
  replaced `_PROVIDER` and abandoned the first, leaving its `BatchSpanProcessor` export thread alive
  with buffered spans it would never flush. The previous provider is now shut down before being
  replaced.
- `emit_spans_from_events` body isn't fully wrapped. `observability/tracing.py`. **[FIXED]** The
  public function now delegates to `_emit_spans_from_events` inside try/except and returns 0 on any
  failure. Telemetry is an observer: a malformed event must not fail the run that produced it.
- **[DEFERRED — eval-only]** `dashboard.collect_rows` catches a narrow exception set; a non-numeric metric crashes the table.
  `eval/dashboard.py:88`. `[reported]`
- `estimate_cost` doesn't apply the cache-read discount (overestimates cache-heavy runs).
  `eval/costs.py`. **[FIXED]** Cost now resolves in three tiers — provider-reported `usage.cost`,
  then `GARUDA_TOKEN_PRICES` overrides, then cache-aware litellm. Confirmed against real billing on
  2026-07-27: self-reported $4.6109 vs actual OpenRouter delta $4.61 over a 50-task run (0.05%).
  The old path billed every prompt token at the fresh-input rate, ~5x high on a 94%-cache run.
- `harbor_environment`: `int(timeout)` truncates sub-second to 0; `write_file` temp leaks on upload
  failure; `duration_ms` hardcoded 0. `eval/harbor_environment.py`. **[FIXED]** All three.
  `max(1, math.ceil(timeout))` — a 0-second budget is read as *no limit*, so a sub-second timeout was
  silently unbounded (the same defect fixed in docker.py/remote.py; leaving it in a third file after
  fixing two was the inconsistency). `write_file` uploads under try/finally, so a failed upload no
  longer leaves its payload in the host temp dir over a long eval run. `duration_ms` is measured
  rather than 0, which had made every eval trajectory read as instantaneous.
- **[DEFERRED — eval-only]** ablation: task `setup` outside try aborts the whole matrix on one failure; rigorous variant
  under-reports tokens (only build sub-agent usage); `_base_config` setattr accepts typo'd keys.
  `eval/ablation.py`. `[reported]`
- **[DEFERRED — unreproduced]** MCP client start/close cross-task cancel-scope hazard (anyio).
  `mcp/client.py`. `[plausible]` Needs a live server and a cancellation race to confirm; a
  speculative anyio fix risks breaking the working path.

---

## nits
- **[DEFERRED — unreproduced]** Possible consecutive USER messages (budget notice + 80% warning after a tool result; task+summary
  after rebuild) — verify Anthropic doesn't 400 on non-alternating roles. `[plausible]`
- **[DEFERRED — unreproduced]** `cache_control` on a trailing tool-role message may not survive litellm's Anthropic translation.
  Needs a live Anthropic call to observe; the cache hit rate measured 94.6%, so no symptom so far.
  `[plausible]`
- Parallel path logs `TOOL_CALL` after execution. **[FIXED]** `TOOL_CALL` is now emitted at
  dispatch, before `gather()`. Trace spans and JSONL consumers derive ordering from these events, so
  a parallel batch previously looked as though it had executed before it was requested.
- **[DEFERRED — cosmetic]** Subagent fork inherits the parent's task string for summarization framing.
- **[DEFERRED — cosmetic]** `task_complete` alongside sibling tool calls returns immediately, leaving unanswered tool_calls in
  the transcript (not re-feedable).
- **[DEFERRED — cosmetic]** Retry log denominator/naming is off by one.

---

## Tooling

Lint and tests are enforced by `.github/workflows/ci.yml` on every push and PR. The ruleset is
pinned in `[tool.ruff.lint]` (`E4`/`E7`/`E9`/`F` + `I` + `B`) — before that, lint findings silently
reaccumulated between cleanup commits because nothing held the line. `E501` is intentionally off.

Live Seatbelt/bwrap tests run in a separate **non-blocking** CI job: they vary by OS image version
(the reason they were made opt-in in `029fa77`), so they stay visible without gating every PR.

## Status (2026-07-27)

All fifteen numbered findings are closed: **13 fixed**, **#2 accepted** as a documented Seatbelt
limitation with a runtime warning, **#10 fixed by refusing** the unsupported backend (its underlying
breakage is still unreproduced on Linux — see the entry).

Two defects found during the same re-audit are tracked in
[SECURITY_CORRECTNESS_TODO.md](SECURITY_CORRECTNESS_TODO.md) as #16–#18, all fixed: the
acceptance-contract livelock, the un-propagated `deadline_sec`, and the whole-file-rewrite bias.

The **P2 backlog below was then worked through** (2026-07-28). Of ~28 items, 10 turned out already
fixed and unmarked, 18 were fixed, and the rest are recorded as deferred with a reason. Both entries
previously flagged for promotion are closed: `verification_commands` already had
`VERIFICATION_COMMAND_TIMEOUT`, and `meta.json` is now written via temp-file + `os.replace` on both
paths (`sessions.py` already was; `runner.update_session_meta` — the one that records *failed* status
— was not).

**Deferred, and why:**
- **Eval-only tooling** (ATIF attribution, `dashboard.collect_rows`, `ablation`): out of scope for
  this pass, which was limited to the agent itself. `harbor_environment` was the exception and is
  fixed — it shared the `int(timeout)` → `0s` bug, and a real timeout bug in the harness that runs the
  benchmark is not something to leave for scoping reasons.
- **`[plausible]` items not reproduced** (MCP anyio cancel-scope, consecutive USER messages,
  `cache_control` on a trailing tool message): each needs a live provider or a race to confirm, and
  guessing at a fix for an unconfirmed fault is how the `(target self)` no-op in #5 happened.
- **Cosmetic** (`edit` snippet offsets on `\r\n`, subagent fork framing, retry log denominator):
  wrong text in a message the model reads past; no behavioural effect.

**Process note.** Five findings (#3/#6/#7/#11/#13) were fixed in code without the ledger being
updated, and re-auditing to discover that cost more than the updates would have. Keep to the rule the
header states: **the ledger entry changes in the commit that changes the code.**
