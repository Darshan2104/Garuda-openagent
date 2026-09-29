# Paired-trial report CLI design

## Purpose

Issue #81 has the schema, gates, and native-event collector needed to evaluate
dual-model routing, but invoking those Python APIs by hand makes live evidence
too easy to omit or mislabel. This design adds a report-only command that turns
already completed native sessions into a reproducible paired-trial report. It
does not launch providers, inspect credentials, run an agent, or treat fixture
data as a live run.

The command supports the final release decision for #81. It does not by itself
close #81: an operator still supplies the actual fixed-version sessions and
independent evidence scores. #74 also remains dependent on a separately
recorded real runtime handoff.

## Chosen approach

Three approaches were considered:

1. Keep the Python API only. It is low code churn, but each operator must
   reconstruct session metadata and risks producing incomplete evidence.
2. Add a report-only CLI over persisted native sessions. It makes the exact
   report contract repeatable without spending provider quota or handling
   credentials. **Chosen.**
3. Add a CLI that also launches the baseline and candidate agents. That would
   combine evaluation mechanics with provider authorization, task isolation,
   retries, and spending controls; it is a later, separately scoped runner.

## Command surface

The command is:

```text
garuda eval dual-model report \
  --baseline TASK_ID=SESSION_ID \
  --candidate TASK_ID=SESSION_ID \
  [--baseline ... --candidate ...] \
  --sessions-dir PATH \
  --task-mix PATH \
  --model-version ROLE=VERSION [--model-version ...] \
  --price-source TEXT \
  --prompt-revision TEXT \
  [--evidence-scores PATH] \
  --output PATH \
  [--require-passing-gates] [--overwrite]
```

`--baseline` and `--candidate` are repeatable. Every task id must occur once
in each side, session ids are validated by `SessionStore`, and all referenced
sessions must be terminal native sessions with readable `meta.json` and
`events.jsonl`. A session reference can never be an arbitrary path.

`--task-mix` is a JSON manifest with a versioned representative task list. Each
task declares its id and one category from the existing required mix
(`read-heavy`, `debugging`, `implementation`, `doc-analysis`, and
`do-not-delegate`). The command rejects duplicate task ids, missing pairs, or
a mix that omits a category. It reports results even when gates fail; only
`--require-passing-gates` turns a measured release-gate failure into a nonzero
exit after the report is written.

`--model-version`, `--price-source`, and `--prompt-revision` are mandatory
provenance. No environment snapshot, API key, OAuth token, raw prompt, raw
model content, or raw event trail is copied to the report. `--evidence-scores`
is an optional JSON mapping of task id to baseline/candidate scores in `0..1`.
When absent, the resulting report says that independent evidence grading is
unknown rather than inventing a quality score.

## Implementation boundaries

`garuda/interfaces/main.py` owns parsing and dispatch. A new small
`garuda/eval/paired_report.py` owns session loading, manifest validation, and
report construction. It uses `SessionStore` only to derive a validated session
directory, then reads the existing meta and event files. It reconstructs the
minimal `AgentResult` required by
`paired_result_from_agent_result`; no agent loop, model factory, or external
runtime is imported.

`garuda/eval/dual_model.py` remains the source of truth for per-trial
collection, comparison, and gate logic. Its persisted payload gains the
comparison summary and an explicit `release_gates_passed` field while retaining
the existing `schema_version`, metadata, and trial list. The existing Python
writer remains compatible for callers that only need trial rows.

## Failure and safety behavior

- Invalid ids, malformed JSONL/manifest/scores, missing session files, a
  running session, duplicate or unpaired tasks, and missing provenance are
  actionable usage errors. No output file is written.
- A report path that already exists is refused unless `--overwrite` is present.
- Unknown model cost remains `null`; it is never zero-filled. Unattributed
  native calls remain in total tokens/cost but cause the release gate to fail.
- Gate failures, unknown scores, fallbacks, and stale reports are published in
  the output. They are observations, not CLI errors unless the caller asks for
  `--require-passing-gates`.
- The command is read-only except for writing the requested report path. It
  neither invokes a provider nor accesses vendor credential stores.

## Tests and documentation

Focused tests will cover CLI parsing, paired session loading, terminal-session
refusal, path/session-id safety, duplicate and incomplete pairs, full required
task-mix coverage, provenance enforcement, unknown-cost propagation, score
validation, non-overwrite behavior, and `--require-passing-gates` exit status.
Existing dual-model and cost-accounting tests remain the gate tests. The
evaluation guide and module map will document the command and its explicit
limits.
