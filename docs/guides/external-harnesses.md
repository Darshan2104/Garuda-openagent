# External harnesses

Garuda ships ACP adapter manifests for subscription-backed coding harnesses
while its native runtime stays the default. You authenticate in your own CLI;
Garuda never sees, reads, or stores subscription credentials. Subscription use
is governed by each vendor's policy, not by Garuda: quota appears only when the
harness itself reports it, and Garuda never estimates, infers, or zero-fills
usage.

**Status:** the shipped manifests (`claude`, `codex`, `cursor`, `opencode`,
`pi`, `goose`) are part of the trusted runtime catalog, so they resolve, show
up in `garuda runtime list`, and honor `disabled_runtimes`. `garuda run
--runtime <id>` launches an installed ACP harness under the native run's
invariants (workspace lease, persisted session and child record, baseline and
delta, broker approvals), and `garuda runtime handoff --confirm` hands a native
session to one — see [ACP runs and handoffs](../reference/cli.md#acp-runs-and-handoffs).
A handed-off session stays with the harness: Garuda does not resume it
natively. The SDK and the dashboard still run only the native loop and refuse
an ACP selection instead of silently running native. Garuda does not verify an
ACP result, and ACP authority is recorded, not enforced (the harness runs its
own edits and commands). The adapters are exercised only against Garuda's
strict ACP v1 test fixture through an installed shim; no vendor CLI has been
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
- Limit: Garuda answers only `session/request_permission` from the agent.
  Cursor's blocking extension requests such as `cursor/ask_question` and
  `cursor/create_plan` get a JSON-RPC "method not found" error (fail closed),
  so turns that depend on them end with the agent's error handling rather
  than a question or plan in Garuda.

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
- Discovery resolves the executable once and the launch factory
  (`adapter_for_discovered`) uses that exact absolute path; without a
  discovered path the factory refuses instead of looking `PATH` up again.
  Changing `PATH` after discovery cannot substitute a different adapter
  binary, but replacing the file at that path can: this is a binding, not a
  sandbox.

## Custom ACP servers

Any stdio ACP server works through a global harness manifest with its launch
command — no code changes are needed for a standard capability set — in the
`runtimes:` list of your global settings file (`~/.agent/settings.yaml`, or the
path in `GARUDA_GLOBAL_SETTINGS`). An entry with a shipped id such as `claude`
replaces the shipped manifest:

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
ids; it cannot declare commands. Generic adapters have no vendor-specific
guarantees: modes, model lists, and extras beyond the wire subset are not
driven. See the [ACP orchestration roadmap](../roadmap/2026-08-acp-orchestration.md).

## Optional live compatibility checks

Set `GARUDA_LIVE_HARNESS` to one installed harness id to run its handshake-only
smoke check locally. The corresponding vendor CLI must already be installed and
logged in with the user's own account; Garuda never accepts or reads a secret
for this check. Without that opt-in environment variable the check is skipped.
With it, a missing, unauthenticated, or incompatible CLI fails the check and is
reported as an environment or compatibility failure—not a green CI result.
