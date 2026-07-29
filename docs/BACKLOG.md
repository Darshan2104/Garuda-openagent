# Open Backlog

**Living document. Residuals only.** An item leaves this file when it is fixed or
when the decision to accept it is recorded here as permanent. Closed work is not
kept — it is in [archive/](archive/), dated.

The rule that makes this useful: nothing here is marked done. If you fix it,
delete it. A ledger that mixes open and closed items cannot tell you what is left
without re-auditing the code, which is what the archived ledgers turned into.

Last verified against code: 2026-07-28.

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

## Before the next benchmark run

- Set `agent_timeout_sec` in the job kwargs to match `override_timeout_sec`.
  Harbor enforces its timeout by killing the agent and tells it nothing, so
  without this the agent has no wall-clock awareness and the deadline plumbing
  buys nothing.
- Confirm the run is on `--mode eval`. The default posture is now `interactive`,
  which has no model-call gates. `eval/harbor_adapter.py` pins this itself, but
  any hand-rolled runner must ask for it explicitly.
- **No score in this repo is validated against a real graded run.** A passing test
  suite plus per-mechanism verification is not a benchmark result.
