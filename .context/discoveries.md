# Verified discoveries

- Garuda already has a mature native loop, session store, permission engine, verifier, MCP client, SDK, server, and local web dashboard.
- The existing `Model` protocol is inference-oriented. It cannot faithfully represent an external coding agent's session, approvals, filesystem ownership, or tool lifecycle.
- The current working tree's review baseline was `1511 passed, 28 skipped` from `pytest -q`, with `ruff check garuda tests` passing. The skipped tests require optional Harbor, tmux, Docker, or live sandbox environments.
- Current documentation reports a different test total, so the status claim must be generated or updated as part of documentation-quality work.
- The local machine has authenticated Claude Code, Codex, Cursor Agent, OpenCode, and Goose CLIs available, which makes it suitable for opt-in adapter integration tests but not for tests that consume subscription quota by default.
