# External harnesses

Garuda ships ACP adapter manifests for subscription-backed coding harnesses
while its native runtime stays the default. You authenticate in your own CLI;
Garuda never sees, reads, or stores subscription credentials.

**Status:** the shipped manifests are part of the trusted runtime catalog, so
`--runtime claude` and `--runtime codex` resolve, show up in discovery, and
honor `disabled_runtimes`. No `garuda run`, SDK, or dashboard path launches an
ACP harness yet: selecting one refuses with "not launchable by this runtime
facade yet" instead of running the native loop. The adapters are exercised
only against Garuda's strict ACP v1 test fixture; neither vendor CLI has been
verified end to end by this repository's tests.

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
  login (`codex login`), then `npm install -g @agentclientprotocol/codex-acp`
  and verify `codex-acp --version`.
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

## Version and capability limits

- Login state shows `unknown`: the shipped manifests declare no login probe,
  and Garuda will not read your credential stores to find out.
- The adapter process gets only `PATH`, `HOME`, and `LANG` from Garuda's
  environment. API-key variables (`ANTHROPIC_API_KEY`, `CODEX_API_KEY`) and
  custom config directories (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) do not reach
  it, and manifests cannot add environment variables. Use each vendor CLI's
  own login in its default location under your home directory.
- The adapters speak Garuda's owned ACP wire subset: initialize, session/new,
  session/prompt, session/cancel, and approval replies. Vendor extras outside
  that subset (modes, sessions lists, images) are not driven.
- No private HTTP endpoint is used anywhere: both adapters are stdio
  subprocesses of commands from the shipped manifests or your global
  configuration.
- Discovery resolves the executable once and the launch factory uses that
  exact absolute path. Changing `PATH` after discovery cannot substitute a
  different adapter binary.

## Custom ACP servers

Any stdio ACP server works through a global harness manifest with its launch
command, in the `runtimes:` list of your global settings file
(`~/.agent/settings.yaml`, or the path in `GARUDA_GLOBAL_SETTINGS`). An entry with a shipped id such as `claude` replaces the shipped
manifest:

```yaml
runtimes:
  - runtime_id: my-agent
    kind: acp
    command: [my-agent-acp]
    version: "1"
    capabilities: [prompt, cancel]
    version_args: [my-agent-acp, --version]
    setup: Install my-agent-acp and log in with its own CLI.
disabled_runtimes: [codex]
```

Project `.agent/settings.yaml` may only add `runtime_refs` aliases to these
ids; it cannot declare commands. See the
[ACP orchestration roadmap](../roadmap/2026-08-acp-orchestration.md).
