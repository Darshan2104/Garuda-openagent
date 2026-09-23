# Dual-model and routing evaluation

Paired cost/quality baselines for dual-model delegation and initial runtime
routing. Every claim compares the same task mix under a single-model baseline
and a candidate configuration, measured over the complete trajectory.

## Result schema

See `garuda/eval/dual_model.py::PairedResult`: task/trial identity, success and
verification outcome, total tokens and cost, per-role tokens and cost
(reasoning, collection, classifier), wall/model/tool time, investigation
counts, and collection job/fallback/stale-report counts.

Unknown cost is recorded as `null` with a reason and excluded from savings —
never compared as zero. This is what keeps an external subscription harness
from reading as free.

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
* Complete attribution of native model calls to a model role.
* Zero collection-authorized workspace mutations.
* Visible fallback and stale-report rates.

If total-trajectory cost does not fall, collection remains opt-in while the
measurements are analyzed. `compare_trials` / `format_comparison` implement
the aggregation and the gate check.
