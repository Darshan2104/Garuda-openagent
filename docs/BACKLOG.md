# Open Backlog

**Living document. Residuals only.** An item leaves this file when it is fixed or
when the decision to accept it is recorded here as permanent. Closed work is not
kept — it is in [archive/](archive/), dated.

The rule that makes this useful: nothing here is marked done. If you fix it,
delete it. A ledger that mixes open and closed items cannot tell you what is left
without re-auditing the code, which is what the archived ledgers turned into.

Last verified against code: 2026-07-30.

---

## Accepted limitations

These are not going to be fixed as stated. They are here so nobody re-derives
them as new findings, and so no design assumes a guarantee that isn't there.

**Seatbelt reads every host file.** macOS Seatbelt has no working
allow-then-deny-subpath override for `file-read*` — an unfiltered
`(allow file-read*)` beats any later, more specific deny (verified empirically),
and a read allowlist tight enough to matter breaks ordinary commands. Seatbelt is
a blast-radius reducer, not a confinement boundary. **Docker is the real answer**
for untrusted work; `readonly` mode is the cheap answer. Surfaced at runtime, not
just in source. See `workspace/sandbox_policy.py`.

**Permission path rules are guardrails, not confinement.** Argument screening
cannot see a command's *results*, so a broad `grep '' .` still surfaces content a
path rule denies. Same class: deny regexes are best-effort — shell expansion and
indirection beat any regex. The OS sandbox and `readonly` mode are the boundary.

**`pane_delta` falls back to full scrollback on a prefix miss.** A fidelity
trade-off — more output, never less. Not a defect.

## Deferred, examined

Judged and set aside, not overlooked. Each needs something we don't have yet.

- **Unreproduced** — MCP client start/close cross-task cancel-scope hazard
  (anyio); possible consecutive USER messages (budget notice after a tool result);
  `cache_control` on a trailing tool-role message surviving litellm's Anthropic
  translation. Each needs a live provider or a race to confirm. Guessing at a fix
  for an unconfirmed fault is how a previous suggested fix shipped as a no-op.
- **Eval-only, outside the agent** — ATIF attribution name-matches before index;
  `dashboard.collect_rows` crashes on a non-numeric metric; `ablation` aborts the
  whole matrix when one task's setup fails.
- **Cosmetic** — `edit` snippet offset math on `\r\n` and pure deletions;
  subagent fork inherits the parent task string for summarization framing;
  retry-log denominator off by one; `task_complete` alongside sibling calls
  returns immediately, leaving unanswered `tool_calls` in the transcript.

## Open work

**Loop latency is now instrumented; nothing has been measured with it yet.**
`core/metrics.py` records per-turn model latency, tool latency, tool wall-clock,
compaction and checkpoint time, and cache-hit rate. The rollup is on
`AgentResult.metadata["metrics"]` and a `turn_metrics` event lands per turn, so the
wall-clock figures elsewhere in this file (945s → 1073s above, −16% below) no longer
have to be assembled by hand. Nothing in this file has been re-measured with it. Two
things to know before quoting a number from it:
- `model_ms` includes retry/backoff inside the client, on purpose — that is
  wall-clock the run spent — so a rate-limited run will show model time that is not
  model *compute* time.
- `parallel_saved_ms` is summed per-call durations minus segment wall-clock. It
  measures overlap, not end-to-end improvement; a run whose turn count moved has not
  been made faster just because this number is positive. The same trap as the
  `preserve_reasoning` cost prediction below.

**Two concurrency behaviour deltas, both deliberate, neither verdict-affecting.**
- *Agent loop.* A response's calls are now split into contiguous read-only /
  not-read-only segments (`loop.py::_segment_calls`), so one write no longer forces
  the whole response sequential. Contiguity is what makes it safe — a read before a
  write still runs before it. Fan-out is bounded by
  `AgentConfig.max_parallel_reads` (8); it was previously unbounded, so a response
  with twenty reads opened twenty at once.
- *Completion gate.* Contiguous runs of side-effect-free verification commands are
  gathered (`evidence.is_side_effect_free`, a fail-closed allowlist). Within such a
  group every command is screened and executed even if an earlier one fails, where
  the serial path stopped — so a *failing* group reports evidence for commands that
  previously would not have run. The rejection is still the first-in-order failure
  with the same feedback text, and every command in a group is a non-mutating
  reader, so no verdict can move. Commands that might write still run one at a time,
  and `_recheck_stability` is deliberately still serial: it re-runs exactly the
  discriminating commands (`pytest`, `make`, the deliverable), which are precisely
  the ones that are not side-effect free.

The honest size of the gate win: the allowlist excludes every assertion runner, which
is exactly what `require_discriminating_evidence` pushes the agent toward. On a
well-formed submission most commands still run serially. The unconditional win is the
two independent git reads in `gather_git_evidence`.

