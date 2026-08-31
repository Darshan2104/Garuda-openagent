# Evaluation

Garuda integrates with Harbor and exports ATIF-v1.7 trajectories. Evaluation is outside the core product loop and pins `--mode eval` so completion evidence is measured under the strict gate stack.

- [Terminal-Bench 2.0](terminal-bench.md)
- [SpreadsheetBench](spreadsheet-bench.md)
- [PDF / OfficeQA](pdf-officeqa.md)

For docker-free harness experiments:

```bash
python -m garuda.eval.ablation --model gemini/gemini-2.5-flash
```

Cost accounting prefers provider-reported cost, then explicit user overrides, then the versioned in-repository price snapshot, and only then LiteLLM's table.
