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
