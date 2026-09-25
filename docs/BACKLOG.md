# Open Backlog

**Living document. Residuals only.** An item leaves this file when it is fixed or
when the decision to accept it is recorded here as permanent. Closed work is not
kept — it is in [archive/](archive/), dated.

The rule that makes this useful: nothing here is marked done. If you fix it,
delete it. A ledger that mixes open and closed items cannot tell you what is left
without re-auditing the code, which is what the archived ledgers turned into.

Last verified against code: 2026-08-04.

**The measurement constraint, stated once because it governs the whole file.**
There is no budget for repeated-trial (multi-seed) benchmark runs. Every score
this repo has is single-trial, on a suite that has flipped `build-coq-from-source`
1.0 → 0.0 on identical code and `bash-ddos-traffic-analyzer` across a prompt change
with no mechanism that explains it. So "run it again and see" is not an available
next step for anything, and a plan whose only evidence would be a reward delta is
not a plan. What this leaves is work that is justifiable without a reward
measurement: a missing gate, an unwired seam, a lost submission, a malformed
transcript — each provable by unit test. Everything that was open here *pending
measurement* has therefore been accepted as permanent below rather than left
looking actionable.

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

**A file tool cannot see CRLF through a local workspace.**
`LocalEnvironment.read_file` goes through `Path.read_text`, whose universal-newline
translation strips `\r` before any tool sees it; CRLF reaches a tool only from an
Environment that preserves it (a container `cat`). So byte-exactness in the file tools
is testable at unit level and effectively untestable through a local Environment —
which is why a snippet-offset drift lived there as long as it did, and it applies to
any other byte-exactness assumption in `tools/`. Recorded as permanent because the fix
is a second read path that exists only in order to be tested.

**The judge cannot check what the grader checks.** The LLM verdict reads the task
statement back against observed output; it has no access to the benchmark's own
oracle, so a run can satisfy every self-chosen criterion and still be graded
wrong. The *decidable* part of this gap is now closed by the deliverable check
below. What remains — is this number right, is this the algorithm asked for — is
judgement, and the judge is the run's answer for it. Recorded as permanent because
the alternative (a per-benchmark oracle mirroring each grader) is the benchmark's
job, not the agent's.

**Three unreproduced hazards, each needing a live provider or a race.** MCP client
start/close cross-task cancel-scope hazard (anyio); possible consecutive USER
messages (a budget notice landing after a tool result); `cache_control` on a
trailing tool-role message surviving litellm's Anthropic translation. Guessing at
a fix for an unconfirmed fault is how a previous suggested fix shipped as a no-op,
and none of the three has a local reproduction. Left alone deliberately.

**Per-turn checkpoints are O(n²), permanently.** `sessions.py::checkpoint_messages`
re-serialises and rewrites the whole transcript every turn.
`eval/harbor_adapter.py` never passes `checkpoint`, so benchmark runs pay none of
it — this is an interactive/long-session cost only. `checkpoint_ms` is recorded if
anyone ever wants the number, but replacing it with an append-only delta log plus
periodic snapshot is not happening on the strength of a complexity argument alone.

**Two deliberate concurrency deltas, neither verdict-affecting.** Recorded so
neither reads as a bug later. *Agent loop*: a response's calls are split into
contiguous read-only / not-read-only segments (`loop.py::_segment_calls`), bounded
by `AgentConfig.max_parallel_reads` (8), so one write no longer forces a whole
response sequential and twenty reads no longer open twenty at once. *Completion
gate*: contiguous runs of side-effect-free verification commands are gathered
(`evidence.is_side_effect_free`, a fail-closed allowlist), so a *failing* group
reports evidence for commands the serial path would not have reached — the
rejection is still the first-in-order failure with the same text, and every command
in such a group is a non-mutating reader. `_recheck_stability` stays serial on
purpose: it re-runs exactly the discriminating commands (`pytest`, `make`, the
deliverable), which are precisely the ones that are not side-effect free. The
honest size of the gate win is small — the allowlist excludes every assertion
runner, which is what `require_discriminating_evidence` pushes the agent toward.

