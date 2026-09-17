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

## Cursor Agent

- Launch command: `agent acp` (native subcommand of the Cursor Agent CLI).
- Setup: install the Cursor Agent CLI (whose documented default binary is
  `~/.local/bin/agent`) and authenticate with your Cursor account, then verify
  `agent --version`.
- Auth stays in your Cursor login. Garuda never reads your Cursor credentials.

## OpenCode

- Launch command: `opencode acp` (native subcommand; registry package
  `opencode-ai`).
- Setup: install the OpenCode CLI and authenticate it, then verify
  `opencode --version`.
- Auth stays in your OpenCode login. Garuda never reads your OpenCode auth
  configuration.

## Pi

- Launch command: `pi-acp` (registry package `pi-acp`).
- Setup: `npm install -g pi-acp`, authenticate Pi itself, then verify
  `pi-acp --version`.
- Auth stays in your Pi login. Garuda never reads your Pi credentials.

## Goose

- Launch command: `goose acp` (native Goose CLI subcommand).
- Setup: install Goose, log in, then verify `goose --version`.
- Auth stays in your Goose login. Garuda never reads your Goose credentials.

## Version and capability limits

- Login state shows `unknown` until a run: Garuda cannot check it without
  reading your credential stores, and it will not do that.
- The adapters speak Garuda's owned ACP wire subset: initialize, session/new,
  session/prompt, session/cancel, and approval replies. Vendor extras outside
  that subset (modes, sessions lists, images) are not driven.
- No private HTTP endpoint is used anywhere: both adapters are stdio
  subprocesses of commands you authorized in global configuration.
- Discovery resolves the executable once and the launch factory uses that
  exact absolute path. Changing `PATH` after discovery cannot substitute a
  different adapter binary.

## Custom ACP servers

Any stdio ACP server works through a global harness manifest with its launch
command — no code changes are needed for a standard capability set. Generic
adapters have no vendor-specific guarantees: modes, model lists, and extras
beyond the wire subset are not driven. See the
[ACP orchestration roadmap](../roadmap/2026-08-acp-orchestration.md).

## Optional live compatibility checks

Set `GARUDA_LIVE_HARNESS` to one installed harness id to run its handshake-only
smoke check locally. The corresponding vendor CLI must already be installed and
logged in with the user's own account; Garuda never accepts or reads a secret
for this check. Without that opt-in environment variable the check is skipped.
With it, a missing, unauthenticated, or incompatible CLI fails the check and is
reported as an environment or compatibility failure—not a green CI result.
