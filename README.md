# Garuda Open Agent

**Universal, provider-agnostic agent harness** for terminal and software engineering tasks.

Garuda is a runtime that runs any LLM against real environments using tools (bash, files, edits, tmux, MCP). It combines ideas from OpenCode, Goose, Terminus-2/KIRA, mini-SWE-agent, and Harbor into one configurable, auditable system.

> **Core thesis:** The harness is the product, not the model.

**Version:** 1.1.1 · **Python:** 3.12+ · **License:** MIT

---

## Features

| Area | Capabilities |
|------|--------------|
| **Models** | Any provider via [LiteLLM](https://github.com/BerriAI/litellm) (`openai/…`, `anthropic/…`, `fireworks_ai/…`, etc.) — retries/backoff, request timeouts, Anthropic prompt caching, extended thinking (`--reasoning-effort` cross-provider, `--thinking-budget` for Anthropic), reasoning preserved across tool-call turns — `thinking_blocks` on Anthropic always, flat `reasoning_content` elsewhere behind opt-in `preserve_reasoning` (measured neutral on minimax-m2.5; see [backlog](docs/BACKLOG.md)), per-provider concurrency governor |
| **Tools** | `bash`, `bash_background`/`task_output`/`kill_task`, `edit` (string replace with shift recovery — auto-recovers from pasted line-number prefixes, CRLF/LF, and indentation drift), `multi_edit` (several atomic edits to one file in one call), `read_file` (line-numbered, offset/limit), `write_file`, `grep` (ripgrep when available — gitignore-aware — else `grep -E`), `glob`, `ls`, `todo`, `update_goal` (north-star objective, re-pinned across compaction), `web_fetch`, `web_search`, `read_pdf`, `read_spreadsheet`, `tmux_exec`, `tmux_capture`, `image_read`, `invoke_subagent`, `buffer_grep`/`buffer_slice`/`buffer_list`/`buffer_query` (archived-context retrieval), `contract` (resolve acceptance criteria — several per call via `marks`), `task_complete` + MCP (`search_tool`/`use_tool` lazy discovery when many are present) |
| **Sessions** | Every run persists to `~/.agent/sessions/` (`~/.garuda` back-compat, `GARUDA_SESSIONS_DIR` override); `garuda sessions` lists, `garuda run --resume <id\|latest>` continues with full context |
| **Hooks** | Lifecycle + tool shell-command hooks from the **global** `~/.agent/settings.yaml` (exit 2 blocks the tool call); hooks in a project's own `settings.yaml` run only if you set `trust_project_hooks: true` globally — a cloned repo can't self-authorize running its commands |
| **Project memory** | `AGENTS.md` / `GARUDA.md` in the workspace root is injected as project instructions |
| **Agents** | YAML or **agent.md** profiles: `build`, `plan`, `explore`, `reviewer`, `harbor` — plus your own in `.agent/agents/` |
| **Skills** | Universal `SKILL.md` format — auto-discovered from `.agent/skills/`, injected into the system prompt; `allowed-tools` frontmatter validated against the profile's tool grants |
| **Custom tools** | Drop `*.py` modules in `.agent/tools/` (opt-in — global setting or `--load-project-tools`) or register per-instance via the SDK |
| **Subagents** | Main agent spins up isolated subagents via `invoke_subagent` |
| **SDK** | `garuda.sdk.SoftwareAgent` — OpenHands-style programmatic API |
| **Workspaces** | `local`, `sandbox`, `tmux`, `docker`, `remote` |
| **Safety** | Permission modes (bash **and** tmux commands screened), workspace path confinement (symlink-resolving), permission-screened verification commands, completion verifier, post-edit diagnostics (syntax check + fast semantic lint via ruff, surfaced to the model), OS sandbox (bubblewrap on Linux, Seatbelt on macOS) with env scrubbing + network egress control, docker resource/network limits |
| **Context** | Output shaping, cache-friendly microcompaction (in-place tool-output pruning), usage-driven proactive + 3-step summarization, archive-on-compaction (pruned/dropped history is demoted to session-disk buffers retrievable via `buffer_grep`/`buffer_slice`, never destroyed), goal + todo list re-pinned after compaction (survive summarization), durable-notes nudge before compaction, turn/context budget reminders, session-wide action memo (a repeated read-only call is answered from the earlier observation instead of re-run; any mutating call invalidates it) and repetition detection |
| **Extensibility** | MCP servers (stdio, HTTP, SSE) with lazy `search_tool`/`use_tool` discovery above `GARUDA_MCP_MAX_DIRECT_TOOLS` (default 10) so many tools don't bloat the prompt, plugin hooks, YAML recipes, subagent handoff |
| **Run modes** | One flag picks a gate posture: `interactive` (default — no model-call gates), `eval` (full completion-gate stack: LLM judge, acceptance contract, discriminating + stable evidence, side-effect sweep), `rigorous` (eval gates + plan → execute → critic), `readonly` (interactive gates, permissions forced read-only). See [Run modes](#run-modes). |
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

## Run modes

`--mode` picks a **run posture**: one flag that implies a coherent set of completion
gates, so you don't configure five booleans to say "cheap" or "strict".

| Mode | Completion gates | Extra model calls | Use it for |
|---|---|---|---|
| `interactive` *(default)* | structural gate only — `task_complete` must carry evidence | none | day-to-day runs |
| `eval` | LLM judge, acceptance contract, discriminating evidence, stable re-verification, side-effect sweep | ~2 per completion attempt, plus a re-run of each verification command | graded / benchmark runs |
| `rigorous` | `eval` gates plus a plan → execute → critic agent | `eval` cost × repair rounds | maximum scrutiny |
| `readonly` | `interactive` gates, permissions forced read-only | none | inspection without writes |

`standard` is a back-compat alias for `interactive`.

**The default is deliberately cheap.** A bare `garuda run` pays for the task plus
local checks (post-edit syntax and lint, the one-shot environment probe) and
nothing else. The strict stack is real work — an LLM judge reading the task
statement back against observed output, acceptance criteria pinned across
compaction, every verification command re-run to prove it isn't order-dependent —
and it belongs to a run whose product is a graded pass. Ask for it with
`--mode eval`.

If you are reproducing benchmark numbers, use `--mode eval`. Harbor runs pin it
automatically.

**The acceptance contract under `eval`.** Criteria are derived from the task
statement once at run start and pinned across compaction; the `contract` tool is
how the agent resolves them, and the completion gate refuses while any are
outstanding. Marking one `verified` requires a note saying what was run and what
it showed. Resolve them in batches — one call carries a `marks` list of
`{id, status, note}` and can settle every criterion at once; a partly malformed
batch still applies its valid entries. The `build` and `harbor` profiles both
grant `contract` for this reason (a profile that omits it under `eval` makes every
`task_complete` unsatisfiable).

Precedence, widest to narrowest: built-in defaults → the mode preset → fields your
profile YAML declares explicitly → explicit CLI flags. So a profile that sets
`enable_acceptance_contract: true` keeps it under `--mode interactive`, and an
explicit `--permission-mode yolo` overrides `--mode readonly`.

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
| `--mode` | Run posture: `interactive` (default) · `eval` · `rigorous` · `readonly`. See [Run modes](#run-modes). |
| `--permission-mode` | `auto` · `smart` · `readonly` · `yolo` |
| `--mcp-config` | Path to MCP servers config (YAML or JSON); auto-discovered when omitted |
| `--load-project-tools` | Import custom tools from `.agent/tools/*.py` (runs repo code; overrides the global setting) |
| `--max-turns` | Max agent turns |
| `--deadline-sec` | Wall-clock budget for the run. The agent paces against it and reserves turns to finish — a turn count can't express "most of my time is gone" when one command may block for minutes |
| `--reasoning-effort` | `minimal` · `low` · `medium` · `high` — extended thinking, cross-provider |
| `--thinking-budget` | Anthropic extended-thinking budget in tokens |
| `--persistent-shell` | Keep one shell alive across bash calls (cwd/env/venv persist; local env) |
| `--no-post-edit-diagnostics` | Disable the syntax check run after `edit`/`write_file` |
| `--no-post-edit-lint` | Disable the fast semantic lint (Python/ruff) run after `edit`/`write_file` |
| `--no-bootstrap` | Skip the session-start environment probe (cold start — the agent discovers the environment itself) |
| `--no-verifier` | Disable completion verification gate |
| `--no-three-step-summary` | Disable 3-step context summarization |
| `--json` | Print JSONL events to stdout |
| `--trajectory` | Save event log to a JSONL file |
| `--resume` | Resume a saved session (full id, unique prefix, or `latest`) |

**Examples:**

```bash
# Full completion-gate stack (the benchmark configuration)
garuda run -t "Fix the failing test in tests/" --mode eval

# Rigorous mode (eval gates plus plan → execute → critic)
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
| **plan** | Read-only | `bash`, `read_file`, `grep`, `glob`, `ls`, `task_complete` | Analysis and planning |
| **explore** | Read-only | `bash`, `read_file`, `grep`, `glob`, `ls`, `task_complete` | Fast codebase search (subagent) |
| **reviewer** | Read-only | `bash`, `read_file`, `task_complete` | Code review (subagent, agent.md) |
| **harbor** | YOLO eval | `bash`, `read_file`, `write_file`, `edit`, `multi_edit`, `grep`, `glob`, `ls`, `todo`, `update_goal`, `contract`, `task_complete` | Harbor benchmarks (runs under `eval` mode) |

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

**Lazy discovery (token-lean).** When the connected servers expose more than
`GARUDA_MCP_MAX_DIRECT_TOOLS` tools (default 10), Garuda stops injecting every tool
schema into the prompt and instead exposes two meta-tools — `search_tool(query)` to
find tools by keyword and `use_tool(name, arguments)` to invoke one. This keeps
requests small no matter how many MCP tools are connected. Below the threshold, tools
are listed directly as before. Set the threshold to a large number to always list
directly.

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
matters. Selecting the Seatbelt backend logs this caveat once at startup, so it is
visible at runtime and not only here: treat macOS Seatbelt as a blast-radius reducer
rather than a confinement boundary. The agent-facing file tools additionally resolve
symlinks and refuse paths escaping the workspace on every platform.

Signals are scoped rather than blanket-allowed: a sandboxed command may signal
itself and its own process group — so `kill`, `timeout`, `make -j` and test runners
work — but not any other process, so it cannot signal the agent or the host.

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

### Adapter options

Set these under the agent's `kwargs` in a Harbor job config. They are
benchmark-scoped — none of them change how `garuda run` behaves.

| kwarg | Purpose |
|---|---|
| `agent_profile` | Profile to run (default `harbor`) |
| `max_turns` | Turn cap for the run |
| `permission_mode` | Usually `yolo` inside a disposable container |
| `agent_timeout_sec` | Set to the same value as `override_timeout_sec`. Harbor enforces its timeout by killing the agent and tells it nothing, so this is what gives the agent a wall-clock budget to pace against |
| `deadline_margin` | Fraction of that budget held back for wind-down (default `0.1`) |
| `system_prompt` | Replace the profile's base prompt inline |
| `system_prompt_path` | Replace it from a file — easier to diff and version than a YAML block |
| `append_system_prompt` | Keep the profile's prompt and add to it |
| `agents_dir` | Extra profile directory, so `agent_profile` can name a fully custom YAML |

**Why the prompt is a job-level knob.** A harness score mixes scaffold quality with
instruction quality, and the two are only separable if the prompt can be swapped
per job without editing a profile the rest of the system shares. The `harbor`
profile ships a deliberately minimal prompt so the baseline measures the scaffold;
supply your own here to measure the difference.

Overrides replace the profile's *base* prompt, so the discovered-skills block and
`AGENTS.md` project memory are still appended as usual. A `system_prompt_path` that
cannot be read is an error, not a fallback — silently reverting to the profile
prompt would score a run that never used the prompt under test.

```yaml
agents:
- import_path: garuda.eval.harbor_adapter:GarudaHarborAgent
  model_name: openrouter/minimax/minimax-m2.5
  override_timeout_sec: 900
  kwargs:
    agent_profile: harbor
    max_turns: 120
    permission_mode: yolo
    agent_timeout_sec: 900        # mirrors override_timeout_sec
    system_prompt_path: prompts/experiment-a.md
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
├── core/            # turn loop + run state, steering, tool runner, completion gate,
│                   # run modes, events, permissions, verifier, sessions, rigorous mode
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
├── ARCHITECTURE.md             # Orientation: postures, boundaries, run path
├── MODULES.md                  # Module map
├── BACKLOG.md                  # Living residuals
└── archive/                    # Dated: original RFC, engineering log, closed ledgers

tests/                          # 839 tests (unit + integration + live-sandbox opt-ins)
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

**Current test status:** 831 passed, 8 skipped of 839 collected (tmux-dependent tests skip when `tmux` is absent; live Seatbelt tests are opt-in via `GARUDA_LIVE_SANDBOX=1`).

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `GARUDA_MODEL` | Default model for CLI commands |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / … | Provider credentials (any LiteLLM-supported var) |
| `GARUDA_GLOBAL_SETTINGS` | Path to the global `settings.yaml` (default `~/.agent/settings.yaml`) — the trust anchor for `load_project_tools` / `trust_project_hooks` |
| `GARUDA_SESSIONS_DIR` | Session store location (default `~/.agent/sessions`) |
| `GARUDA_MCP_MERGE` | `0` to disable project+global MCP config merging |
| `GARUDA_MCP_MAX_DIRECT_TOOLS` | Above this many MCP tools, expose them via `search_tool`/`use_tool` instead of listing all schemas (default 10) |
| `GARUDA_MODEL_MAX_CONCURRENCY` | Cap concurrent model calls per provider (governor) |
| `GARUDA_TOKEN_PRICES` | Per-model rate overrides in USD per million tokens, e.g. `'{"minimax-m2.5": {"input": 0.302, "cache_read": 0.033, "output": 1.33}}'`. Used only when the provider doesn't report a per-call cost; keys match as substrings of the model name |
| `GARUDA_SERVE_TOKEN` | Bearer token for `garuda serve` |
| `GARUDA_TRACING` | Enable OTLP tracing |
| `DOCKER_HOST` | Remote Docker daemon for `--workspace-kind remote` |

---

## Documentation

Start here, in order:

- [Architecture](docs/ARCHITECTURE.md) — run postures, trust boundaries, the run path, where to change what
- [Module map](docs/MODULES.md) — what each package owns and the file to open first
- [Open backlog](docs/BACKLOG.md) — residuals only; nothing in it is marked done

[docs/archive/](docs/archive/) holds the original RFC, the engineering log, and the
closed review ledgers, dated. Kept for provenance and **not** maintained — don't
read them as current behavior.

---

## License

MIT