**Three unmeasured cost/quality trades, accepted as they stand.** Each was left
open pending a measurement that is not coming:

- *Loop latency.* `core/metrics.py` records per-turn model latency, tool latency,
  tool wall-clock, compaction, checkpoint time and cache-hit rate; the rollup is on
  `AgentResult.metadata["metrics"]` with a `turn_metrics` event per turn. Nothing in
  this file has been re-measured with it, and nothing will be. Two traps if you ever
  read a number off it: `model_ms` includes retry/backoff inside the client on
  purpose (it is wall-clock the run spent, not model *compute*), and
  `parallel_saved_ms` is summed per-call durations minus segment wall-clock — it
  measures overlap, not end-to-end improvement, so a run whose turn count moved has
  not been made faster just because that number is positive.
- *The posture flip.* `interactive` is the default, so a plain run pays for no LLM
  judge, no acceptance contract, no stable re-verification. It costs less; how much
  *quality* that costs on a real task set is unknown. The `eval_gates` ablation
  variant exists for the comparison if a budget ever does.
- *`preserve_reasoning` stays off.* The echo is built and confirmed reaching the
  wire (prompt-growth arithmetic, cache holding 94.8% → 95.5%), and over 4
  terminal-bench-pro tasks on minimax-m2.5 it did not make the model reason more —
  it spread the same reasoning over 29% more turns: total reasoning chars 43,082 →
  42,628 (−1%), reasoning per turn 399 → 307 (−23%), turns 108 → 139 (+29%), cost
  $0.145 → $0.230 (+59%), `write_file` 8 → 15, reward 1/4 → 2/4 (and that 2/4 is
  `bash-ddos-traffic-analyzer`, which flips on identical code). Turning it on
  requires repeated trials to justify, which is the one thing unavailable, so the
  flag stays off. Keep the code: it is correct, and the finding is the point.

**`readonly` cannot run a read-only `git`.** The mode screens `bash` with
`evidence.is_side_effect_free`, whose allowlist has no `git` — correctly, because
`git commit`/`checkout` write and even `git status` refreshes `.git/index`. So a
`reviewer` run cannot `git diff` or `git log`, which is the natural tool for the job;
it reads files with `read_file`/`grep` instead. Not fixed by widening that allowlist:
it is shared with the completion gate's decision about what may run *concurrently*,
where admitting an index-writing command reintroduces exactly the race it exists to
prevent. A separate readonly-only allowlist of `git log`/`show`/`diff` would work and
is deliberately not built yet — one more list to keep correct, for a gap with a
workaround.

**`is_side_effect_free` reads a quoted `>` as a redirect.** `split_segments` is
quote-aware, but `_MUTATING_SHELL` is applied to the raw segment, so
`echo "cat a > b"` is judged unsafe though it writes nothing. Conservative in the
direction the function must fail, and pinned by test in both directions
(`tests/test_harness_robustness.py`) so nobody narrows the match to unquoted
operators and admits a real redirect. The cost is one rephrase by the agent.

**A profile's `tools:` list no longer filters a tool the caller supplied.** Recorded
because it is a deliberate behaviour change, not an oversight: `register_tool` and
`.agent/tools` are both explicit acts, and filtering them made both features silent
no-ops on every shipped profile (`build` names 27 tools, so nothing a user added ever
matched). Restriction of *discovered* tools is unchanged. If a profile ever needs to
refuse a caller-supplied tool, that wants a new opt-in field, not the reinstatement of
this filter.

