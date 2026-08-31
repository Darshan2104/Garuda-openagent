# Security & Correctness Backlog — completion record

**Opened:** 2026-07-27 · **Closed:** 2026-07-27 · **Source:** the 15 numbered findings in
[2026-07-28-REVIEW_FINDINGS.md](2026-07-28-REVIEW_FINDINGS.md), re-verified
against the working tree, plus three defects
found while analysing a 50-task terminal-bench-pro run on the same date.

Every item was re-checked against current code before being classified. That mattered: the ledger
listed five items as open that were already fixed, and described #1 and #9 as worse than they were.
Tests: `tests/test_security_correctness_backlog.py`, `tests/test_gate_livelock_and_rewrite.py`.

---

## Status

| # | Item | Ledger said | Verified state | Outcome |
|---|---|---|---|---|
| 1 | SSRF in `web_fetch` | open (P0) | Partial — redirects unvetted | **T1 fixed** |
| 2 | Seatbelt reads every host file | open (P1) | Open, deliberate | **T2 accepted + surfaced** |
| 3 | Hook RCE from project settings | open (P1) | Already fixed | ledger corrected |
| 4 | Resume path traversal | FIXED | Fixed | — |
| 5 | Seatbelt denies `signal` | open (P1) | Open | **T3 fixed** |
| 6 | Verifier fails open on `**REJECTED**` | open (P1) | Already fixed | ledger corrected |
| 7 | Stale token count in condensation | open (P1) | Already fixed | ledger corrected |
| 8 | Rigorous drops approved plan | FIXED | Fixed | — |
| 9 | Malformed MCP config crashes run | open (P1) | Already isolated | **T4 fixed a different bug** |
| 10 | `bash_background` broken under bwrap | open (P1) | Open, unreproduced | **T5 refuses** |
| 11 | Docker timeout kills client | open (P1) | Already fixed | ledger corrected |
| 12 | Permission path rules bypassed | FIXED | Fixed | — |
| 13 | `allow_prefixes` bypasses ASK | open (P1) | Already fixed | ledger corrected |
| 14 | `chat --json` corrupts stdout | open (P1) | Open | **T6 fixed** |
| 15 | Harbor adapter MCP leak | FIXED | Fixed | — |
| 16 | Acceptance-contract livelock | *new* | Open | **T7 fixed** |
| 17 | `deadline_sec` never propagated | *new* | Open | **T8 fixed** |
| 18 | Whole-file rewrites over `edit` | *new* | Open | **T9 fixed** |

