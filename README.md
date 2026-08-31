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
- Runs locally, in a sandbox, tmux, Docker, or a remote Docker environment.
- Provides a CLI, SDK, JSON-RPC job service, and local web dashboard.
- Is planned to supervise subscription-backed coding harnesses through ACP while
  preserving Garuda's native runtime. See the
  [ACP orchestration roadmap](docs/roadmap/2026-08-acp-orchestration.md).

## Quick start

```bash
git clone https://github.com/Darshan2104/Garuda-openagent.git
cd Garuda-openagent
pip install -e ".[dev]"

export OPENROUTER_API_KEY=sk-or-...
garuda run -t "List all Python files in the current directory"
```

```bash
garuda chat --agent build
garuda run -t "Fix the failing test" --mode rigorous
garuda run -t "Map the authentication flow" --mode readonly
garuda web --workspace .
```

Use Docker for untrusted work. macOS Seatbelt reduces write/network blast radius
but does not confine host file reads.

## Documentation

- [Getting started](docs/guides/getting-started.md)
- [Using Garuda](docs/guides/using-garuda.md)
- [Configuration](docs/guides/configuration.md)
- [Web dashboard](docs/guides/web-dashboard.md)
- [CLI reference](docs/reference/cli.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Development](docs/development/development.md)
- [Evaluation](docs/evaluation/index.md)
- [Open backlog](docs/BACKLOG.md)

The complete documentation map is at [docs/index.md](docs/index.md). Contributors
and coding agents should start with [AGENTS.md](AGENTS.md).

## Installation extras

```bash
pip install -e ".[docs]"            # PDF and spreadsheet readers
pip install -e ".[eval]"            # Harbor integration
pip install -e ".[observability]"   # OpenTelemetry export
```

## Development

```bash
pytest -q
ruff check garuda tests
```

See [Development](docs/development/development.md) for reproducible dependency
checks and optional live integration tests.

## License

MIT
