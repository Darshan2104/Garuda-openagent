# SpreadsheetBench eval (Harbor)

Eval-only adapter notes for spreadsheet tasks. SpreadsheetBench is not part of the core Garuda product; this directory documents how to run it through Harbor when a compatible dataset is registered.

## Overview

SpreadsheetBench evaluates agents on spreadsheet manipulation (formulas, formatting, data transforms). Garuda uses the same Harbor agent as Terminal-Bench; only the dataset changes.

## Run

When a Harbor dataset entry exists (e.g. `spreadsheet-bench`):

```bash
harbor run -d spreadsheet-bench@1.0 \
  --agent garuda.eval.harbor_adapter:GarudaHarborAgent \
  --model openai/gpt-4o-mini \
  --ae OPENAI_API_KEY=$OPENAI_API_KEY
```

## Job config

```bash
harbor run -c garuda/eval/benchmarks/spreadsheet/job.yaml
```

## Garuda profile

The `harbor` agent profile enables `bash`, file tools, and `task_complete`. For spreadsheet-heavy tasks, ensure the container image includes required tools (e.g. `libreoffice`, `python3`, `openpyxl`) in the task environment Dockerfile.

## Trajectories

ATIF logs are written to `agent/trajectory.json` per trial for leaderboard and trace analysis.
