# SpreadsheetBench

SpreadsheetBench is an eval-only Harbor target; it does not add a core Garuda product feature.

```bash
harbor run -d spreadsheet-bench@1.0 \
  --agent garuda.eval.harbor_adapter:GarudaHarborAgent \
  --model openrouter/deepseek/deepseek-v4-flash-0731
harbor run -c garuda/eval/benchmarks/spreadsheet/job.yaml
```

The same Harbor agent is used as Terminal-Bench. Task images and office tooling must be supplied by the dataset/container, for example LibreOffice, Python, or openpyxl. Trials write ATIF trajectories under `agent/trajectory.json`.