**mcp 2.0 is not supported, and the bound says so.** 2.0 renamed
`streamablehttp_client` to `streamable_http_client` *and* changed its signature:
`headers` and `timeout` are gone, replaced by a caller-built `http_client`. So a
rename shim does not do it — the HTTP transport needs a second construction path.
`pyproject.toml` therefore pins `mcp>=1.9.0,<2`. It is not written because supporting
2.0 is hard; it is written because there is no local HTTP MCP server to verify a 2.0
path against, and the fixture is stdio. Raise the bound when there is something to
test it with, not before.

Worth knowing how this was found, because the gating CI job cannot find it: `test`
installs with `-c constraints.txt` (mcp pinned at 1.19.0), so the version a *user*
resolves is never what the gate exercises. A clean clone plus the README's own
`pip install -e ".[dev]"` pulled mcp 2.0.0, and `import garuda.mcp.client` — on the
path of every run, MCP configured or not — raised ImportError. The harness could not
execute one task, and five test modules failed to collect. `latest-deps` exists for
exactly this and is deliberately non-blocking, so it warns and nothing stops. The
lesson is in the standing rules below: install the way the README says, in a clean
environment, before calling a release good.

## Standing rules that came out of measurement

Not residuals — constraints. Each cost a run to learn, and violating one silently
undoes work already paid for.

**A pinned suite says nothing about what a user installs.**
The gate installs with `-c constraints.txt` on purpose — a failure there is this
repo's fault and not an upstream release's — but the consequence is that the
dependency *bounds* in `pyproject.toml` go untested, and a bound is a claim about
what works. It was wrong (`mcp>=1.9.0` admitted a 2.0 that breaks on import), and a
clean-venv install was the only thing that could say so. Two timing-dependent tests
in `test_robustness_e2e.py` surfaced the same way: they asserted a launched process
had written its pidfile before the sweep killed it, which is an ~80ms race the warm
interpreter happened to win and a cold venv lost 5/5. Run the documented install in a
throwaway environment as part of finishing, not as a courtesy.

**A green suite says nothing about the wiring. Drive the commands.**
2026-08-04: eight defects were found by running the documented interfaces end to end
against a live model, with the suite passing throughout. Six were reachable from a
single documented command, and three were total: `--mode eval` died with a provider 400
on its first `task_complete` (a USER-role note between an assistant `tool_calls` block
and its results), `--mode rigorous` raised `TypeError` before its first turn
(`RigorousAgent.run` had not tracked a signature change in `DefaultAgent.run`), and
`read_pdf`/`read_spreadsheet` were dead on every macOS host (`base64` with a positional
file, which BSD does not accept). The pattern in all three: each unit was tested and
correct, and nothing tested the seam between them. `--mode readonly` was worse than
broken — it inverted, leaving `--agent harbor` on `yolo`. Before trusting a posture or
an extension point, run it once and read the trajectory; a unit test on either side of
a seam will not tell you the seam is open.

**Prompt additions suppress investigation. This repo has hit it twice.**
2026-07-30: cost-framed batching guidance cut investigation 32%, grep to zero on
all four tasks. 2026-07-31: falsification guidance carrying no cost framing at all
cut investigation 31%, grep to zero on all four tasks. The common factor is not the
framing — it is that the prompt grew and the grounding paragraph was diluted. Treat
prompt length as a budget: a new paragraph costs attention somewhere else, and the
cost lands on investigation. Rebalancing to lead with grounding recovered it
(25 → 35 calls, grep 0 → 3) while keeping the batching win (contract calls 79 → 6,
cost still 58% below the un-batched baseline), so the recovery is known to work.
Anyone editing `types.py:DEFAULT_SYSTEM_PROMPT`, `agents/defaults/harbor.yaml` or
`agents/defaults/build.yaml` must measure investigation and grep counts before and
after — not just cost — and should prefer the shared `task_complete` contract over a
profile prompt when the guidance is about finishing rather than exploring.
`tests/test_prompt_discipline.py` enforces the placement rule and bans
validation-task vocabulary from every prompt.

