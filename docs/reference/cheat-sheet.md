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
garuda init                                # propose roles in ~/.agent/garuda.yaml
garuda doctor                              # configuration, harness logins, leases, worktrees
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

## Verify and guard

```bash
garuda run --check "python -m pytest -q" -t "Fix the failing tests"
garuda run --check "npm test" --check "npm run lint" -t "Fix the lint errors"
garuda run --no-edits -t "Explain how errors are reported"
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
garuda run --name fix-login -t "Fix the login bug"
garuda run -t "Review the change from @fix-login"
garuda run --with fix-login --with add-tests -t "Write the release note"
garuda sessions show fix-login
garuda sessions show latest --json
garuda run --resume latest --all-projects -t "Continue the newest session anywhere"
garuda run --resume fix-login --as claude -t "Finish it on Claude Code"
```

## Background and parallel work

```bash
garuda run --bg --name docstrings -t "Add docstrings to src/calc"
garuda run --bg --isolation worktree --name usage-docs -t "Add a Usage section to README.md"
garuda run --isolation auto -t "Fix the flaky test"
garuda sessions cancel docstrings
garuda sessions merge usage-docs --check "python -m pytest -q"
garuda sessions merge usage-docs --into main --image my-checks --check "python -m pytest -q"
garuda sessions remove-worktree usage-docs
garuda approvals list latest
garuda approvals answer latest APPROVAL_ID --allow
garuda approvals answer latest APPROVAL_ID --deny
```

## Agents and memory

```bash
garuda agent list
garuda agent new docs-writer --from garuda/build --project
garuda agent check docs-writer
garuda agent show docs-writer
garuda agent prompt docs-writer
garuda agent migrate .agent/agents/old-profile.yaml
garuda run --agent docs-writer -t "Update the install section"
garuda run --agent-file ./agents/one-off.yaml -t "Try this definition once"
garuda memory list
garuda memory review
```

## Roles and flows

```bash
garuda config show
garuda config show --flow plan-build-review
garuda config trust
garuda config migrate
garuda init --project
garuda run --role coder -t "Add input validation to the signup form"
garuda flow run plan-only -t "Plan how to add pagination"
garuda flow run plan-build-review -t "Add a --version flag"
garuda flow show FLOW_ID
garuda flow resume FLOW_ID
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
garuda serve --workspace . --allow-agent reviewer --permission-ceiling readonly
garuda doctor --json
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
| `garuda.yaml` | Project checks and flows (used after `garuda config trust`) |
| `.agent/agents/<name>.yaml` | The project's agents |
| `.agent/skills/<name>/SKILL.md` | Skills |
| `.agent/memory.md` | Project notes you accepted with `garuda memory review` |
| `.agent/mcp.json` | Project MCP servers (start after `garuda mcp trust`) |
| `.agent/tools/*.py` | Python tools (only with `--load-project-tools`) |
| `.agent/settings.yaml` | Project defaults |
| `~/.agent/settings.yaml` | Trusted global settings: models, routing, runtimes, capacity, hooks, trust |
| `~/.agent/garuda.yaml` | Your roles, flows, checks and harness limits |
| `~/.agent/agents/<name>.yaml` | Your agents, for every project |
| `~/.agent/AGENTS.md`, `~/.agent/memory.md` | Your own instructions and accepted notes |
| `~/.agent/mcp.json` | Global MCP servers |
| `~/.agent/sessions/<id>/` | Saved sessions (events, approvals, `worker.log`) |
| `~/.agent/worktrees/` | Worktrees of `--isolation worktree` sessions |
| `~/.agent/usage/` | The usage ledger, one file per month |
