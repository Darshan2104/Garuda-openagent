# Garuda architecture context

Garuda is a coding-agent control plane. Today it owns a native agent loop over a provider-agnostic `Model` protocol; the planned architecture adds an `AgentRuntime` layer above that loop so Garuda can also supervise external coding harnesses through ACP.

The invariant is explicit ownership: a native Garuda runtime owns its model loop and tools, while an external runtime owns its own loop and native session. Garuda owns cross-runtime sessions, context packs, routing, repository diffs, verification, and normalized events.

Read [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) for the current runtime and [docs/roadmap/2026-08-acp-orchestration.md](../docs/roadmap/2026-08-acp-orchestration.md) for the target state.
