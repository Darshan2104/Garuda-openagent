# Evaluation

Garuda integrates with Harbor and exports ATIF-v1.7 trajectories. Evaluation is outside the core product loop and pins `--mode eval` so completion evidence is measured under the strict gate stack.

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
