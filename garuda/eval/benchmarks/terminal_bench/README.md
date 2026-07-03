# Terminal-Bench 2.0 via Harbor

Garuda integrates with [Harbor](https://www.harborframework.com/) as a custom agent for Terminal-Bench 2.0 evaluation. Trajectories are exported in ATIF-v1.7 format to `agent/trajectory.json` per trial.

## Prerequisites

```bash
pip install -e ".[eval]"
export OPENAI_API_KEY=...   # or provider-specific key for your model
```

## Quick run

```bash
harbor run -d terminal-bench@2.0 \
  --agent garuda.eval.harbor_adapter:GarudaHarborAgent \
  --model openai/gpt-4o-mini \
  --n-concurrent 4
```

## Job configuration

Use the bundled job config for reproducible runs:

```bash
harbor run -c garuda/eval/benchmarks/terminal_bench/job.yaml
```

Override the model on the CLI:

```bash
harbor run -c garuda/eval/benchmarks/terminal_bench/job.yaml \
  --model anthropic/claude-sonnet-4-20250514
```

## Agent options

Pass kwargs via Harbor agent config:

| Kwarg | Default | Description |
|-------|---------|-------------|
| `agent_profile` | `harbor` | Garuda YAML profile (`garuda/agents/defaults/harbor.yaml`) |
| `max_turns` | profile default | Override max agent turns |
| `permission_mode` | `yolo` | Permission engine mode for eval |

## Outputs

Each trial writes:

- `agent/trajectory.json` — ATIF-v1.7 trajectory (Harbor-compatible)
- `agent/events.jsonl` — raw Garuda event log

## Smoke test (single task)

```bash
harbor trial -d terminal-bench@2.0 \
  --task-name hello-world \
  --agent garuda.eval.harbor_adapter:GarudaHarborAgent \
  --model openai/gpt-4o-mini
```

## Offline proxy — the ablation runner (no Docker, no Harbor)

The full Terminal-Bench baseline above needs the Harbor + Docker stack and a
frontier API key. For a **docker-free, model-agnostic** measurement of the
*harness itself* (verifier on/off, condenser strategy, standard vs rigorous),
use the ablation runner. It grades tasks with a ground-truth `check(workspace)`
independent of the agent's self-report, so a variant can't pass by claiming to:

```bash
python -m garuda.eval.ablation --model gemini/gemini-2.5-flash
# subset of variants:
python -m garuda.eval.ablation --model gemini/gemini-2.5-flash --variants baseline,no_verifier
```

The built-in suite exercises file creation, line counting, **buried-value
search** (grep/read), **in-place edit**, and **integer summation** (bash +
verification) — the tool families Terminal-Bench stresses — and prints a
per-variant pass-rate / turns / tokens table. It is the recommended pre-flight
before spending a full Terminal-Bench run.
