# Evaluation

Evaluation stays outside the core product loop. Harbor runs pin `--mode eval`
so completion evidence is measured under the strict gate stack, and Garuda can
export those recorded events as ATIF-v1.7 trajectories.

## Records and trajectories

Every session keeps its append-only event log. The CLI can also write the raw
event stream to a chosen JSONL path:

```bash
garuda run -t "Inspect this project" --mode eval --trajectory run-events.jsonl
```

Raw event JSONL is not the same file shape as an ATIF export. Harbor integration
converts events into ATIF-v1.7 with agent, step, usage, cost, and result fields.

## Benchmarks

- [Terminal-Bench 2.0](terminal-bench.md)
- [SpreadsheetBench](spreadsheet-bench.md)
- [PDF / OfficeQA](pdf-officeqa.md)

For docker-free harness experiments:

```bash
python -m garuda.eval.ablation --model gemini/gemini-2.5-flash
```

To persist measured native trials as a model × harness artifact, add
`--matrix-out matrix.json`. Validated external-harness trial feeds can be
combined with `--external-trials external.json`; each feed must contain a
`trials` list with discovered harness version/capabilities and measured
completion, cost, approvals, and handoff fields.

Cost accounting prefers provider-reported cost, then explicit user overrides, then the versioned in-repository price snapshot, and only then LiteLLM's table.

## Offline incident evidence

```bash
python -m garuda.eval.contract_matrix --incidents --out incident-reports
```

The opt-in v1 corpus contains invented files, tasks and ScriptModel responses;
it copies no transcripts or credentials and constructs no provider client. The
entry also disables LiteLLM's import-time remote price-map fetch. Fixture shell
commands use Environment and need only the existing shell/Git tools, with no
new interpreter requirement inside a workspace. This slice runs local fixtures;
it makes no Docker, remote workspace, vendor adapter or live-model integration claim.

| Incident | Existing owner exercised | Outcome |
|---|---|---|
| Ignored config | `garuda_yaml.load_text` refuses a misspelled role field before inference | Pass |
| Denied reviewer command | Native tool dispatch preserves a declared denial and does not create the command's marker | Pass |
| No-edits startup changes | `NoEditsGuard` captures before synthetic startup and detects its metadata write | Pass |
| Partial output | Native loop executes well-formed prefix arguments despite `finish_reason=length` | Expected-fail / not yet guarded |
| Stale edits | EditTool refuses an obsolete text precondition and preserves current bytes | Pass |
| Unmatched intent | Flow recovery persists a quarantined receipt for a launch without closure | Pass |
| Quota race | Two contenders observe waiting; the quota store admits only one dispatch claim | Pass |

These are mechanism regressions, not evidence that a reviewer can complete its
task despite a denied command. The stale-edit case proves missing-text refusal;
it does not claim snapshot/version protection when an old substring still
exists. Startup is a synthetic metadata write, not a vendor adapter launch.
The quota case proves atomic claim contention, not provider availability or the
ACP post-start dispatch check. Unmatched-intent recovery does not launch a model.

`fixtures/minimal-v1.json` pins every AgentConfig field, including the static
prompt, tools, turn budget and disabled model-based helpers. Loading refuses
schema drift. Its version and canonical SHA-256 identify the baseline independently
of product defaults. `ablation.run_minimal_pair(task, mechanism, responses, base_dir)`
replays identical scripts and setup under this configuration, changing only the
selected supported boolean field. Authority policies normalize each temporary
fixture directory to the same logical root. No mode preset or arbitrary override
can introduce hidden changes. This is a separate, controlled ablation API; the
incident corpus itself records owner observations, not invented baseline trials.

The existing matrix writer/loader preserves additive, versioned `evaluation`
evidence. Source/schema digests identify known runtime/tools, the fixture and
script identify task/model, and actual outbound system-message digests identify
the prompt. Local environment identity covers platform, host Python and the
Environment implementation; it is not a container image or full dependency lock.
Unknown identities use `null`, including prompts for pre-inference refusals and
checks without a supplied `AblationTask.check_id`, and task fixtures with setup
callbacks but no supplied `fixture_id`. Those identities must bind
external fixture/grader inputs, not merely a function name. Reports contain no
task text, tool output or transcript. Ordinary trials retain their existing shape.

Model completion, independent fixture grading and authorized acceptance remain
distinct. Corpus assertions are not user-authorized acceptance receipts, so
authorized acceptance stays `unavailable` through the existing acceptance policy.
Monetary cost remains `null` with a reason, even though ScriptModel makes no
provider calls. Coverage counts include expected-fail and unknown separately;
neither is a passing contract. Unexpected case failures retain unknown coverage
and make the command fail, while preserving the other cells.

Offline proof: `pytest tests/test_eval_incidents.py -q`. Restoring
`harness_matrix.py` from HEAD makes its report round-trip regression fail because
the previous loader drops evaluation provenance. Disabling the schema rejection,
permission enforcement, no-edits comparison, edit precondition, intent quarantine
or quota claim-state guard separately makes the corresponding incident fail.
The partial-output assertion is a strict expected failure until its owner gains
a guard. This is deterministic contract evidence only. A single run or pair never
establishes uplift. Optional live-model experiments are outside this slice and
must be separately opted into with explicit spend/turn/time caps; tests never run them.

## Deterministic editing-worker fan-out