**`reasoning_effort` is a budget, not a floor — do not set it blind.** Measured
2026-07-31 on `bash-ddos-traffic-analyzer` with minimax-m2.5: asking for `medium`
*lowered* thinking against leaving it unset — per-turn reasoning mean 265 → 200
chars, single-turn peak 1,644 → 719 — and the run went from 33 turns to the 60-turn
cap at +160% cost. The effort levels impose a ceiling below what this model spends
unprompted, so `harbor.yaml` leaves it unset. Do not assume `high` beats the
default either; measure peak per-turn reasoning on the target model before setting
it at all.

**A cost argument about prompt content is only valid alongside a claim about turn
count.** The `preserve_reasoning` carry cost was predicted at +4.7% and realized at
+59% — wrong by 20× — because the prediction held the trajectory constant and the
trajectory was exactly what changed. The same trap sits under `parallel_saved_ms`.

**Single-task probes on this suite lie in both directions.** On `bash-ddos` alone
the reasoning echo showed reasoning/turn +25% and the full-file-rewrite loop turning
into edits; across 4 tasks reasoning/turn fell 23% and `write_file` nearly doubled.
The same config gave 49 and then 37 turns on the same task. Do not tune on one task.

## Open work

**ACP authority is recorded, not enforced.** `acp/authority.py` negotiates one
owner per tool family from `families`/`mediated`/`sandbox`, which are Garuda
extension fields in `agentCapabilities` — standard ACP v1 agents send none, and
the sandbox flag is the agent's own unverified claim. Garuda advertises
`clientCapabilities: {}`, so a v1 agent runs its own edits and commands; nothing
routes them through Garuda even where the map says `garuda`. Enforcing it means
advertising `fs`/`terminal` client capabilities, serving those methods through
the broker, and deriving ownership from what was advertised rather than
declared.

**Restart recovery has no production ACP or handoff caller.** `recover()` reaps
only children that `AcpRuntime(store=…)` recorded, and only
`HandoffTransaction(store=…)` records switch cancels, but no entry point builds an
`AcpRuntime` or calls `execute_handoff` yet, so today the reaping path runs only in
tests. Wiring the first ACP entry point must pass the session store (the adapter
warns when it has none). The `cancellations` audit list is also not consumed by
classification yet.

**Interactive sessions take no workspace lease.** Only `run_agent_task` acquires
the mutating lease. Dashboard chat (`interfaces/web/live.py`), CLI chat, and the
SDK `Conversation` call `agent.run` directly, so they can interleave with a
leased run on the same workspace.

**SDK `Conversation`, `recipe run`, and eval runners carry no workspace
baseline.** They persist no session (`sdk/conversation.py`,
`config/recipes.run_recipe` via `interfaces/main.run_recipe_command`,
`eval/harbor_adapter.py`, `eval/ablation.py`, subagents in `core/subagent.py`),
so they never enter
`workspace/evidence.begin_session_evidence`: their verifier gets no
`workspace_delta_loader` and nothing records what the run changed versus
preexisting dirt. Fix by giving them a persisted session (or an explicit
in-memory evidence store) and routing them through the same boundary, with a
refusal test per entry point.

**Handoff and resume do not carry the session delta forward.** No production
caller passes `workspace=` to `runtime/handoff.execute_handoff`, so product
handoffs carry no delta. `--resume` starts a new session with a fresh baseline,
so the prior session's work is attributed as preexisting dirt.

**The dashboard still parks approvals outside the P0.17 broker.**
`garuda.acp.broker.ApprovalBroker` is installed only by `run_agent_task`
(`interfaces/runner.py`). Dashboard chat (`interfaces/web/live.py`) builds its own
`interfaces/web/approvals.ApprovalBroker` and runs turns through `agent.run`
directly, so browser allow/deny/timeout outcomes are not persisted as
`approval:<id>` session records. `ApprovalBroker.decide_acp()` is exercised only by
tests; no ACP adapter screens terminal/edit requests through it yet. The fix is to
keep the web module as a transport (thread marshalling, heartbeat, structured
arguments) over the shared broker, and to route ACP family requests through
`decide_acp` before execution, with an integration test on each path.

