# Terminal-Bench 2.0 via Harbor

```bash
pip install -e ".[eval]"
export OPENROUTER_API_KEY=...
harbor run -d terminal-bench@2.0 \
  --agent garuda.eval.harbor_adapter:GarudaHarborAgent \
  --model openrouter/deepseek/deepseek-v4-flash-0731 \
  --n-concurrent 4
```

Use the bundled reproducible configuration:

```bash
harbor run -c garuda/eval/benchmarks/terminal_bench/job.yaml
```

Each trial writes `agent/trajectory.json` (ATIF-v1.7) and `agent/events.jsonl`. Common agent kwargs are `agent_profile`, `max_turns`, and `permission_mode`; use a disposable container with `yolo` only when appropriate.

For a model-agnostic, docker-free pre-flight, use the ablation runner described in [Evaluation](index.md). It grades workspace output independently of the agent's self-report.
