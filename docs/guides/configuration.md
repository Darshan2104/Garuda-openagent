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

### Bounded collection model

An independently bound collection model is inert unless trusted configuration
also enables collection. The reasoning model remains the controller and must
explicitly call `delegate_collection`; the worker cannot edit the workspace or
complete the parent task. `--no-collection` removes the role and tool entirely.

```yaml
# ~/.agent/settings.yaml
models:
  strong:
    model: openrouter/example/reasoning
  fast-reader:
    model: openrouter/example/fast-reader
model_bindings:
  default:
    reasoning: strong
    collection: fast-reader
model_bindings_default: default
collection:
  enabled: true
  profile: explore
  handoff: brief            # brief or none; full is rejected
  budget:
    max_jobs_per_run: 8
    max_parallel_jobs: 3
    max_turns_per_job: 12
    max_tokens_per_job: 30000
```

Each job uses a separate collection-model context and event log. A brief
handoff contains the bounded working-state card and referenced buffer pointers,
not the parent transcript. Requests may narrow workspace paths and web-source
URL prefixes; evidence outside those scopes or referencing a missing buffer is
rejected. The returned parent tool result is a bounded JSON report, while the
full child trace is stored under the parent session's `subagents/` directory.
If an ordinary collection-worker attempt ends after evidence gathering, Garuda
makes one terminal-only submission attempt with only `submit_collection`
available; the same report and evidence validation still applies.

The non-mutating toolkit and path checks are guardrails. Use a read-only mount
or isolated snapshot when strict write confinement is required.

## Model transports

Direct transports join `garuda/model/transports.py` only with all six
admission artifacts: a vendor-support citation (https, public docs), a
capability declaration, a test double, an opt-in integration test, cost
semantics with unknown-kept-unknown, and migration notes. Private endpoints,
reverse-engineered subscription paths, and copied OAuth material are never
admissible — subscription-backed agents stay behind ACP runtimes instead.

## MCP

Garuda resolves `.agent/mcp.json`/YAML, `.garuda/` compatibility files, `.cursor/mcp.json`, and global `~/.agent/mcp.json`. Project and global servers merge by default, with project entries winning name collisions. Above the direct tool threshold, Garuda exposes lazy `search_tool` and `use_tool` meta-tools.

Collection workers treat MCP tools as effect-unknown by default. A user may
authorize one exact remote tool for collection in the global
`~/.agent/mcp.json` entry. The same `tool_effects` key in project configuration
is ignored; a repository cannot grant its own tools collection authority.

```json
{
  "mcpServers": {
    "docs": {
      "command": "docs-mcp",
      "tool_effects": {
        "fetch_document": "read_only"
      }
    }
  }
}
```

This declaration is a trusted guardrail assertion, not confinement or proof
about the remote implementation. Side-effecting and unknown tools remain absent
from collection workers, and lazy `use_tool` calls re-check the selected tool.

```bash
garuda mcp list
garuda run -t "Use the issue tracker" --mcp-config custom-mcp.json
```

## Initial runtime selection

`garuda run` picks the runtime a session starts on: an explicit `--runtime`,
then a profile pin, a trusted rule, the optional classifier, the configured
default, and finally built-in native. Rules, defaults, and the classifier
live only in the global `~/.agent/settings.yaml`.

```yaml
# ~/.agent/settings.yaml
routing:
  default_runtime: native
  classifier:
    enabled: true
    model_role: collection        # or reasoning
    allow_reasoning_fallback: false
    minimum_confidence: 0.75
    candidates: [native, codex]   # omit to offer every configured runtime
    max_output_tokens: 256
    timeout_sec: 20
    on_failure: default
```

The classifier is called only when no explicit choice, profile pin, or rule
selects a runtime. It gets the task text, agent, mode, language and marker-file
names, and the approved candidates with their capabilities. It gets no tools
and no file contents. Its answer is revalidated. An invalid, low-confidence,
incapable, or timed-out answer uses `default_runtime`. Without a `collection`
model in the global default binding (or the alias named by `model_binding`),
classification is skipped unless `allow_reasoning_fallback` is true. It is also skipped when no model binding is configured at all. The call makes one attempt and drops thinking and reasoning settings so `max_output_tokens` holds. A
project `.agent/settings.yaml` may only opt out with
`routing: {classifier: false}`.

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
