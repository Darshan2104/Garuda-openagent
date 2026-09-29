# Dual-model and routing evaluation

Paired cost/quality baselines for dual-model delegation and initial runtime
routing. Every claim compares the same task mix under a single-model baseline
and a candidate configuration, measured over the complete trajectory.

## Result schema

See `garuda/eval/dual_model.py::PairedResult`: task/trial identity, success and
verification outcome, total tokens and cost, per-role tokens and cost
(reasoning, collection, classifier), wall/model/tool time, investigation
counts, and collection job/fallback/stale-report counts.

`classifier_accounting` converts a session's persisted initial-selection
record into `classifier_tokens`/`classifier_cost_usd` values for a trial. A run
with no classifier call counts as a known zero. An unpriced call keeps the
trial total unknown. `paired_result_from_agent_result` materializes native
event trails (including collection attempt summaries), and `save_paired_results`
refuses to persist a report without immutable model-version, price-source, and
prompt-revision metadata. It provides the reproducible result-to-report path;
an operator still has to run the configured baseline and candidate models.

Unknown cost is recorded as `null` with a reason and excluded from savings —
never compared as zero. This is what keeps an external subscription harness
from reading as free.

## Build a paired report from sessions

After running one terminal native session per baseline/candidate task, create
a task-mix manifest. It contains the existing mix definition and an exact task
assignment, for example:

```json
{
  "categories": [
    {"id": "read-heavy", "delegation_expected": true},
    {"id": "debugging", "delegation_expected": true},
    {"id": "implementation", "delegation_expected": true},
    {"id": "doc-analysis", "delegation_expected": true},
    {"id": "do-not-delegate", "delegation_expected": false}
  ],
  "tasks": [
    {"id": "read-files", "category": "read-heavy"},
    {"id": "debug-test", "category": "debugging"},
    {"id": "small-change", "category": "implementation"},
    {"id": "read-doc", "category": "doc-analysis"},
    {"id": "pure-edit", "category": "do-not-delegate"}
  ]
}
```

The full manifest must assign at least one task to every representative
category. Build the report with session IDs rather than copying transcripts:

```bash
garuda eval dual-model report \
  --sessions-dir .agent/sessions \
  --task-mix task-mix.json \
  --baseline read-files=baseline-session-id \
  --candidate read-files=candidate-session-id \
  --model-version reasoning=provider/model@pinned-version \
  --price-source 'provider invoice export 2026-09' \
  --prompt-revision git:abc123 \
  --output paired-report.json
```

Repeat the two trial flags for every task. The command reads only terminal
persisted sessions; it never starts a model or loads provider credentials. It
requires one baseline and one candidate for every manifest task, writes no raw
event payloads (but records the referenced session IDs), and refuses to replace
an existing report unless `--overwrite` is explicit. Use
`--require-passing-gates` in automation to return nonzero
after emitting a valid report whose release gates fail. A report built from
fixtures or local sessions is evidence for its recorded trials only, not proof
that a live rollout should proceed.

## Representative mix

* Read-heavy exploration (locate files, extract snippets, enumerate symbols).
* Debugging against a failing check.
* Small implementation with verification.
* Document analysis (PDF/spreadsheet/image evidence).
* At least one task class where delegation should not be used (pure edit or
  single-file reasoning), to pin over-delegation.

Fix model versions, prompts, prices, and seeds where supported before
comparing. Single-trial scores stay single-trial; do not present a one-run
delta as measured.

## Release thresholds

* At least 20% lower median total cost on read-heavy tasks.
* At least 30% lower reasoning-model input tokens on read-heavy tasks.
* No more than a two-percentage-point completion-rate regression.
* No material decline in evidence quality or repository investigation.
* Complete attribution of native model calls to a model role; unknown
  attribution fails the gate.
* Zero collection-authorized workspace mutations.
* Visible fallback and stale-report rates.

If total-trajectory cost does not fall, collection remains opt-in while the
measurements are analyzed. `compare_trials` / `format_comparison` implement
the aggregation and the gate check. The committed fixture is an offline schema
and gate example, not evidence that any live configuration has passed release.