Provider-free development script for [#384](https://github.com/Darshan2104/Garuda-openagent/issues/384):

```bash
python scripts/measure_editing_fanout.py --delay 0.05 --out editing-fanout.json
```

This is a **deterministic fixture with scripted delays, not a latency or quality
claim**. Scripts make two calls per child, each sleeping for the configured delay
and reporting 120 invented tokens and zero dollars. No provider client is
constructed; no provider calls or charges occur. The root `SpendGate` settles
those synthetic usage reports through the normal native loop.

Four disjoint file rewrites and four same-file rewrites each run in fresh
temporary repositories. N=1 integrates after each batch of one, so the next
child starts from a fresh parent base. N=4 launches one batch of four, waits for
all children to settle, then attempts integration in request order. Disjoint
stale siblings rebase onto this service's recorded integrations, with checks on
the combined tree and destination revalidation before apply. Overlap, outside
drift, parent HEAD/ref/index movement, metadata and link changes still refuse.
There is no content merge or retry of refused candidates.
For the overlapping scenario N=1 ends with the fourth rewrite; N=4 ends with
the first. These are different delivered outcomes, so timing ratios would not
compare equivalent completed work.

One run on 2026-11-17, Python 3.12.0, `macOS-26.7.1-arm64-arm-64bit`, 0.05 s per call:

| Edits | N | Wall (s) | Ready / refused / quarantined | Integrated (rebased) / stale-base | Integration wait sum (s) | Integration duration sum (s) | Discarded candidates / tokens / worker-seconds |
|---|---:|---:|---|---|---:|---:|---|
| Disjoint | 1 | 7.274 | 4 / 0 / 0 | 4 (0) / 0 | 0.018 | 3.511 | 0 / 0 / 0.000 |
| Disjoint | 4 | 5.792 | 4 / 0 / 0 | 4 (3) / 0 | 6.781 | 3.841 | 0 / 0 / 0.000 |
| Overlapping | 1 | 5.369 | 4 / 0 / 0 | 4 (0) / 0 | 0.018 | 2.825 | 0 / 0 / 0.000 |
| Overlapping | 4 | 3.801 | 4 / 0 / 0 | 1 (0) / 3 | 4.137 | 2.072 | 3 / 720 / 3.354 |

Every row has **4 launches, 8 scripted calls, 960 settled scripted tokens,
zero remaining reservations, and $0 provider spend**. Per-candidate JSON rows
retain the request order, typed outcome, observed usage (240 tokens each),
integration state/code, rebase evidence and timings; aggregate rows count
rebased integrations. Each candidate is ready before its own integration attempt; the separate integration refusals do not change that
creation outcome.

Wall time covers first batch dispatch through the last integration result,
including native execution, Git snapshots, ownership operations, checks and
retirement on that path. It excludes fixture setup, parent lease acquisition and
release, and final temporary-directory cleanup. Integration wait is measured
from a child's settled-ready event to its integration attempt, including the
batch barrier and earlier integrations. It is coordinator wait, not lock wait:
the service refuses busy integration instead of queueing it. Summed waits and
worker times can overlap and exceed wall time. Integration duration includes
checks/publication/retirement for winners and refusal processing for losers.
Discarded work means unused stale candidates, their observed scripted tokens,
and their summed admission-to-settled-ready time; it does not measure rerun cost
or final directory deletion. The overlapping N=4 case exposes this conflict
overhead; the disjoint N=4 case now delivers all four edits with three checked
rebases and no discarded candidates.

The fixture substitutes only Docker check dispatch (inspecting actual materialized
candidate files) and the current process's start identity, as the worker tests
do for sandboxes without `/bin/ps`. Real native children, isolated repositories,
lease/capacity records, permissions, snapshot revalidation and patch application
remain in use. This is not live Docker, OS identity, provider or non-local backend
validation. Run it from a source checkout in a dedicated process. Never import
it into a process doing real agent work: Docker dispatch, process identity and environment
settings are patched process-wide. Lease, recovery, session and settings stores
are redirected to temporary directories for the measurement.
The report command exits nonzero for unexpected outcomes. The regression test
checks outcomes, spend settlement and serialized dispatch, with no speed assertion:

```bash
pytest tests/test_eval_editing_fanout.py -q -p no:cacheprovider -p no:rerunfailures
```

## Dual-model evaluation

The optional collection role can gather bounded read-only evidence while the
reasoning model remains the controller. Rollout decisions should use paired
native sessions rather than anecdotal runs. See
[Dual-model routing and paired reports](dual-model-routing.md) for task-mix
manifests, provenance requirements, offline report generation, and rollout
gates.

## Live harness smoke tests

Opt-in only: `GARUDA_LIVE_HARNESS=codex` (or a comma list, or `all`) runs one
trivial prompt per selected harness in a fixture workspace and reports the
exact harness, resolved binary, discovered version/auth outcome, elapsed time,
and whether a response event was observed:

```bash
GARUDA_LIVE_HARNESS=codex pytest tests/test_live_harness.py -q
python -m garuda.eval.live_harness --harness codex --workspace /tmp/smoke-ws
```

CI never sets the variable, so CI never needs a subscription. Caps are
strict by construction: fixed trivial prompt text, a bounded deadline, and no
spend beyond the single roundtrip. An unavailable binary is reported as an
explicit per-harness `skipped` result (especially useful with `all`) rather
than raising a local configuration error.
