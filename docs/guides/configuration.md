# Configuration

## Project home

Garuda discovers project configuration from `.agent/`; `.garuda/` remains a backwards-compatible alias.

```text
.agent/
├── agents/       # YAML or agent.md profiles
├── skills/       # <name>/SKILL.md
├── tools/        # opt-in Python tools
├── mcp.json      # project MCP servers
└── settings.yaml # project defaults
```

`AGENTS.md` or `GARUDA.md` at the workspace root is included as project instructions. Root-level project memory must not self-authorize project tools or hooks; those trust decisions live in global settings.

## Models

Set `GARUDA_MODEL` or pass `--model provider/model`. The provider credential must match the selected model. LiteLLM handles normal inference routing, retries, prompt caching, streaming, and reasoning settings.

## Model transports

Direct transports join `garuda/model/transports.py` only with all six
admission artifacts: a vendor-support citation (https, public docs), a
capability declaration, a test double, an opt-in integration test, cost
semantics with unknown-kept-unknown, and migration notes. Private endpoints,
reverse-engineered subscription paths, and copied OAuth material are never
admissible — subscription-backed agents stay behind ACP runtimes instead.

## MCP

Garuda resolves `.agent/mcp.json`/YAML, `.garuda/` compatibility files, `.cursor/mcp.json`, and global `~/.agent/mcp.json`. Project and global servers merge by default, with project entries winning name collisions. Above the direct tool threshold, Garuda exposes lazy `search_tool` and `use_tool` meta-tools.

```bash
garuda mcp list
garuda run -t "Use the issue tracker" --mcp-config custom-mcp.json
```

## Permissions and hooks

Use profile permission rules for guardrails and a Docker workspace when a real confinement boundary is needed. Project hooks and project Python tools remain opt-in because cloned repository code must not self-authorize execution.

```yaml
# ~/.agent/settings.yaml
trust_project_hooks: true
load_project_tools: true
```

## Environment variables

| Variable | Purpose |
|---|---|
| `GARUDA_MODEL` | Default model selection |
| `GARUDA_GLOBAL_SETTINGS` | Global settings path |
| `GARUDA_SESSIONS_DIR` | Session-store location |
| `GARUDA_MCP_MERGE` | Disable project/global MCP merge with `0` |
| `GARUDA_MCP_MAX_DIRECT_TOOLS` | Direct MCP schema threshold |
| `GARUDA_MODEL_MAX_CONCURRENCY` | Per-provider model-call cap |
| `GARUDA_TOKEN_PRICES` | Reproducible price overrides |
| `GARUDA_SERVE_TOKEN` | JSON-RPC server bearer token |
| `GARUDA_TRACING` | Enable OTLP tracing |