**13 fixed · 1 accepted (#2) · 1 fixed-by-refusing (#10) · 5 ledger corrections.**

---

## T1 — SSRF: redirects were never re-validated

The pre-existing `_ssrf_error` already rejected private/loopback/link-local/reserved addresses and
capped the read size. The hole that made the rest largely decorative: `urlopen` follows redirects and
only the caller's URL was checked, so an attacker-controlled page answering
`302 Location: http://169.254.169.254/…` reached cloud metadata through the *host-side* tool.

**Shipped.** `_ValidatingRedirectHandler` re-runs the guard on every hop and refuses the fetch if any
hop resolves non-publicly; hops capped at `MAX_REDIRECTS`. Unresolvable hosts now fail **closed** —
previously they returned `None` (allow), making "could not resolve" indistinguishable from "resolved
to something we'd reject". IPv4-in-IPv6 forms (`::ffff:…`, 6to4, Teredo) are unwrapped before
checking. Verified with a live redirect server.

**Deliberately not done:** proxies are left at urllib's default. An earlier draft disabled them; that
was wrong — a proxy is operator configuration, not model-controlled, so it is not an SSRF vector, and
disabling it breaks every deployment that needs one.

**Residual, documented:** the guard resolves the host and `urlopen` resolves it again, so a short-TTL
record can differ between the two (DNS rebinding). Closing it needs connect-to-pinned-IP with the
original Host header and SNI.

## T2 — Seatbelt host-file reads: accepted, now surfaced

Not fixed, and the reason is technical rather than neglect: Seatbelt has no working
allow-then-deny-subpath override for `file-read*`, and a read allowlist tight enough to deny `~/.ssh`
breaks basic exec because dyld needs broad, version-varying library access.

**Shipped.** The limitation is now visible at runtime, not only in source: selecting the Seatbelt
backend logs `READ_GAP_WARNING` once per process, and the README states the boundary explicitly.
macOS Seatbelt is a blast-radius reducer, not a confinement boundary; `docker` or `readonly` mode is
the answer when read confinement matters.

A partial deny-list of known secret directories would be cheap and would raise the bar, but needs
re-testing per macOS release. Left as a spike rather than shipped half-verified.

## T3 — Seatbelt `signal`: the finding's own fix was a no-op

`(allow process*)` does not cover `signal`, so `kill`, `timeout` and `make -j` failed with "Operation
not permitted".

**Shipped.** `(allow signal (target self) (target pgrp))` plus `(allow file-ioctl)` for pty-driven
tools.

**Correction worth recording:** the finding suggested `(allow signal (target self))`, which does
nothing here. Verified live on macOS 25.5 — with only `self`, `sleep 30 & kill $!` still fails;
`(target pgrp)` is the operative clause. `(target others)` is withheld, so a sandboxed command can
signal its own process group but never the agent or the host (`kill -TERM 1` is still refused, live).

## T4 — MCP config: the finding was already fixed; a worse bug was next to it

Per-entry isolation already worked — 3 valid + 1 malformed entry loaded the 3. My initial reading of
this finding was wrong.

**What was actually broken:** `args: hello` — the natural typo for `args: [hello]` — was iterated as
a *string* into `['h','e','l','l','o']`. The server launched with per-character arguments and failed
somewhere far from the cause, with no warning.

**Shipped.** `_string_list` coerces a scalar to a one-element list *with a warning*; `_string_map`
and genuinely wrong types (`args: 42`, `env: "str"`) raise a named `TypeError` caught by the existing
per-entry guard, so only that entry is skipped.

## T5 — `bash_background` under bwrap: refuse rather than lie

With `--unshare-pid --die-with-parent`, a backgrounded process is PID 1 of a namespace torn down when
the launcher returns, and the captured PID is namespace-local.

**Shipped.** `_unsupported_backend` refuses on the bwrap backend before spawning anything, naming the
cause and the alternatives (foreground `timeout`, or `--workspace-kind docker`). A clear refusal is
recoverable; a phantom task id the agent then polls is not.

**Honest limitation:** never reproduced — no bwrap host was available, so this rests on the namespace
semantics, not an observed failure. If a Linux box shows background tasks surviving, relax the guard
rather than keep a needless refusal.

## T6 — `chat --json` stdout purity

Header, `task>` prompt, thinking status, `on_done` and `Bye.` all printed to stdout alongside the
JSONL events, breaking any consumer piping stdout to a parser — the interface's entire purpose.

**Shipped.** `ChatRenderer` takes a `stream` (default stdout, so nothing changes outside JSON mode)
and every plain-backend write goes through `_out`. `chat_loop` selects stderr in JSON mode and routes
the prompt, blank lines, `Bye.` and the approval prompt there too. Asserted end-to-end by parsing
every stdout line.

## T7 — Acceptance-contract livelock

`harbor.yaml` enforced acceptance criteria without shipping the `contract` tool that is the only way
to discharge one, so every `task_complete` was rejected forever: 602 attempts across 50 tasks, 39 of
49 trials pinned at the turn cap, 8 killed by the wall clock, ~20% of all output tokens spent on
rejected completions. Tasks that had already solved the problem still livelocked.

**Shipped.** (1) `contract` added to the `harbor` and `build` allowlists. (2) The gate no longer
enforces a contract it cannot let the agent discharge — it warns, emits `contract/skipped`, and drops
it; the decision is memoised so a skipped run does not pay for another `derive_contract` call per
attempt. (3) Circuit breaker: after `CONTRACT_REJECT_LIMIT` (3) rejections naming the *identical*
outstanding set, the gate emits `gate_yield` once (latched) and lets verification run — a changed set
restarts the count, so a gate that is still steering is never cut off. (4)
`enable_acceptance_contract` became a profile field, and the parametrised invariant test caught the
same latent bug in **`build`, `explore` and `plan`**; `explore`/`plan` set it `false`, since a
read-only agent producing a report has no workspace evidence for criteria to check.

Chose *disable + warn* over auto-registering the tool (degrades to prior behaviour instead of
injecting a tool a profile may exclude deliberately), and *yield to verification* over terminating
(evidence, stability and LLM-verdict checks still stand — the run loses one guard, not all of them).

**Hardened during self-review.** The streak lives on `CompletionGateState`, and `gate` is an optional
parameter — so a caller passing `gate=None` got a breaker that could never count, i.e. the original
unbounded rejection loop. The contract is now simply not enforced without a gate, on the principle
that a gate which cannot guarantee termination should not run. Only one call site exists today and it
always passes a gate; the test pins the behaviour so that stays true.

## T8 — `deadline_sec` never reached the agent

Harbor enforces `override_timeout_sec` by killing the agent from outside and tells it nothing —
`AgentContext` is output-only — so the deadline machinery in `loop.py` and
`max_command_budget_fraction` were inert for the whole benchmark run. All 8 timeouts were hard kills
mid-work; one was cut off on a `write_file` at step 61.

**Shipped.** `agent_timeout_sec` (+ `deadline_margin`, default 10%) on the harbor adapter sets
`config.deadline_sec` below the harness timeout, so the agent's own wind-down runs before the kill
lands. Bad values are ignored with a warning rather than crashing the run. A generic `--deadline-sec`
flag was added too — a wall-clock budget is not an eval-only concern.

Set `agent_timeout_sec` to the same value as `override_timeout_sec` in job configs; harbor provides
no way to read it automatically.

## T9 — Whole-file rewrites instead of edits

`write_file` averaged 6,015 chars per call against opencode's 2,786 and was chosen over
`edit`/`multi_edit` more than twice as often — 112 calls consuming 29% of all tool-argument tokens.
Genuine agent behaviour, not a wiring bug.

**Cause.** `resolve_system_prompt` is `profile.system_prompt or DEFAULT_SYSTEM_PROMPT`, so a profile
with its own prompt discards **all** of the operating principles, including "prefer edit". The tool
description carried no guidance either.

**Shipped.** `WriteFileTool.description` now states the trade-off and names `edit`/`multi_edit`,
matching the symmetry `edit` already had. Tool descriptions are sent in the tools array on every
request (`loop.py:188`) **independently of the system prompt**, so a profile that replaces its prompt
cannot drop this — which is exactly the failure mode that caused the bias.

**Reverted during self-review — two things I got wrong:**

1. A runtime steer that pre-read the target on every `write_file` to report the changed-line count.
   `Environment.read_file` is unbounded, so overwriting a 40 MB file allocated **80 MB** to compute a
   hint that was then discarded (the 512 KB similarity cap ran *after* the read, protecting nothing).
   Measured, not theorised. A write that previously did zero reads should not read at all.
2. Its justification — "the tool description alone does not hold, a profile can replace the system
   prompt" — was simply false. Tool descriptions are not part of the system prompt. The description
   fix already achieves what the steer was added for, so the steer was redundant *and* harmful.
   `tests/test_gate_livelock_and_rewrite.py` now asserts `write_file` performs no read of its target.
3. An edit-preference line briefly added to `harbor.yaml`'s prompt: tuning the benchmark profile from
   benchmark traces is the benchmark-specific change the agent should not carry.

**Consequence to be honest about:** what ships for T9 is a tool-description change only. It is free
and safe, but its effect on behaviour is unmeasured — the same epistemic status the reverted steer
had, with none of the cost.

---

## Genericity

Audited and enforced by test: agent code (`core`, `tools`, `agents`, `model`, `workspace`, `context`,
`mcp`) never imports `garuda.eval`, and carries no benchmark task names. Benchmark adapters depend on
the agent; never the reverse. There are no hardcoded workspace or grader assumptions — `answer_check`
is a caller-supplied hook, and `bootstrap`/`evidence` reason structurally rather than about any task
set.

## Prompt: generic stays as-is, benchmark becomes configurable

`profile.system_prompt or DEFAULT_SYSTEM_PROMPT` means a profile with its own prompt replaces the
operating principles wholesale rather than overriding selected ones. That is *not* a defect for
normal use — `run`, `chat`, `serve` and `DefaultAgent()` all default to `build`, whose 37-line prompt
carries all eight principles. It only bites `harbor`, whose prompt is deliberately 4 lines.

**Resolved by making the prompt a benchmark variable rather than changing any profile** (2026-07-27):
`system_prompt`, `system_prompt_path`, `append_system_prompt` and `agents_dir` on the Harbor adapter.
Rationale: a harness score mixes scaffold quality with instruction quality, and separating them needs
the prompt swappable per job — not a better prompt baked into a shared profile, which would be the
benchmark tuning this codebase is meant to avoid.

Two details that matter:
- Overrides replace the profile's **base** prompt and re-run `resolve_system_prompt`, so the
  discovered-skills block and `AGENTS.md` memory are still appended. Overwriting the *resolved*
  prompt instead would have dropped both silently.
- An unreadable `system_prompt_path` raises. Falling back to the profile prompt would score a run
  that never used the prompt under test, which is the one failure a prompt experiment cannot tolerate.

The generic prompts are unchanged and pinned by test
(`test_generic_profiles_keep_their_own_prompts`). A composable prompt — shared principles plus a
profile preamble — remains a possible future change; it would alter behaviour for every profile, so
it stays a decision rather than something made silently.

**Standing caveat for any score quoted from the `harbor` profile:** it was produced with a 4-line
prompt, against baselines your notes record as using a hand-tuned ~50-line prompt plus four injected
skill docs. That asymmetry is now adjustable, but it applies to every number gathered before it was.

## Stale tests found and fixed (pre-existing, not from this work)

Three tests encoded the *old* completion contract — `task_complete` with a bare summary and no
verification command — which `require_discriminating_evidence` now correctly refuses. They were
already red in the working tree before this backlog was touched:

- `test_phase5.py::test_default_agent_exports_atif_compatible_events`
- `test_phase5.py::test_garuda_harbor_agent_run`
- `test_ablation.py::test_grading_catches_agent_that_lies`

The first two now supply a real discriminating command plus the criteria-extraction and verdict
responses the completion path actually requests — fixed by meeting the gate, not by weakening it. The
third asserted `agent_success is True` for an agent that lies; the gate now refuses that claim before
grading runs, so it asserts False and still checks the ground-truth verdict, which was its real point.

Worth a look: these are assertions about *your* gate's intended semantics, and the third in particular
records a genuine behaviour change.

## P2 backlog — worked through 2026-07-28

The P2/nits backlog is **closed**: 27 entries marked fixed, 10 deferred with a stated reason. Both
items previously flagged for promotion turned out already done (`VERIFICATION_COMMAND_TIMEOUT`
existed; `sessions.py` already used `os.replace` — though `runner.update_session_meta`, the path that
records *failed* status, did not, and now does).

**Ten of ~28 P2 items were already fixed and unmarked.** With the P0/P1 pass that makes 18 stale
entries in one ledger — the reason every item in this document was re-verified against code rather
than read off the list.

Highest-value fixes, by what they cost a real run:

| Fix | What it cost |
|---|---|
| Local exec discarded partial output on timeout | A build that burned its budget returned empty stdout; the agent retried blind instead of reading the error it had produced |
| Streaming collapsed index-less tool calls | Every parallel call merged into one slot — 2nd and 3rd calls vanished, arguments concatenated onto the first |
| Microcompaction never pruned tool-call arguments | A whole-file `write_file` stayed in context for the rest of the run (73% reduction now measured) |
| `int(timeout)` → `timeout 0s` in docker/remote/harbor | A sub-second budget was read as **no limit** — live-verified fixed (1.1s, rc=124) |
| Container `read_file` via login shell | Profile banners were prepended to file *contents* — live-verified clean against a planted banner |
| No server request-body ceiling | `Content-Length: 5000000000` allocated toward 5 GB **before the auth check** |
| Deny regexes evadable | `rm -fr /`, `rm --recursive --force /`, `dd of=/dev/sda if=…` all passed |
| `count_tokens` fallback ignored tool args | A full-file write estimated ~0 tokens, so the window overflowed instead of compacting |
| Summarizer transcript unbounded | 400 KB (~100k tokens) — the request meant to recover from overflow could itself overflow |
| tmux marker broke on `cmd &` | `cmd &; printf` is a syntax error, so the marker never printed and the poller waited out its full timeout |
| `harbor_environment` `duration_ms` hardcoded 0 | Every eval trajectory recorded each command as instantaneous |

Two regressions I introduced and caught in self-review, both the same failure mode — a fix stricter
than the thing it replaced:

- `write_file` pre-read the target to emit a hint: `read_file` is unbounded, so overwriting a 40 MB
  file allocated **80 MB** for advice it then discarded. Reverted; the guidance lives in the tool
  description, which no profile can drop.
- `_glob_regex` escaped `[`, making the post-filter stricter than the `find` prefilter it refines, so
  `src/[ab].ts` silently matched **nothing**. Character classes now pass through.

## Deferred, with reasons

Ten items remain, tagged `[DEFERRED]` in the ledger so they read as *examined and judged* rather than
*never looked at* — the distinction this ledger kept failing to make.

- **Eval-only tooling** (ATIF attribution, `dashboard.collect_rows`, `ablation`) — outside the agent.
  `harbor_environment` was the exception and is fixed: a timeout bug in the harness that runs the
  benchmark is not something to leave for scoping reasons.
- **`[plausible]`, not reproduced** (MCP anyio cancel-scope, consecutive USER messages, `cache_control`
  on a trailing tool message) — each needs a live provider or a race to confirm. Guessing at a fix for
  an unconfirmed fault is exactly how finding #5's suggested `(target self)` turned out to be a no-op.
- **Cosmetic** (`edit` snippet offsets on `\r\n`, subagent fork framing, retry-log denominator,
  `task_complete` alongside sibling calls) — wrong text in a message, no behavioural effect.

## Known residuals on fixed items

- `meta.json` is still read-modify-write without a lock; two concurrent writers can lose an update.
  Single-writer-per-session in practice.
- SSRF: DNS rebinding remains possible (guard resolves, `urlopen` resolves again). Needs
  connect-to-pinned-IP with the original Host header and SNI.
- Permission path rules cannot see a command's *results*, so a broad `grep '' .` still surfaces denied
  content. A guardrail, not a confinement boundary.
- Deny regexes stay best-effort: shell expansion and indirection beat any regex. The OS sandbox and
  `readonly` mode are the boundary.
- `pane_delta` still falls back to full scrollback on a prefix miss — a fidelity trade-off (more
  output, never less), not a defect.

## Before the next benchmark run

Set `agent_timeout_sec` in the job kwargs to match `override_timeout_sec`. Harbor enforces its
timeout by killing the agent and tells it nothing, so without this the agent still has no wall-clock
awareness and T8 buys nothing.

**Nothing here is validated against a real run.** 807 passing tests plus targeted per-mechanism
verification (including live Docker and live Seatbelt checks) is not the same as a scored benchmark.