**The deliverable check is new and its hit rate is unknown.**
`eval/answer_checks.py` extracts the output files a task statement unconditionally
asks for and rejects a completion when one is missing, or not the format its name
claims; the Harbor adapter wires it in per task. It is advisory
(`authoritative = False`), so it can only reject and never approve — the judge still
decides everything else. Per the measurement constraint, how often it fires on a real
task set will stay unknown. The mitigations are that it is switchable
(`enable_deliverable_check=False` on the Harbor agent) and that every rejection names
the file and the reason, so a wrong one is obvious in the trajectory rather than
buried in a verdict.

What to know before touching the extraction. **A false rejection fails a run whose
work was fine** — strictly worse than the false positive the check exists to catch —
so every rule in it is biased toward silence, and review of the first cut found five
false-rejection bugs and three silent-suppression bugs in a hundred lines of regex. Both
classes come from the same place: a marker's *scope*. `if`/`unless` hedge their whole
sentence; the temporal set (`when`, `whenever`, `once`, `after`, `while`, `for each`)
and the exemplifying set (`e.g.`, `for example`) hedge only what *follows* them, so
position decides whether they apply at all; a marker of *either* kind in a fragment
ending in `:` carries across the list it introduces and resets at a blank line. That
last clause is the one that keeps getting missed: the defect appeared three times — the
splitter
breaking on `:`, then on `e.g.`'s own full stop, then the carry keying on the
sentence-wide hedges only — each time turning `<condition>:` plus a bulleted list into
unconditional requirements. `tests/test_backlog_clearing.py` pins 39 phrasings, and
anything changed here needs a phrasing added to that battery in both directions — a
statement that must yield the path, and one that must not. A battery with only one
direction is how the `for example` case passed for the wrong reason for two rounds.

Four known extraction limits, all failing toward silence rather than rejection, all
judged not worth the false-rejection risk of fixing:
- **A temporal marker ahead of the verb drops the requirement.** `After you finish,
  write the version to VERSION.txt` → `[]`, likewise for `when`/`once`/`while`.
  Ordering-then-requirement is ordinary phrasing, so this is likely the broadest of the
  four — unmeasured, like the hit rate itself. It is the deliberate price of reading
  `When a mismatch is found, write it to diff.txt` as conditional, which it is: nothing
  in the surface form tells the two apart. The `:` form of the same statement is
  silenced by the carry rule below rather than by position, so the two punctuations
  agree instead of one of them extracting. Pinned in `test_requirement_phrasings`.
- **One verb governing two paths yields only the first.** `Write results to
  /app/out.json and a log to /var/log/run.log` → `['/app/out.json']`; the requirement
  pattern needs a verb per path and `finditer` is non-overlapping. Scanning on past a
  match for more paths would just as happily pick up an *input* file.
- **The `:` carry over-extends without a blank line.** `If errors exist:\n- write them
  to errors.txt\nWrite the summary to summary.json` → `[]`, and the same for a temporal
  marker in place of the `if`. Markdown normally wants the blank line, and reading a
  bullet list as ended by anything less is guesswork. Note what it costs: the
  suppression is not confined to the list, so a requirement in trailing prose goes with
  it — the one case where the `:` form silences *more* than the comma form does.
- **Only `.json` treats an empty file as malformed.** Every other parseable suffix has
  a legitimate empty reading (zero records, null document, zero rows), and a plain
  `.txt` is not checked for content at all.