**Per-turn checkpoints are still O(n²) and still unmeasured.**
`sessions.py::checkpoint_messages` re-serialises and rewrites the whole transcript
every turn. Left alone deliberately: `eval/harbor_adapter.py` never passes
`checkpoint`, so benchmark runs pay none of it — this is an interactive/long-session
cost only. `checkpoint_ms` is now recorded, so the decision to replace it with an
append-only delta log plus periodic snapshot can be made on a number rather than on
the complexity argument.

**Docs onboarding path is new and unproven.** ARCHITECTURE → MODULES → this file
replaced a long status diary. If a contributor still can't get oriented from those
three, the gap is a bug in them.

**The posture flip is unmeasured.** `interactive` is now the default, so a plain
run no longer pays for the LLM judge, the acceptance contract, or stable
re-verification. How much *quality* that costs on a real task set has not been
measured — only that it costs less. Worth an ablation run (`eval_gates` variant
exists for exactly this comparison) before anyone concludes the cheap default is
free.

**Investigation depth is prompt-governed, and the prompt is load-bearing.**
Measured 2026-07-30 on 4 tasks, three prompt revisions of the same code:
adding batching guidance cut cost 67% *and* cut investigation 32% (grep went to
zero on all four tasks) — the agent read batching as permission to check less.
Rebalancing to lead with grounding recovered investigation (25 → 35 calls, grep
0 → 3) while keeping the batching win (contract calls 79 → 6; cost still 58%
below the un-batched baseline). Two things remain unresolved:
- **Reward did not follow.** `bash-ddos-traffic-analyzer` scored 1.0 un-batched,
  0.0 under both later prompts, and grounding did not bring it back. One trial
  each, in a suite that has flipped `build-coq` 1.0 → 0.0 on identical code, so
  this is not evidence either way — it is an unexplained regression that a
  repeated-trial run should either reproduce or dismiss.
- **Grounding costs wall-clock.** Output tokens rose ~44% on the two tasks that
  were already investigating enough (8.7k → 12.5k, 11.6k → 16.8k), taking total
  time on the 4 tasks to 1073s against 945s un-batched — i.e. the batching speed
  win is gone even though the cost win holds. Fine for a graded run, worth
  knowing before tuning this prompt for latency.
Anyone editing these prompts (`types.py:DEFAULT_SYSTEM_PROMPT`,
`agents/defaults/harbor.yaml`, `agents/defaults/build.yaml`) should assume a
cost-framed sentence will be read as a licence to skip work, and measure
investigation counts — not just cost — before and after.

**The final turn cannot force a commit.** Surfaced by the 2026-07-28 core-16 run:
`bash-tree-diff-sync` spent all 60 turns, never called `task_complete`, and the
run reported failure with correct work on disk. `steering.py` sets
`final_turn_forced` at the turn cap and delivers `FINAL_TURN_NUDGE`, but a nudge
is a message — nothing converts "budget exhausted, work done" into a completion
attempt, and the harness cannot submit on the model's behalf. Open rather than
obvious because a harness-issued completion has to decide what evidence it
carries; a gate that accepts an empty one is worse than the missing commit.

**The LLM verdict approves incorrect work.** Four false positives across the 16
tasks of that same run. One of them, `debug-bst-segfault-with-gdb`, is instructive:
after the contract-lockout fix the agent committed work the grader rejected and
the verdict passed it — the previous 1.0 there was a lucky workspace state under
a run that reported failure. The judge is the only gate that reads the task
statement back against observed output, so nothing downstream catches a false
positive; it is the run's answer. This is the gate `--mode eval` numbers rest on.

The next move on this is probably not more prompt text. A judge reads the task
statement back against observed output; what it cannot do is check the thing the
*grader* checks, which is where the falsification entry below lands too. The seam
already exists — `AgentConfig.answer_check(env) -> VerificationResult | None`,
consulted before the LLM verdict and returning None for "no opinion" — and it is
unused. A per-benchmark checker wired in there (output file present and parseable,
answer in the requested form, the domain invariant the task names) turns a judge
call into a decidable one for the tasks it covers, and costs nothing on the rest.
Raised independently in external review, 2026-07-31.

**`reasoning_effort` is a budget, not a floor — do not set it blind.** Measured
2026-07-31 on `bash-ddos-traffic-analyzer` with minimax-m2.5: asking for
`medium` *lowered* thinking against leaving it unset — per-turn reasoning mean
265 → 200 chars, peak on a single turn 1,644 → 719 — and the run went from 33
turns to the 60-turn cap at +160% cost. The effort levels impose a ceiling below
what this model spends unprompted. `harbor.yaml` therefore leaves it unset. Do
not assume `high` beats the default either; measure peak per-turn reasoning on
the target model before setting it at all.

