# Cheat sheet

Everyday commands, ready to copy. Run them from your project directory, or add
`--workspace /path/to/project`. Every flag here is checked against the real
CLI in CI.

## Set up

```bash
python -m pip install -e .                 # from a clone of the repository
export OPENROUTER_API_KEY=sk-or-...        # default model's key (placeholder)
export GARUDA_MODEL=anthropic/MODEL_ID     # optional: another LiteLLM model
garuda --help
```

## Read and understand (read-only)

```bash
garuda run --mode readonly -t "Explain how this project is organized"
garuda run --agent explore -t "Find where the config file is parsed"
garuda run --agent plan -t "Plan how to add pagination to the list endpoint"
garuda run --agent reviewer --mode readonly -t "Review the changes in review.diff"
```

## Change code

```bash
garuda run -t "Fix the failing test in tests/test_parser.py"
garuda run --mode eval -t "Make the test suite pass"
garuda run --mode rigorous -t "Fix the bug and prove it is fixed"
garuda chat --workspace .
```

## Choose where commands run

```bash
garuda run --workspace-kind sandbox -t "Run the tests"
garuda run --workspace-kind sandbox --allow-network -t "Install dependencies and run the tests"
garuda run --workspace-kind docker --no-network -t "Run the test suite"
garuda run --workspace-kind docker --docker-image python:3.12 --docker-memory 4g --docker-cpus 4 -t "Run the test suite"
garuda run --workspace /srv/app --workspace-kind remote --docker-host ssh://builder.example -t "Run the test suite"
```

## Limit a run

```bash
garuda run --deadline-sec 900 -t "Fix the flaky test"
garuda run --max-turns 30 -t "Fix the flaky test"
garuda run --model openrouter/VENDOR/MODEL -t "Fix the flaky test"
garuda run --reasoning-effort high -t "Find the race condition"
garuda run --no-collection -t "Fix the flaky test"
```

## Sessions

```bash
garuda sessions
garuda sessions --limit 20
garuda run --resume latest -t "Continue where you left off"
garuda run --resume 3f2a -t "Continue from that session"
```

## Dashboard

```bash
garuda web --read-only
garuda web --allow-workspace . --max-permission smart
garuda web --allow-workspace ~/code/api --allow-workspace ~/code/web --port 8790 --no-browser
```

## Automation

```bash
garuda run --json -t "List the public functions" > events.jsonl
garuda run --trajectory run.jsonl -t "Fix the failing test"
garuda recipe run fix-and-test.yaml --param issue="Login is case sensitive"
garuda serve --workspace . --port 8765
```

## MCP

```bash
garuda mcp list --no-connect
garuda mcp trust
garuda mcp list
garuda run --mcp-config tools.json -t "Use the issue tracker to find open bugs"
```

## External harnesses

```bash
garuda runtime list
garuda runtime inspect codex
garuda run --runtime claude -t "Add type hints to src/utils.py"
garuda runtime handoff --session latest --to codex
garuda runtime handoff --session latest --to codex --confirm
garuda runtime recover --session latest
garuda runtime reclaim --session latest
garuda runtime resume --session latest -t "Continue natively"
garuda runtime support --session latest
```

## Files Garuda reads

| Path | What it is |
|---|---|
| `AGENTS.md` or `GARUDA.md` | Project instructions added to every run |
| `.agent/agents/<name>.yaml` | Your own profiles |
| `.agent/skills/<name>/SKILL.md` | Skills |
| `.agent/mcp.json` | Project MCP servers |
| `.agent/tools/*.py` | Python tools (only with `--load-project-tools`) |
| `.agent/settings.yaml` | Project defaults |
| `~/.agent/settings.yaml` | Trusted global settings: models, routing, runtimes, trust |
| `~/.agent/mcp.json` | Global MCP servers |
| `~/.agent/sessions/<id>/` | Saved sessions |
