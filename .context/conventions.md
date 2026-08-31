# Repository conventions

- Python 3.12+; use `ruff check garuda tests` and focused `pytest` coverage.
- `.agent/` is Garuda's project-home convention. `AGENTS.md` is repository guidance; `agent.md` is an OpenCode-compatible agent-profile format.
- The session event log is append-only and persistence is atomic. Preserve that property for new session and handoff records.
- Permission checks are guardrails. Docker is the confinement boundary; macOS Seatbelt is a blast-radius reducer with unconfined host reads.
- New docs belong under `docs/`; nested README files are intentionally avoided.
- Design external interfaces for capability discovery, versioning, cancellation, resume, and explicit failure states.
