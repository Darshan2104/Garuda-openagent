# Garuda documentation

Garuda is a universal, provider-agnostic agent harness for terminal and software engineering tasks. It provides a native LLM loop today and is evolving into a control plane that can also supervise subscription-backed coding harnesses.

## Use Garuda

- [Getting started](guides/getting-started.md) — install, authenticate, and run a first task.
- [Using Garuda](guides/using-garuda.md) — run modes, sessions, workspaces, skills, subagents, and the SDK.
- [Configuration](guides/configuration.md) — profiles, `.agent/`, MCP, hooks, permissions, and environment variables.
- [Web dashboard](guides/web-dashboard.md) — local browser interface, grounding, approvals, and trajectory inspection.
- [External harnesses](guides/external-harnesses.md) — Claude Code and Codex via ACP on your own subscription login.
- [CLI reference](reference/cli.md) — command map and common flags.

## Understand and extend Garuda

- [Architecture](ARCHITECTURE.md) — runtime path, trust boundaries, and change points.
- [Module map](MODULES.md) — package ownership and files to open first.
- [Development](development/development.md) — tests, lint, dependencies, and release checks.
- [Browser checks](development/browser-checks.md) — live dashboard validation.
- [Evaluation](evaluation/index.md) — Harbor, benchmark adapters, and ablations.

## Plan and history

- [Open backlog](BACKLOG.md) — accepted limitations and unfinished work.
- [Durable decisions](../.context/decisions.md) — current cross-harness and
  repository decisions.
- [ACP orchestration roadmap](roadmap/2026-08-acp-orchestration.md) — the dependency-ordered plan for cross-harness sessions and subscriptions.
- [Archive](archive/index.md) — historical RFCs and closed review records; not current behavior.
