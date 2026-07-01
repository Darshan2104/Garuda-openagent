# Garuda Open Agent

**Universal, provider-agnostic agent harness** for terminal and software engineering tasks.

Garuda is a runtime that runs any LLM against real environments using tools (bash, files, patches, tmux, MCP). It combines ideas from OpenCode, Goose, Terminus-2/KIRA, mini-SWE-agent, and Harbor into one configurable, auditable system.

> **Core thesis:** The harness is the product, not the model.

**Version:** 1.0.0 · **Python:** 3.12+ · **License:** MIT

---

## Features

| Area | Capabilities |
|------|--------------|
| **Models** | Any provider via [LiteLLM](https://github.com/BerriAI/litellm) (`openai/…`, `anthropic/…`, etc.) |
| **Tools** | `bash`, `read_file`, `write_file`, `apply_patch`, `tmux_exec`, `tmux_capture`, `image_read`, `invoke_subagent`, `task_complete` |
| **Agents** | Built-in profiles: `build`, `plan`, `explore`, `harbor` — customizable via YAML |
| **Workspaces** | `local`, `sandbox`, `tmux`, `docker`, `remote` |
| **Safety** | Permission modes, completion verifier, optional OS sandbox (bubblewrap) |
| **Context** | Output shaping, proactive + 3-step summarization |
| **Extensibility** | MCP servers, plugin hooks, YAML recipes, subagent handoff |
| **Modes** | `standard` (fast) or `rigorous` (plan → execute → critic) |
| **Interfaces** | Headless CLI, interactive chat, JSON-RPC IDE server |
| **Evaluation** | Harbor adapter + ATIF-v1.7 trajectory export |

Spreadsheet and PDF benchmarks mentioned in early docs are **eval-only targets** (see [Evaluation](#evaluation-harbor)), not core product features.

---

## Installation

```bash
git clone https://github.com/Darshan2104/Garuda-openagent.git
cd Garuda-openagent

# Core + dev tools
pip install -e ".[dev]"

# Add Harbor eval support (optional)
pip install -e ".[eval]"
```

### Optional system dependencies

| Tool | Used for | Required? |
|------|----------|-----------|
| `tmux` | Interactive terminal tasks (`--workspace-kind tmux`) | Recommended |
| `docker` | Container isolation (`docker`, `remote` workspaces) | Optional |
| `bubblewrap` (`bwrap`) | OS sandbox (`--workspace-kind sandbox`) | Optional |
| API key | LLM calls (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) | Yes for real runs |

---

## Quick start

```bash
# Set your model (any LiteLLM provider/model string)
export GARUDA_MODEL=openai/gpt-4o-mini
export OPENAI_API_KEY=sk-...

# Run a single task
garuda run -t "List all Python files in the current directory"

# Interactive session with permission prompts
garuda chat --agent build

# Save trajectory as JSONL
garuda run -t "Create hello.txt" --trajectory run.jsonl
```

---

## CLI reference

### `garuda run` — headless task execution

```bash
garuda run -t "TASK" [options]
garuda run -f task.md [options]
```

| Flag | Description |
|------|-------------|
| `-t`, `--task` | Task description |
| `-f`, `--file` | Read task from a file |
| `--model` | Model name (default: `$GARUDA_MODEL` or `openai/gpt-4o-mini`) |
| `--agent` | Agent profile: `build`, `plan`, `explore`, `harbor` (default: `build`) |
| `--agents-dir` | Directory with custom agent YAML profiles |
| `--workspace` | Workspace root directory (default: `.`) |
| `--workspace-kind` | `local` · `sandbox` · `tmux` · `docker` · `remote` |
| `--docker-image` | Image for docker/remote workspaces (default: `ubuntu:22.04`) |
| `--docker-host` | Remote Docker daemon (`DOCKER_HOST`, e.g. `tcp://host:2375`) |
| `--mode` | `standard` · `rigorous` · `readonly` |
| `--permission-mode` | `auto` · `smart` · `readonly` · `yolo` |
| `--mcp-config` | Path to MCP servers YAML |
| `--max-turns` | Max agent turns |
| `--no-verifier` | Disable completion verification gate |
| `--no-three-step-summary` | Disable 3-step context summarization |
| `--json` | Print JSONL events to stdout |
| `--trajectory` | Save event log to a JSONL file |

**Examples:**

```bash
# Rigorous mode (plan → execute → critic)
garuda run -t "Fix the failing test in tests/" --mode rigorous

# Docker-isolated run
garuda run -t "Install deps and run pytest" --workspace-kind docker

# Tmux for interactive terminal work
garuda run -t "Start a server and curl it" --workspace-kind tmux

# OS sandbox (Linux + bubblewrap)
garuda run -t "Refactor utils.py" --workspace-kind sandbox
```

### `garuda chat` — interactive session

```bash
garuda chat --agent build --model openai/gpt-4o-mini
```

Enter tasks at the `task>` prompt. Permission prompts appear in `smart` mode.

### `garuda serve` — JSON-RPC IDE server

```bash
garuda serve --host 127.0.0.1 --port 8765
```

Send HTTP POST requests with JSON-RPC 2.0 bodies:

```bash
# Health check
curl -s -X POST http://127.0.0.1:8765 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"health","id":1}'

# Run a task
curl -s -X POST http://127.0.0.1:8765 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"run","params":{"task":"List files"},"id":2}'
```

| Method | Description |
|--------|-------------|
| `health` | Server status and version |
| `run` | Execute a task (`params.task`, optional `model`, `agent`, `mode`) |
| `list_agents` | List available agent profile names |

### `garuda recipe` — YAML workflows

```bash
garuda recipe run RECIPE.yaml --param issue="login bug" --param test_command="pytest -q"
```

Bundled example: `garuda/config/defaults/fix-and-test.yaml`

```yaml
name: fix-and-test
parameters:
  - name: issue
    required: true
  - name: test_command
    default: "pytest"
steps:
  - agent: plan
    prompt: "Analyze this issue and propose a fix plan: {{issue}}"
  - agent: build
    prompt: "Implement the fix for: {{issue}}"
  - agent: build
    prompt: "Run {{test_command}} and fix any failures related to: {{issue}}"
```

---

## Agent profiles

Profiles live in `garuda/agents/defaults/` (or your `--agents-dir`).

| Profile | Access | Tools | Use case |
|---------|--------|-------|----------|
| **build** | Read/write/exec | All core + MCP | Implementation, fixes |
| **plan** | Read-only | `bash`, `read_file` | Analysis and planning |
| **explore** | Read-only | `bash`, `read_file` | Fast codebase search (subagent) |
| **harbor** | YOLO eval | bash, files, patch, `task_complete` | Harbor benchmarks |

### Custom agent YAML

```yaml
# .garuda/agents/my-agent.yaml
name: my-agent
description: Custom agent for our repo
permission_mode: smart
max_turns: 100
tools:
  - bash
  - read_file
  - write_file
  - task_complete
tool_rules:
  bash:
    default: allow
system_prompt: |
  You are a specialist agent for this codebase.
```

```bash
garuda run -t "..." --agent my-agent --agents-dir .garuda/agents
```

---

## MCP integration

Connect stdio MCP servers via a YAML config:

```yaml
# .garuda/mcp.yaml
servers:
  - name: my-server
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
```

```bash
garuda run -t "Use MCP tools" --mcp-config .garuda/mcp.yaml
```

MCP tools are namespaced as `mcp__<server>__<tool>`.

---

## Workspaces

| Kind | Description |
|------|-------------|
| `local` | Run commands directly on the host (default) |
| `sandbox` | Linux bubblewrap isolation when `bwrap` is installed |
| `tmux` | Persistent tmux session for interactive terminals |
| `docker` | Ephemeral container with workspace mounted at `/workspace` |
| `remote` | Docker on a remote daemon via `--docker-host` / `DOCKER_HOST` |

---

## Permissions

| Mode | Behavior |
|------|----------|
| `smart` | Allow safe ops; prompt for risky commands (chat mode) |
| `auto` | Auto-approve most tool calls |
| `readonly` | Deny writes and patches |
| `yolo` | Allow everything (eval/sandboxed use only) |

The `task_complete` tool triggers a **completion verifier** that checks summary quality and optional verification commands before accepting task completion.

---

## Evaluation (Harbor)

Garuda integrates with [Harbor](https://www.harborframework.com/) for benchmark evaluation. Trajectories are exported in **ATIF-v1.7** format.

```bash
pip install -e ".[eval]"

harbor run -d terminal-bench@2.0 \
  --agent garuda.eval.harbor_adapter:GarudaHarborAgent \
  --model openai/gpt-4o-mini
```

See also:

- `garuda/eval/benchmarks/terminal_bench/` — Terminal-Bench 2.0
- `garuda/eval/benchmarks/spreadsheet/` — SpreadsheetBench (eval only)
- `garuda/eval/benchmarks/pdf/` — OfficeQA / PDF (eval only)

---

## Project structure

```
garuda/
├── agents/          # YAML agent profile loader + defaults
├── config/          # Recipe loader + defaults
├── core/            # Agent loop, events, permissions, verifier, rigorous mode
├── context/         # Context manager, summarizer, output shaping
├── model/           # LiteLLM + ScriptModel
├── tools/           # bash, files, patch, tmux, MCP tools, subagent
├── workspace/       # local, sandbox, tmux, docker, remote
├── mcp/             # MCP stdio client
├── plugins/         # before/after tool hooks
├── eval/            # Harbor adapter, ATIF export, benchmark configs
└── interfaces/      # CLI, JSON-RPC server, runner

docs/
├── GARUDA_OPEN_AGENT_RFC.md   # Architecture RFC
└── MODULES.md                 # 33-module work breakdown

tests/
├── test_phase1.py … test_phase6.py
└── fixtures/                  # MCP echo server for tests
```

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev,eval]"

# Run all tests
pytest tests/ -v

# Run a specific phase
pytest tests/test_phase6.py -v
```

**Current test status:** 35 passed, 1 skipped (`docker` not available in some CI environments).

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `GARUDA_MODEL` | Default model for CLI commands |
| `OPENAI_API_KEY` | OpenAI / compatible providers |
| `ANTHROPIC_API_KEY` | Anthropic models |
| `DOCKER_HOST` | Remote Docker daemon for `--workspace-kind remote` |

---

## Documentation

- [Architecture RFC](docs/GARUDA_OPEN_AGENT_RFC.md) — design goals, interfaces, roadmap
- [Module breakdown](docs/MODULES.md) — all 33 modules and status

---

## License

MIT
