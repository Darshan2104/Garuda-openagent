# Garuda Open Agent

**Universal, provider-agnostic agent harness** for terminal and software engineering tasks.

Garuda is a runtime that runs any LLM against real environments using tools (bash, files, patches, tmux, MCP). It combines ideas from OpenCode, Goose, Terminus-2/KIRA, mini-SWE-agent, and Harbor into one configurable, auditable system.

> **Core thesis:** The harness is the product, not the model.

**Version:** 1.1.0 · **Python:** 3.12+ · **License:** MIT

---

## Features

| Area | Capabilities |
|------|--------------|
| **Models** | Any provider via [LiteLLM](https://github.com/BerriAI/litellm) (`openai/…`, `anthropic/…`, `fireworks_ai/…`, etc.) — retries/backoff, request timeouts, Anthropic prompt caching, extended thinking (`--reasoning-effort` cross-provider, `--thinking-budget` for Anthropic), per-provider concurrency governor |
| **Tools** | `bash`, `bash_background`/`task_output`/`kill_task`, `edit` (string replace), `read_file` (line-numbered, offset/limit), `write_file`, `grep`, `glob`, `ls`, `todo`, `web_fetch`, `web_search`, `read_pdf`, `read_spreadsheet`, `tmux_exec`, `tmux_capture`, `image_read`, `invoke_subagent`, `buffer_grep`/`buffer_slice`/`buffer_list`/`buffer_query` (archived-context retrieval), `task_complete` + MCP |
| **Sessions** | Every run persists to `~/.agent/sessions/` (`~/.garuda` back-compat, `GARUDA_SESSIONS_DIR` override); `garuda sessions` lists, `garuda run --resume <id\|latest>` continues with full context |
| **Hooks** | Lifecycle + tool shell-command hooks from the **global** `~/.agent/settings.yaml` (exit 2 blocks the tool call); hooks in a project's own `settings.yaml` run only if you set `trust_project_hooks: true` globally — a cloned repo can't self-authorize running its commands |
| **Project memory** | `AGENTS.md` / `GARUDA.md` in the workspace root is injected as project instructions |
| **Agents** | YAML or **agent.md** profiles: `build`, `plan`, `explore`, `reviewer`, `harbor` — plus your own in `.agent/agents/` |
| **Skills** | Universal `SKILL.md` format — auto-discovered from `.agent/skills/`, injected into the system prompt; `allowed-tools` frontmatter validated against the profile's tool grants |
| **Custom tools** | Drop `*.py` modules in `.agent/tools/` (opt-in — global setting or `--load-project-tools`) or register per-instance via the SDK |
| **Subagents** | Main agent spins up isolated subagents via `invoke_subagent` |
| **SDK** | `garuda.sdk.SoftwareAgent` — OpenHands-style programmatic API |
| **Workspaces** | `local`, `sandbox`, `tmux`, `docker`, `remote` |
| **Safety** | Permission modes (bash **and** tmux commands screened), workspace path confinement (symlink-resolving), permission-screened verification commands, completion verifier, OS sandbox (bubblewrap on Linux, Seatbelt on macOS) with env scrubbing + network egress control, docker resource/network limits |
| **Context** | Output shaping, cache-friendly microcompaction (in-place tool-output pruning), usage-driven proactive + 3-step summarization, archive-on-compaction (pruned/dropped history is demoted to session-disk buffers retrievable via `buffer_grep`/`buffer_slice`, never destroyed), durable-notes nudge before compaction, turn/context budget reminders, repetition detection |
| **Extensibility** | MCP servers (stdio, HTTP, SSE), plugin hooks, YAML recipes, subagent handoff |
| **Modes** | `standard` (fast), `rigorous` (plan → execute → critic), `readonly` |
| **Interfaces** | Headless CLI, interactive chat, JSON-RPC server with an async job queue |
| **Evaluation** | Harbor adapter + ATIF-v1.7 trajectory export |

Spreadsheet and PDF benchmarks mentioned in early docs are **eval-only targets** (see [Evaluation](#evaluation-harbor)), not core product features.

---

## Installation

```bash
git clone https://github.com/Darshan2104/Garuda-openagent.git
cd Garuda-openagent

# Core + dev tools
pip install -e ".[dev]"

# Add document tools (PDF, Excel)
pip install -e ".[docs]"

# Add Harbor eval support (optional)
pip install -e ".[eval]"
```

### Optional system dependencies

| Tool | Used for | Required? |
|------|----------|-----------|
| `tmux` | Interactive terminal tasks (`--workspace-kind tmux`) | Recommended |
| `docker` | Container isolation (`docker`, `remote` workspaces) | Optional |
| `bubblewrap` (`bwrap`) | OS sandbox on Linux (`--workspace-kind sandbox`); macOS uses the built-in Seatbelt (`sandbox-exec`) | Optional |
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

## How to use Garuda

Seven ways to drive the agent — pick the one that fits. The exhaustive flag list is in [CLI reference](#cli-reference) below.

### 1. Install

```bash
cd Garuda-openagent
pip install -e .          # Python 3.12+; installs the `garuda` command
```

### 2. Give it a model + API key

Garuda talks to any provider through [LiteLLM](https://github.com/BerriAI/litellm), so you set the provider's own env var and pass a `provider/model` string:

```bash
export OPENAI_API_KEY=...        # or ANTHROPIC_API_KEY, FIREWORKS_AI_API_KEY, ...
export GARUDA_MODEL="anthropic/claude-sonnet-5"   # optional default; else pass --model
```

Model examples: `openai/gpt-4o-mini` (built-in default), `anthropic/claude-sonnet-5`, `fireworks_ai/accounts/fireworks/models/gpt-oss-120b`.

### 3. Run a task (headless — the common case)

```bash
garuda run -t "Add a --json flag to cli.py and a test for it" \
  --model anthropic/claude-sonnet-5 \
  --workspace .
```

Handy flags: `-f task.md` (read task from file), `--agent <profile>` (`build` default; also `explore`, `plan`, `reviewer`), `--permission-mode {auto,smart,readonly,yolo}`, `--mode rigorous` (plan → execute → critic), `--max-turns N`, `--reasoning-effort high` (extended thinking), `--persistent-shell` (cwd/env/venv survive across bash calls), `--json` (stream events), `--resume latest` (continue a prior session).

### 4. Interactive session

```bash
garuda chat --model anthropic/claude-sonnet-5
```

Enter tasks at the `task>` prompt; in `smart` mode it asks before risky actions and streams tokens live.

### 5. Use it from Python (SDK)

```python
import asyncio
from garuda.sdk import SoftwareAgent

async def main():
    agent = SoftwareAgent(workspace=".", model="anthropic/claude-sonnet-5", agent="build")
    # register a custom tool for just this agent (per-instance, not global):
    # agent.register_tool(MyTool())
    result = await agent.run("Fix the failing test in tests/test_foo.py")
    print(result.success, result.final_message)

asyncio.run(main())
```

For multi-turn context, use `from garuda.sdk import Conversation` (see [Software Agent SDK](#software-agent-sdk)).

### 6. Run it as a service (job queue)

```bash
garuda serve --host 127.0.0.1 --port 8765 --max-jobs 4
```

`submit` a task → get a `job_id`, then poll `status` / `events` (cursor-based, incremental) / `result`, or `cancel`. Jobs from different workspaces run concurrently with isolated tool sets (per-run registry — no global state). A bearer token is auto-generated and printed for loopback binds; non-loopback binds refuse to start without an explicit `--token`. See [`garuda serve`](#garuda-serve--json-rpc-server--job-queue).

### 7. Configure a project with `.agent/`

Drop a single folder at your workspace root and Garuda picks everything up automatically:

```
.agent/
  agents/        # custom + sub-agent profiles (build.yaml, researcher.yaml, ...)
  skills/        # SKILL.md files (progressive-disclosure instructions)
  tools/         # *.py custom tools  (opt-in — see trust note below)
  mcp.json       # MCP servers (merged with the global ~/.agent/mcp.json)
  settings.yaml  # per-project defaults (e.g. mcp_merge)
```

- **Custom tools** → a `.py` exporting `TOOLS = [...]`, `get_tools()`, or `register(registry)`. Importing runs repo code, so it's off by default and a project **cannot enable it for itself**: enable per-run with `garuda run --load-project-tools`, or permanently with `load_project_tools: true` in your **global** `~/.agent/settings.yaml`.
- **MCP servers** → listed in `.agent/mcp.json`; restrict a profile to a subset with `mcp_servers: [name, ...]` in the profile.
- **Skills / sub-agent profiles** → `.agent/skills/<name>/SKILL.md` and `.agent/agents/<name>.yaml`; discovered the same standard way.

> `.agent/` is the standard convention; `.garuda/` still works everywhere as a back-compat alias (`.agent/` wins on conflict). The same convention applies at the user level: `~/.agent/` holds your global `settings.yaml`, `mcp.json`, and `sessions/`.

### Handy extras

```bash
garuda sessions            # list recent runs (resume any with --resume <id|latest>)
garuda mcp list            # show resolved MCP config + the tools each server exposes
garuda recipe run flow.yaml -p key=value   # run a multi-step YAML workflow
```

**Fastest start:** `pip install -e .`, `export ANTHROPIC_API_KEY=...`, then `garuda run -t "your task" --model anthropic/claude-sonnet-5`.

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
| `--agent` | Agent profile name (default: `build`; built-ins: `plan`, `explore`, `reviewer`, `harbor`) |
| `--agents-dir` | Directory with custom agent profiles (default: `.agent/agents`, `.garuda/agents`) |
| `--workspace` | Workspace root directory (default: `.`) |
| `--workspace-kind` | `local` · `sandbox` · `tmux` · `docker` · `remote` |
| `--docker-image` | Image for docker/remote workspaces (default: `ubuntu:22.04`) |
| `--docker-host` | Remote Docker daemon (`DOCKER_HOST`, e.g. `tcp://host:2375`) |
| `--docker-memory` / `--docker-cpus` | Container resource limits (default: `2g` / `2`) |
| `--no-network` | Disable network for docker/remote containers (default: bridged) |
| `--allow-network` | Allow network egress inside the OS sandbox (denied by default) |
| `--allow-unsandboxed` | Let `--workspace-kind sandbox` run unconfined if no backend exists (default: fail loudly) |
| `--mode` | `standard` · `rigorous` · `readonly` (default: the profile's own) |
| `--permission-mode` | `auto` · `smart` · `readonly` · `yolo` |
| `--mcp-config` | Path to MCP servers config (YAML or JSON); auto-discovered when omitted |
| `--load-project-tools` | Import custom tools from `.agent/tools/*.py` (runs repo code; overrides the global setting) |
| `--max-turns` | Max agent turns |
| `--reasoning-effort` | `minimal` · `low` · `medium` · `high` — extended thinking, cross-provider |
| `--thinking-budget` | Anthropic extended-thinking budget in tokens |
| `--persistent-shell` | Keep one shell alive across bash calls (cwd/env/venv persist; local env) |
| `--no-post-edit-diagnostics` | Disable the syntax check run after `edit`/`write_file` |
| `--no-verifier` | Disable completion verification gate |
| `--no-three-step-summary` | Disable 3-step context summarization |
| `--json` | Print JSONL events to stdout |
| `--trajectory` | Save event log to a JSONL file |
| `--resume` | Resume a saved session (full id, unique prefix, or `latest`) |

**Examples:**

```bash
# Rigorous mode (plan → execute → critic)
garuda run -t "Fix the failing test in tests/" --mode rigorous

# Docker-isolated run with no network
garuda run -t "Install deps and run pytest" --workspace-kind docker --no-network

# Tmux for interactive terminal work
garuda run -t "Start a server and curl it" --workspace-kind tmux

# OS sandbox (bubblewrap on Linux, Seatbelt on macOS)
garuda run -t "Refactor utils.py" --workspace-kind sandbox
```

### `garuda chat` — interactive session

```bash
garuda chat --agent build --model openai/gpt-4o-mini
```

Enter tasks at the `task>` prompt. Permission prompts appear in `smart` mode. Supports `--workspace`, `--workspace-kind`, `--agents-dir`, `--mcp-config`, `--mode`, `--json`.

### `garuda serve` — JSON-RPC server + job queue

```bash
garuda serve --host 127.0.0.1 --port 8765 \
  --max-jobs 4 --model-max-concurrency 8
```

`--max-jobs` caps concurrent jobs (default 4; excess jobs queue); `--model-max-concurrency` caps in-flight model calls per provider across all jobs (0 = unlimited). On a loopback bind a bearer token is auto-generated and printed; a non-loopback bind requires an explicit `--token` / `GARUDA_SERVE_TOKEN`.

Send HTTP POST requests with JSON-RPC 2.0 bodies:

```bash
# Health check
curl -s -X POST http://127.0.0.1:8765 \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"jsonrpc":"2.0","method":"health","id":1}'

# Blocking run (holds the connection until done, returns all events)
curl -s -X POST http://127.0.0.1:8765 \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"jsonrpc":"2.0","method":"run","params":{"task":"List files"},"id":2}'

# Async job: submit, then poll status / events / result
curl -s -X POST http://127.0.0.1:8765 \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"jsonrpc":"2.0","method":"submit","params":{"task":"List files"},"id":3}'
# -> {"result":{"job_id":"...","state":"queued","session_id":"..."}}
```

| Method | Description |
|--------|-------------|
| `health` | Server status and version |
| `run` | Blocking: execute a task and return the full event list (`params.task`, optional `model`, `agent`, `mode`, `workspace`) |
| `submit` | Enqueue a task, return `{job_id, state, session_id}` immediately |
| `status` | Job state, turn count, event count (`params.job_id`) |
| `events` | Incremental events since `params.cursor`; returns a new `cursor` |
| `result` | Final result once done (`ready`, `success`, `final_message`, `events`) |
| `cancel` | Cancel a queued/running job |
| `jobs` | List submitted jobs |
| `sessions` | List recent saved sessions |
| `list_agents` | List available agent profile names |

Jobs may target different workspaces with different `.agent/` tool sets — tool registries are layered per run, so concurrent heterogeneous jobs don't leak tools into each other.

### `garuda sessions` — list saved sessions

```bash
garuda sessions --limit 20
```

### `garuda mcp list` — inspect MCP configuration

```bash
garuda mcp list [--workspace .] [--mcp-config custom.json] [--no-connect]
```

Shows which config file(s) resolved, the servers they define, and (unless `--no-connect`) connects to each server to enumerate the tools it exposes.

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

Profiles live in `garuda/agents/defaults/`, your project's `.agent/agents/` (or `.garuda/agents/`), or an explicit `--agents-dir`.

| Profile | Access | Tools | Use case |
|---------|--------|-------|----------|
| **build** | Read/write/exec | All core + MCP | Implementation, fixes |
| **plan** | Read-only | `bash`, `read_file` | Analysis and planning |
| **explore** | Read-only | `bash`, `read_file` | Fast codebase search (subagent) |
| **reviewer** | Read-only | `bash`, `read_file` | Code review (subagent, agent.md) |
| **harbor** | YOLO eval | bash, files, patch, `task_complete` | Harbor benchmarks |

### agent.md (OpenCode-compatible)

Place markdown agents in `.agent/agents/` (or `.garuda/agents/`):

```markdown
---
name: my-coder
description: Full-stack coding agent
permission_mode: smart
tools:
  - bash
  - read_file
  - write_file
  - edit
  - invoke_subagent
path_rules:
  deny:
    - "**/.env"
    - "**/secrets/*"
bash_rules:
  ask:
    - "sudo .*"
---

You are an expert software engineer...
```

```bash
garuda run -t "..." --agent my-coder     # .agent/agents is discovered automatically
```

### Skills (universal SKILL.md format)

Add skills under `.agent/skills/` (or `.garuda/skills/`; profiles can add more via `skills_dirs:`):

```markdown
---
name: pdf-processing
description: Extract and analyze PDF documents
allowed-tools: [read_pdf, bash]
---

# PDF Processing

When reading PDFs, use read_pdf first. Summarize page by page...
```

Skills are discovered automatically and injected into the agent's system prompt. Restrict which load with `skills:` in the agent profile. When a skill's `allowed-tools` names a tool the profile doesn't grant, Garuda logs a warning.

### Subagents

The main `build` agent can delegate to isolated subagents:

```bash
# Agent calls: invoke_subagent(profile="explore", task="find auth flow")
# Also available: plan, reviewer
```

Subagents get their own permissions, event log, and tool set per profile.

### Software Agent SDK

```python
import asyncio
from garuda.sdk import SoftwareAgent

async def main():
    agent = SoftwareAgent(workspace=".", model="openai/gpt-4o-mini", agent="build")
    result = await agent.run("List Python files and summarize structure")
    print(result.final_message)

asyncio.run(main())
```

Multi-turn conversations:

```python
from garuda.sdk import Conversation

async def main():
    chat = Conversation(workspace=".", model="openai/gpt-4o-mini")
    r1 = await chat.run("Find all API routes")
    r2 = await chat.run("Add tests for the auth routes")
    await chat.close()
```

### Custom tools

Two ways to add tools, both scoped to the run (never process-global):

**File-based** — drop a module in `.agent/tools/`:

```python
# .agent/tools/hello.py
from garuda.types import ToolResult

class HelloTool:
    name = "hello"
    description = "Say hello"
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    async def execute(self, arguments, env, ctx) -> ToolResult:
        return ToolResult(tool_call_id="", content=f"Hello {arguments['name']}")

TOOLS = [HelloTool()]        # or: def get_tools(): ...  /  def register(registry): ...
```

Enable with `garuda run --load-project-tools` (per run) or `load_project_tools: true` in your **global** `~/.agent/settings.yaml` (permanent). A project's own `settings.yaml` cannot enable this for itself — importing tool modules executes repo code, so only you can grant it.

**SDK** — register on a specific agent instance:

```python
agent = SoftwareAgent(workspace=".", model="openai/gpt-4o-mini")
agent.register_tool(HelloTool())
```

If the profile has a `tools:` allowlist, add the tool's name there (or omit `tools:` to allow all discovered tools).

### Document files (PDF, Excel)

```bash
pip install -e ".[docs]"
garuda run -t "Summarize report.pdf" --agent build
```

Tools: `read_pdf`, `read_spreadsheet` (`.xlsx`, `.csv`)

### Custom agent YAML

```yaml
# .agent/agents/my-agent.yaml
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
garuda run -t "..." --agent my-agent
```

---

## MCP integration

Connect MCP servers over **stdio**, **HTTP (streamable)**, or **SSE** via a YAML or JSON
config. Both formats normalize to the same set of servers, and `${VAR}` env
interpolation works in string values (command / args / env / url / headers).

**Garuda YAML** — a `servers:` list with explicit `name`:

```yaml
# .agent/mcp.yaml
servers:
  - name: filesystem
    transport: stdio            # optional; inferred (url present -> http, else stdio)
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
    env:
      TOKEN: ${MY_TOKEN}        # env var interpolation
  - name: remote-api
    url: https://mcp.example.com/mcp   # http/sse transports use a url
    auth: ${API_TOKEN}                 # shorthand for an Authorization: Bearer header
```

**Cursor / Claude Desktop JSON** — an `mcpServers` dict keyed by name (the key
becomes the server name):

```json
// .agent/mcp.json  (or .cursor/mcp.json)
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
      "env": { "TOKEN": "${MY_TOKEN}" }
    }
  }
}
```

The snake_case alias `mcp_servers` is also accepted. If you already use Cursor,
copy your `.cursor/mcp.json` into the repo as-is — Garuda reads it directly.

### Auto-discovery & merging

Pass `--mcp-config <path>` (or set `mcp_config_path` in an agent profile) to load
a specific file alone. When neither is set, Garuda looks for:

1. `{workspace}/.agent/mcp.json` (or `.yaml` / `.yml`)
2. `{workspace}/.garuda/mcp.json` (or `.yaml` / `.yml`) — back-compat
3. `{workspace}/.cursor/mcp.json` (drop-in Cursor compat)
4. `~/.agent/mcp.json` — global (honors `GARUDA_GLOBAL_SETTINGS`)

The first project-scope file **and** the global file are both loaded and their
servers **merged** (project wins on name clash), so your global servers are always
available and a repo can add its own. Set `GARUDA_MCP_MERGE=0` (or
`mcp_merge: false` in the project's `settings.yaml`) for the legacy single-file
behavior. Which files loaded is logged at INFO — or just run `garuda mcp list`.

So dropping `.agent/mcp.json` into a repo is enough — no flag required:

```bash
garuda run -t "Use MCP tools"                           # auto-discovers + merges
garuda run -t "Use MCP tools" --mcp-config custom.json  # explicit override
```

If nothing is found, MCP is simply disabled. macOS `claude_desktop_config.json` is
**not** auto-read (wrong scope) — copy or symlink it to one of the paths above if
you want it. Restrict a profile to specific servers with `mcp_servers: [name, ...]`.

MCP tools are namespaced as `mcp__<server>__<tool>`.

---

## Workspaces

| Kind | Description |
|------|-------------|
| `local` | Run commands directly on the host (default) |
| `sandbox` | OS-level isolation — bubblewrap on Linux, Seatbelt on macOS; env scrubbed, network denied by default (`--allow-network` to permit); fails loudly if no backend unless `--allow-unsandboxed` |
| `tmux` | Persistent tmux session for interactive terminals |
| `docker` | Ephemeral container with workspace mounted at `/workspace`; memory/CPU/pids limits, `--no-network` to isolate |
| `remote` | Docker on a remote daemon via `--docker-host` / `DOCKER_HOST` |

**Sandbox guarantees, honestly stated:** on both backends, secrets are scrubbed from
the environment and network egress is denied by default; writes are confined to the
workspace. On Linux (bubblewrap) reads are also confined via an explicit allowlist.
On macOS, Seatbelt cannot practically confine `file-read*` without breaking basic
process execution, so file *reads* are not confined there — use `docker` when that
matters. The agent-facing file tools additionally resolve symlinks and refuse paths
escaping the workspace on every platform.

---

## Permissions

| Mode | Behavior |
|------|----------|
| `smart` | Allow safe ops; prompt for risky commands (chat mode) |
| `auto` | Auto-approve most tool calls |
| `readonly` | Deny writes and patches |
| `yolo` | Allow everything (eval/sandboxed use only) |

Configure per-agent in YAML/agent.md:

```yaml
permission_mode: smart
tool_rules:
  write_file: allow
  bash: allow
path_rules:
  deny: ["**/.env", "**/id_rsa"]
  ask: ["**/package-lock.json"]
bash_rules:
  deny: ["rm -rf /"]
  ask: ["sudo .*"]
```

The `task_complete` tool triggers a **completion verifier** that checks summary quality and optional verification commands before accepting task completion.

### Hooks

Shell-command hooks fire on lifecycle events and around tool calls (a `before_tool`
hook exiting with code 2 blocks the call):

```yaml
# ~/.agent/settings.yaml   (global — always trusted)
hooks:
  before_tool:
    - match: "bash"
      command: "./check.sh"
  session_start:
    - command: "echo start"
```

A **project's own** `settings.yaml` may declare hooks too, but they only run if you
set `trust_project_hooks: true` in your global settings — same trust boundary as
`load_project_tools`: a cloned repo cannot self-authorize running shell commands.

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
├── agents/          # profile loader (YAML + agent.md) + default profiles
├── config/          # .agent/ home resolver, recipes, defaults
├── core/            # agent loop, events, permissions, verifier, sessions, rigorous mode
├── context/         # context manager, summarizer, condenser, output shaping
├── model/           # LiteLLM adapter, concurrency governor, ScriptModel
├── skills/          # SKILL.md discovery + progressive disclosure
├── tools/           # bash, files, search, tmux, web, buffers, subagent, registry
├── workspace/       # local, sandbox (+policy), tmux, docker, remote
├── mcp/             # MCP client (stdio/http/sse) + config resolution
├── plugins/         # lifecycle + tool hooks
├── sdk/             # SoftwareAgent / Conversation programmatic API
├── observability/   # OTLP tracing (GARUDA_TRACING)
├── eval/            # Harbor adapter, ATIF export, benchmark configs
└── interfaces/      # CLI, JSON-RPC server, job queue, runner

docs/
├── GARUDA_OPEN_AGENT_RFC.md    # Architecture RFC
├── MODULES.md                  # Module work breakdown
├── ENGINEERING_PLAN.md         # Running engineering log / status updates
└── CONFIG_SCALABILITY_PLAN.md  # .agent/ home + scalability design

tests/                          # ~460 tests (unit + integration + live-sandbox opt-ins)
└── fixtures/                   # MCP echo server for tests
```

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev,eval]"

# Run all tests
pytest tests/ -v

# Include live OS-sandbox tests (macOS Seatbelt / Linux bwrap hosts)
GARUDA_LIVE_SANDBOX=1 pytest tests/ -v
```

**Current test status:** 459 passed, 7 skipped (tmux-dependent tests skip when `tmux` is absent; live Seatbelt tests are opt-in via `GARUDA_LIVE_SANDBOX=1`).

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `GARUDA_MODEL` | Default model for CLI commands |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / … | Provider credentials (any LiteLLM-supported var) |
| `GARUDA_GLOBAL_SETTINGS` | Path to the global `settings.yaml` (default `~/.agent/settings.yaml`) — the trust anchor for `load_project_tools` / `trust_project_hooks` |
| `GARUDA_SESSIONS_DIR` | Session store location (default `~/.agent/sessions`) |
| `GARUDA_MCP_MERGE` | `0` to disable project+global MCP config merging |
| `GARUDA_MODEL_MAX_CONCURRENCY` | Cap concurrent model calls per provider (governor) |
| `GARUDA_SERVE_TOKEN` | Bearer token for `garuda serve` |
| `GARUDA_TRACING` | Enable OTLP tracing |
| `DOCKER_HOST` | Remote Docker daemon for `--workspace-kind remote` |

---

## Documentation

- [Architecture RFC](docs/GARUDA_OPEN_AGENT_RFC.md) — design goals, interfaces, roadmap
- [Module breakdown](docs/MODULES.md) — module-by-module status
- [Engineering plan](docs/ENGINEERING_PLAN.md) — running log of shipped work
- [Config & scalability plan](docs/CONFIG_SCALABILITY_PLAN.md) — the `.agent/` home + job-queue design

---

## License

MIT
