# Garuda Open Agent

Garuda is a provider-agnostic coding-agent harness for terminal and software
engineering work. It combines a configurable agent loop with real tools,
workspaces, permissions, context management, evidence-based completion, sessions,
MCP, and observability.

> The harness is the product, not the model.

**Python:** 3.12+ · **License:** MIT · **Current release:** 1.2.0

## What it does

- Runs LiteLLM-supported providers with retries, streaming, reasoning support,
  prompt caching, concurrency control, and reproducible cost accounting.
- Gives agents safe, inspectable tools for shell, files, edits, search, documents,
  tmux, web access, MCP, skills, custom tools, and subagents.
- Persists sessions, records event logs and trajectories, and requires useful
  completion evidence rather than accepting a bare claim of success.
- Verifies results with checks you choose (`--check`, trusted project checks),
  kept apart from the agent's own completion check.
- Runs locally, in a sandbox, tmux, Docker, or a remote Docker environment, and
  in per-session Git worktrees merged only after checks pass in Docker.
- Queues background runs under per-runtime capacity, with approvals you can
  answer from another terminal or the dashboard.
- Lets you define agents (`garuda agent new`), name roles with exact models,
  and run plan → build → review flows with independent reviews and consults.
- Provides a CLI, SDK, JSON-RPC job service, and local web dashboard.
- Launches subscription-backed coding harnesses through ACP and supports
  native-to-ACP handoff while preserving Garuda's native runtime. Garuda records
  ACP lifecycle and workspace changes, but does not apply its native completion
  verifier to ACP results. See [External harnesses](docs/guides/external-harnesses.md).

## Quick start

```bash
git clone https://github.com/Darshan2104/Garuda-openagent.git
cd Garuda-openagent
pip install -e ".[dev]"

export OPENROUTER_API_KEY=sk-or-...
garuda run -t "List all Python files in the current directory" --mode readonly
```

Model-backed runs can incur provider charges. The built-in default is DeepSeek
through OpenRouter; choose another supported model and matching credential with
`--model` or `GARUDA_MODEL`.

```bash
garuda chat --agent build
garuda run -t "Fix the failing test" --mode rigorous
garuda run -t "Map the authentication flow" --mode readonly
garuda web --allow-workspace .
```

Use Docker for untrusted work. Permission rules and the `sandbox` workspace kind
are guardrails; macOS Seatbelt reduces write/network blast radius but does not
confine host file reads.

## Documentation

Read the searchable documentation at
[darshan2104.github.io/Garuda-openagent](https://darshan2104.github.io/Garuda-openagent/).

| Start here | Then |
|---|---|
| [Quickstart](docs/guides/getting-started.md): install and run a safe first task | [Use cases](docs/use-cases/index.md): recipes from easy to hard |
| [How Garuda works](docs/guides/how-garuda-works.md): runtimes, workspaces, modes, sessions | [Command builder](docs/guides/command-builder.md): click to build a command |
| [Cheat sheet](docs/reference/cheat-sheet.md): everyday commands | [Troubleshooting](docs/reference/troubleshooting.md): common problems |
| [Feature index](docs/reference/features.md): every feature and its demo | [Teams of roles](docs/use-cases/teams.md): roles, flows, reviews |

Guides: [Safety and workspaces](docs/guides/safety-and-workspaces.md) ·
[Configuration](docs/guides/configuration.md) ·
[Agent definitions](docs/guides/agents.md) ·
[Sessions and flows](docs/guides/sessions-and-flows.md) ·
[External harnesses](docs/guides/external-harnesses.md) ·
[Web dashboard](docs/guides/web-dashboard.md) ·
[CLI reference](docs/reference/cli.md) ·
[Evaluation](docs/evaluation/index.md)

Contributors and coding agents should start with [AGENTS.md](AGENTS.md), then
[Architecture](docs/ARCHITECTURE.md) and
[Development](docs/development/development.md). Open work is in the
[backlog](docs/BACKLOG.md).

## Installation extras

```bash
pip install -e ".[docs]"            # PDF and spreadsheet readers
pip install -e ".[site]"            # MkDocs documentation site
pip install -e ".[eval]"            # Harbor integration
pip install -e ".[observability]"   # OpenTelemetry export
```

## Development

```bash
pytest -q
pip install ruff   # not part of the dev extra
ruff check garuda tests
```

See [Development](docs/development/development.md) for reproducible dependency
checks and optional live integration tests.

## License

MIT
