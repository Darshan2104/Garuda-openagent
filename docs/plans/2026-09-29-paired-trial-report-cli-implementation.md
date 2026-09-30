# Paired-trial report CLI implementation plan

**Status:** Ready for implementation

**Design:**
[Paired-trial report CLI](../design/2026-09-29-paired-trial-report-cli-design.md)

## Objective

Provide a read-only `garuda eval dual-model report` command that converts
completed, persisted native sessions into a reproducible paired-trial report.
It must surface unknowns and regressions rather than converting them to
successes, and it must never launch providers or access credential stores.

## Task 1: add report construction and validation

### Files

- Create `garuda/eval/paired_report.py`.
- Extend `garuda/eval/dual_model.py` only to serialize an optional comparison
  and release-gate outcome without changing existing caller behavior.
- Extend `garuda/eval/__init__.py` with the public report constructor only if
  the package already exports adjacent paired-evaluation helpers.
- Create `tests/test_paired_report.py`.

### Steps

1. Define immutable trial references (`task_id`, `trial`, `session_id`) and
   parse `TASK_ID=SESSION_ID` values without accepting empty fields.
2. Build a report service around `SessionStore(root).session_dir(session_id)`.
   Validate each ref through the store, read `meta.json` and `events.jsonl`,
   and reject absent, malformed, or non-terminal sessions before creating an
   output file.
3. Reconstruct a minimal `AgentResult` from terminal session metadata,
   persisted metrics/initial selection, and parsed event records. Delegate all
   per-call accounting to `paired_result_from_agent_result`.
4. Validate exact baseline/candidate pairing, unique task ids, mandatory
   provenance, and `0..1` evidence scores. Reject an incomplete representative
   task mix or task/category mismatch.
5. Compare rows with `compare_trials`, then write a JSON payload containing
   schema version, task-mix version, supplied provenance, trial rows,
   comparison, and `release_gates_passed`. Never include raw event payloads,
   prompt content, credential-like metadata, or an environment dump.
6. Refuse an existing output path unless the caller chooses overwrite. Return a
   result that separates a valid measured regression from an input error.

### Tests

- Successful pair from two terminal session fixtures includes rows, comparison,
  provenance, and no raw event content.
- Invalid session ids, unreadable/malformed files, running sessions, missing
  pairs, duplicates, incomplete task mix, invalid score, and missing
  provenance fail before a report is written.
- Unknown cost remains unknown, and unattributed calls remain in total spend
  while failing the release gate.
- Existing output refuses unless overwrite is explicit.

### Verification

```bash
pytest tests/test_paired_report.py tests/test_dual_model_evaluation.py tests/test_cost_accounting.py -q
ruff check garuda tests
```

## Task 2: wire the CLI without a provider path

### Files

- Modify `garuda/interfaces/main.py`.
- Create or extend `tests/test_paired_report_cli.py`.

### Steps

1. Add the `eval dual-model report` parser hierarchy. It accepts repeatable
   `--baseline`/`--candidate`, an explicit sessions root, task-mix manifest,
   repeatable model versions, price source, prompt revision, optional evidence
   score file, output, overwrite, and require-passing-gates flag.
2. Keep parser and dispatch synchronous/read-only. It must not call model
   setup, runtime discovery, an agent loop, or a vendor executable.
3. Render a compact comparison to stdout after the report is written. Return
   exit code 0 for a valid report even when release gates fail; return a
   distinct nonzero status only with `--require-passing-gates`. Use input-error
   status for malformed arguments or report construction failures.

### Tests

- Parser help exposes the command and required options.
- Valid command writes one report and prints a comparison.
- Regression still writes a report and succeeds by default, but
  `--require-passing-gates` returns nonzero after writing.
- Error paths do not write partial reports and do not invoke model/runtime
  factories (monkeypatch the relevant boundaries to fail if called).

### Verification

```bash
pytest tests/test_paired_report.py tests/test_paired_report_cli.py tests/test_dual_model_evaluation.py -q
ruff check garuda tests
```

## Task 3: document the operational boundary

### Files

- Update `docs/evaluation/dual-model-routing.md`.
- Update `docs/MODULES.md`.
- Extend `tests/test_docs_contract.py` only if a declared command reference
  needs a contract assertion.

### Steps

1. Show an example command with redacted, placeholder model version data.
2. State that the command converts completed sessions only; it neither runs
   providers nor proves a synthetic fixture is live evidence.
3. State the three remaining release actions: run the fixed baseline/candidate
   mix, supply independent quality scores where applicable, and publish the
   resulting report with the final acceptance evidence.

### Verification

```bash
python scripts/check_docs.py
pytest tests/test_paired_report_cli.py tests/test_dual_model_evaluation.py -q
```

## Final verification

```bash
pytest tests/test_paired_report.py tests/test_paired_report_cli.py tests/test_dual_model_evaluation.py tests/test_cost_accounting.py -q
ruff check garuda tests
python scripts/check_docs.py
pytest -q
```

The implementation PR references #81 but must not close it automatically:
actual fixed-version baseline/candidate results and the separate #74 handoff
evidence remain external acceptance inputs.