**The forced final submission is a mechanism, not a measured win.** A run that
spends its budget without submitting now gets one extra exchange in which
`task_complete` is the only tool available (`loop.py::_final_submission`,
`AgentConfig.force_final_submission`). This closes the `bash-tree-diff-sync` shape —
60 turns spent, correct work on disk, never submitted — and it deliberately does not
submit *for* the model: the evidence is the model's own, run by the ordinary gate, so
a fabricated command fails exactly as it would have earlier, and an empty one is
still refused under `require_discriminating_evidence`. What is unknown is how often
the model takes the offer and how often that submission is accepted. Costs one model
call, and only on a run that has already failed.

**Better evidence still has not converted to reward.** Falsification framing
demonstrably improved the *evidence*: deliberation appeared at the completion step
on 3 of 4 tasks (from 1 of 4) and grew 52%, the assertion share of accepted evidence
went 17% → 27%, `train-fasttext` replaced its load-and-print oracle with three real
asserts, cost +17% and wall-clock −16%. Reward stayed 1/4, and `train-fasttext`
still scores 0.0 while asserting its shapes, finiteness and n-gram sizes. Nor was the
gain uniform: the same change made `advanced-json` produce *weaker* evidence (two
`execution` became one `execution` plus two `syntax`), and `bash-ddos` hit the turn
cap. The gate can force a check that *can* fail; it cannot make the agent check the
thing the grader checks. With the decidable part of that gap now handled by the
deliverable check, what is left here is the judgement part — see the accepted
limitation above — and it is not an evidence-strength problem. Recorded as open only
because it is the gap that matters most, not because there is a known next move.

**Two event-log residuals left after the observability pass.** `session_start` now
carries the resolved gate stack, the four turn-scoped events carry `turn`,
`permission_ask` names the call it blocked, `summarization` says whether it pruned or
summarized, and `metrics`/`mode`/`acceptance` reach `meta.json`
(`tests/test_run_observability.py`). Two things a trajectory still cannot see:

- **`tool_result` is overloaded.** The steering failure-streak marker reuses it with a
  completely different payload (`{failure_streak, steered, turn}`, no `tool_call_id`),
  so a consumer filtering `type == "tool_result"` sees a phantom result. Fixing it
  properly means a new `EventType.STEERING`, which changes the log vocabulary and would
  strand readers of the ~16 existing harbor job trees; readers must discriminate on the
  absent `tool_call_id` either way, so the wart is cheaper than the migration. Five more
  steering note kinds (`CONTINUE_NUDGE`, `REPEAT_NUDGE`, `TASK_COMPLETE_STUCK_NUDGE`,
  `CONTEXT_WARNING_NUDGE`, `turn_budget_notice`) emit nothing at all, so "the agent was
  nudged for repetition on turn 14" is not answerable.
- **Summarizer model calls are invisible in token accounting.** `summarize_incremental`
  and `summarize_three_step` call `model.complete` directly rather than through the
  loop's `_timed_complete`, so their prompt/completion tokens never reach
  `accumulate_usage`, `usage_totals` or `turn_metrics`. A three-step summarize is three
  uncounted model calls; only its wall-clock shows, as `compaction_ms`. Any cost figure
  derived from the event log therefore understates a run that compacted — which is the
  opposite of the direction `eval/costs.py` is careful about elsewhere.

## Before the next benchmark run

- Confirm the run is on `--mode eval`. The default posture is `interactive`, which
  has no model-call gates. `eval/harbor_adapter.py` pins this itself, but any
  hand-rolled runner must ask for it explicitly.
- Set `agent_timeout_sec` in the job kwargs to match `override_timeout_sec`. Harbor
  enforces its timeout by killing the agent and tells it nothing, so without this the
  agent has no wall-clock awareness and the deadline plumbing buys nothing. No longer
  only a checklist item: the adapter now logs a warning when it is missing, so a run
  that forgot says so in its own log.
- **Every score is single-trial and will stay that way.** See the measurement
  constraint at the top. Treat any single-run delta, in either direction, as
  unmeasured — including deltas from the changes recorded above.
