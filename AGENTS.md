# Garuda contributor guide

Garuda is a provider-agnostic coding-agent harness. Its product boundary is the runtime around a model or coding harness: workspace control, permissions, context, evidence, sessions, and observability.

## Start here

1. Read [README.md](README.md), then [docs/index.md](docs/index.md).
2. Read `.context/conventions.md` and durable context relevant to the change. Runtime handoff files are created only during a Garuda session.
3. For runtime changes, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/MODULES.md](docs/MODULES.md).
4. Treat [docs/BACKLOG.md](docs/BACKLOG.md) as open work only. Remove an item when fixed; do not mark it done in place.

## Working agreements

- Preserve user changes in a dirty worktree. Stage only files you intentionally changed.
- Keep a module focused on one responsibility. New cross-entry-point wiring belongs in `garuda/agents/setup.py` or a dedicated shared service, not copied into every interface.
- Use protocols at product boundaries. `Model` is for inference transports; external coding agents belong behind the planned `AgentRuntime` boundary, not behind `Model`.
- Fail closed for security, permission, workspace, network, and verification ambiguity. Never describe a guardrail as a sandbox boundary.
- Do not read, copy, persist, or proxy vendor OAuth tokens. Integrations launch user-authenticated official CLIs or documented public APIs.
- Keep reasoning, secrets, raw transcripts, and large tool outputs out of `.context/`. Handoffs contain decisions, evidence, next actions, and paths.

## Tests and quality checks

```bash
pytest tests/test_<area>.py -q
pytest -q
ruff check garuda tests
```

Run the narrowest relevant tests first. Browser-dashboard checks require a live Chrome installation and are documented in [docs/development/browser-checks.md](docs/development/browser-checks.md). Live Docker, tmux, Harbor, and OS-sandbox checks are optional environment integrations; state clearly when they were not run.

## Documentation contract

- Keep root `README.md` short: value proposition, safe quick start, and navigation.
- Put product and contributor detail under `docs/`; do not add nested README files.
- Use lower-case, hyphenated names for new documentation pages.
- Update relevant architecture, reference, and roadmap pages with behavior changes. Record a durable decision in `.context/decisions.md` when it affects a public boundary or security posture.

## Context pack

Committed, durable context: `.context/architecture.md`, `.context/decisions.md`, `.context/discoveries.md`, and `.context/conventions.md`.

Session-managed, gitignored context: `.context/current-task.md` and `.context/handoff.md`.

Only Garuda's `ContextPackManager` writes generated files. Agents may read them and propose changes, but durable context is reviewed repository content and runtime context is not a collaboration scratchpad.