**The reasoning echo is built, correct, and does not pay — `preserve_reasoning`
is off by default.** Non-Anthropic providers return a flat `reasoning_content`
that was captured, logged and dropped, so a reasoning model re-derived its
thinking every turn. It is now echoed back when the flag is on, confirmed
reaching the wire by prompt-growth arithmetic rather than by assumption, with the
cache holding (94.8% → 95.5%). Measured over 4 terminal-bench-pro tasks on
minimax-m2.5 against the same code with the flag off:

| | off | on |
|---|---|---|
| total reasoning chars | 43,082 | 42,628 (−1%) |
| reasoning per turn | 399 | 307 (−23%) |
| turns | 108 | 139 (+29%) |
| cost | $0.145 | $0.230 (+59%) |
| write_file | 8 | 15 |
| reward | 1/4 | 2/4 |

It did not make the model reason more — it spread the same reasoning over 29%
more turns. The 2/4 is one task (`bash-ddos-traffic-analyzer`) that this file
already records as flipping on identical code, so it is not evidence. Keep the
flag; do not turn it on without repeated trials. Three traps this cost us:
- **A one-task probe lied in both directions.** On `bash-ddos` alone the echo
  showed reasoning/turn +25% and the full-file rewrite loop converting into
  edits. Neither replicated: across 4 tasks reasoning/turn fell 23% and
  `write_file` nearly doubled (`sanitize-jinja2` went 1 → 10 rewrites). The same
  config also gave 49 and then 37 turns on the same task. Single-task deltas on
  this suite are noise; do not tune on them.
- **"It rides in the cached prefix so it is nearly free" was wrong by 20×.** The
  carry cost predicted +4.7%. Realized was +59%, because the prediction held the
  trajectory constant and the trajectory is exactly what changed. A cost argument
  about prompt *content* is only valid alongside a claim about turn count.
- **`reasoning_effort` is a ceiling, not a floor** — see the entry above.

**Prompt additions suppress investigation — this repo has now hit it twice.**
2026-07-30: cost-framed batching guidance cut investigation 32%, grep to zero.
2026-07-31: falsification guidance, carrying no cost framing at all, cut
investigation 31% and grep to zero on all four tasks. The common factor is not
the framing, it is that the prompt grew and the grounding paragraph was diluted.
Treat prompt length as a budget: adding a paragraph costs attention somewhere
else, and the cost lands on investigation. Anyone editing these must measure
investigation and grep counts before and after, and should prefer the shared
`task_complete` contract over a profile prompt when the guidance is about
finishing rather than about exploring. `tests/test_prompt_discipline.py` enforces
the placement rule and bans validation-task vocabulary from every prompt.

**Falsification framing works at the decision point, unevenly, and reward does
not follow.** The `task_complete` contract now asks for commands whose exit status
would change if the work were wrong. Measured over 4 tasks: deliberation appeared
at the completion step on 3 of 4 tasks (from 1 of 4) and grew 52%; the assertion
share of accepted evidence went 17% → 27%; `train-fasttext` replaced its
load-and-print oracle with three real asserts. Cost +17%, wall-clock **−16%**.
But it was not uniform — `advanced-json` produced *weaker* evidence (two
`execution` became one `execution` plus two `syntax`, one of them
`print(all(...))`, which is the print-instead-of-assert pattern with a computed
boolean) — and `bash-ddos` hit the turn cap. Reward stayed 1/4. Open questions:
- **Better evidence has not converted to reward anywhere.** `train-fasttext` now
  asserts its shapes, finiteness and n-gram sizes and still scores 0.0. The gate
  can force a check that *can* fail; it cannot make the agent check the thing the
  grader checks. That gap — self-chosen acceptance criteria versus actual ones —
  is where the remaining zeros live, and it is not an evidence-strength problem.
- **`print(all(...))` is a hole worth closing.** It computes the right predicate
  and then throws the answer away by printing it. The classifier correctly grades
  it `syntax`, so the gate discounts it, but the model reached for it anyway.

## Before the next benchmark run

- Set `agent_timeout_sec` in the job kwargs to match `override_timeout_sec`.
  Harbor enforces its timeout by killing the agent and tells it nothing, so
  without this the agent has no wall-clock awareness and the deadline plumbing
  buys nothing.
- Confirm the run is on `--mode eval`. The default posture is now `interactive`,
  which has no model-call gates. `eval/harbor_adapter.py` pins this itself, but
  any hand-rolled runner must ask for it explicitly.
- **Every score here is single-trial.** Graded runs now exist — core-16 and
  core-17, both 2026-07-28, plus the 4-task prompt comparisons of 2026-07-29/30 —
  so the earlier "nothing is validated against a real graded run" no longer holds.
  Nothing has been run twice, though, and the suite is demonstrably noisy:
  `build-coq-from-source` has flipped 1.0 → 0.0 on identical code, and
  `bash-ddos-traffic-analyzer` flipped across a prompt change with no mechanism
  that explains it. Treat any single-run delta, in either direction, as unmeasured
  until a repeated-trial run separates signal from noise.
