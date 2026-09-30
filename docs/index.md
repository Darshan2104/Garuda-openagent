# Garuda documentation

Garuda is the runtime around a coding model or external coding harness: it owns
workspace selection, permission guardrails, context, evidence, sessions, and
observability. Start with a read-only native run, then follow the task-specific
guides below.

## Start here

1. [Getting started](guides/getting-started.md) — install Garuda, configure a
   model, run a safe first task, and inspect its session.
2. [Using Garuda](guides/using-garuda.md) — choose an interface, mode,
   workspace, profile, and runtime for everyday work.
3. [Configuration](guides/configuration.md) — configure models, collection,
   MCP, runtime selection, permissions, hooks, and environment variables.
4. [Safety and workspaces](guides/safety-and-workspaces.md) — choose between
   guardrails, host execution, OS sandboxing, Docker, and remote Docker.

## Feature map

| Feature | Command | Guide |
|---|---|---|
| Safe first inspection | `garuda run -t TASK --mode readonly` | [Getting started](guides/getting-started.md) |
| Headless task | `garuda run -t TASK` | [Using Garuda](guides/using-garuda.md#choose-an-interface) |
| Interactive conversation | `garuda chat` | [Using Garuda](guides/using-garuda.md#choose-an-interface) |
| Run modes and completion gates | `garuda run -t TASK --mode eval` | [Run modes](guides/using-garuda.md#choose-a-run-mode) |
| Sessions and continuation | `garuda sessions` | [Sessions](guides/using-garuda.md#sessions-and-continuation) |
| Workspace backends and network posture | `garuda run -t TASK --workspace-kind docker --no-network` | [Safety and workspaces](guides/safety-and-workspaces.md) |
| Profiles, project instructions, and skills | `garuda run -t TASK --agent reviewer` | [Profiles and skills](guides/using-garuda.md#profiles-project-instructions-and-skills) |
| MCP servers | `garuda mcp list --no-connect` | [Configuration](guides/configuration.md#mcp) |
| YAML recipes | `garuda recipe run workflow.yaml` | [MCP, recipes, hooks, and tools](guides/using-garuda.md#mcp-recipes-hooks-and-project-tools) |
| Local web dashboard | `garuda web --read-only` | [Web dashboard](guides/web-dashboard.md) |
| External ACP runtimes | `garuda runtime list` | [External harnesses](guides/external-harnesses.md) |
| Native-to-ACP handoff and recovery | `garuda runtime handoff --session S --to R [--confirm]` | [External harnesses](guides/external-harnesses.md) |
| JSON-RPC job service | `garuda serve` | [SDK and service](guides/using-garuda.md#sdk-and-service) |
| Trajectories and benchmarks | `garuda run -t TASK --trajectory run.jsonl` | [Evaluation](evaluation/index.md) |
| Paired dual-model reports | `garuda eval dual-model report ...` | [Dual-model evaluation](evaluation/dual-model-routing.md) |

Native and ACP runs do not have identical behavior. Native runs use Garuda's
model loop, tools, permissions, and completion gates. ACP runs use an external
harness: Garuda records lifecycle and workspace evidence but does not verify the
harness result with the native completion gate. Runtime-specific details belong
in the [external harness guide](guides/external-harnesses.md).

## Reference and advanced operation

- [CLI reference](reference/cli.md) — command inventory and flags.
- [Safety and workspaces](guides/safety-and-workspaces.md) — permissions,
  confinement, network posture, trusted extensions, and pre-run checks.
- [External harnesses](guides/external-harnesses.md) — vendor setup, runtime
  discovery, ACP limitations, and custom servers.
- [Web dashboard](guides/web-dashboard.md) — local browser UI, grounding,
  approvals, and runtime controls.
- [Evaluation](evaluation/index.md) — trajectories, benchmarks, live harness
  checks, collection experiments, and paired reports.
- [Browser checks](development/browser-checks.md) — opt-in real-browser
  validation for dashboard changes.

## Understand and extend Garuda

- [Architecture](ARCHITECTURE.md)
- [Module map](MODULES.md)
- [Development](development/development.md)
- [Major changes](major-changes.md)
- [Contributor guide](../AGENTS.md)

## Open work and history

- [Open backlog](BACKLOG.md) — unfinished work only.
- [Roadmaps and issue catalog](roadmap/index.md) — planned work, not current
  behavior.
- [Design records](design/index.md) and [implementation plans](plans/index.md) —
  dated rationale and delivery records, not current command reference.
- [Archive](archive/index.md) — historical records retained for provenance.
