# External harnesses

Garuda can supervise subscription-backed coding harnesses through ACP while its
native runtime stays the default. You authenticate in your own CLI; Garuda
never sees, reads, or stores subscription credentials.

## Claude Code

- Launch command: `claude-agent-acp` (registry package
  `@agentclientprotocol/claude-agent-acp`).
- Setup: install Node.js 20+, install and log in Claude Code with your Claude
  Pro/Max login, then `npm install -g @agentclientprotocol/claude-agent-acp`
  and verify `claude-agent-acp --version`.
- Auth stays in your Claude Code login (macOS Keychain or
  `~/.claude/.credentials.json` on Linux). Garuda never reads either. Do not
  set `ANTHROPIC_API_KEY` if you want subscription billing.

## Codex

- Launch command: `codex-acp` (registry package
  `@agentclientprotocol/codex-acp`, successor of `@zed-industries/codex-acp`).
- Setup: install Node.js 20+ and the Codex CLI, authenticate with ChatGPT
  login (`codex login`) or export `CODEX_API_KEY` yourself, then
  `npm install -g @agentclientprotocol/codex-acp` and verify
  `codex-acp --version`.
- Auth stays in your Codex login (`~/.codex/auth.json`). Garuda never reads it.

## Version and capability limits

- Login state shows `unknown` until a run: Garuda cannot check it without
  reading your credential stores, and it will not do that.
- The adapters speak Garuda's owned ACP wire subset: initialize, session/new,
  session/prompt, session/cancel, and approval replies. Vendor extras outside
  that subset (modes, sessions lists, images) are not driven.
- No private HTTP endpoint is used anywhere: both adapters are stdio
  subprocesses of commands you authorized in global configuration.

## Custom ACP servers

Any stdio ACP server works through a global harness manifest with its launch
command; see the [ACP orchestration roadmap](../roadmap/2026-08-acp-orchestration.md).
