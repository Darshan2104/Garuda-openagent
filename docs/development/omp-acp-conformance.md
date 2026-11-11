# oh-my-pi (omp) ACP conformance report

!!! warning "No-prompt report"
    This is a **no-prompt** conformance report for one omp version, recorded on
    2026-11-06 for [#380](https://github.com/Darshan2104/Garuda-openagent/issues/380).
    No `session/prompt` was sent, so no model turn ran and no provider quota was
    used. It shows what `omp acp` does at launch, handshake, session creation,
    idle cancellation and shutdown. It does **not** show how omp behaves during
    a task. See [What was not tested](#what-was-not-tested).

The shipped `omp` runtime (`garuda/acp/builtin/omp.json`) is based on this
report. It's a separate runtime from `pi`: the `pi` manifest still launches the
`pi-acp` registry adapter and hasn't changed. Garuda runs omp only as an
external `AgentRuntime` over ACP and never as an inference `Model`.

## What was run

| Item | Value |
|---|---|
| Executable | `/opt/homebrew/bin/omp`, a link to `/opt/homebrew/Cellar/omp/18.16.0/bin/omp` (Homebrew, Mach-O arm64) |
| Version | `omp --version` prints `omp/18.16.0`; `initialize` reports `agentInfo` `{name: "omp", title: "omp", version: "18.16.0"}` |
| ACP entrypoint | `omp acp`: "Run omp as an ACP (Agent Client Protocol) server over stdio". It takes no flags, and the stream is newline-delimited JSON-RPC on stdout. |
| Environment | `PATH`, `HOME` and `LANG` only (Garuda passes the same set, plus `USER`). `HOME` was an empty temporary directory, so no omp login, config or session store was visible. |
| Isolation | omp ran under `sandbox-exec` with all network access denied, and with file access to `~/.omp`, `~/.pi`, `~/.config` and `~/Library/Keychains` denied, as were the Security-framework mach services. Nothing could reach a model provider or a stored credential. |
| Clients | A raw JSON-RPC script, and Garuda's own `AcpRuntime` (`start`, then idle `cancel`, then `close`) with `session/prompt`, `authenticate`, `session/set_mode` and `session/set_config_option` blocked by a guard |

The redacted capture is committed at
`tests/fixtures/omp/omp-18.16.0-no-prompt.json`.

## Handshake

`initialize` was sent with Garuda's parameters (`protocolVersion: 1`, empty
`clientCapabilities`). omp answered in about 0.09 seconds:

| Field | Value |
|---|---|
| `protocolVersion` | `1`, which matches Garuda's ACP v1 client |
| `agentCapabilities.loadSession` | `true` (declared only; not exercised) |
| `agentCapabilities.sessionCapabilities` | `list`, `fork`, `resume`, `close` |
| `agentCapabilities.promptCapabilities` | `image: true`, `embeddedContext: true` |
| `agentCapabilities.mcpCapabilities` | `http: true`, `sse: true` |
| Garuda mediation fields (`families`, `mediated`, `sandbox`) | absent, as for every standard v1 agent |
| `authMethods` | one method: `agent`, "Use existing local credentials" ("Authenticate via the provider keys/OAuth state already configured under ~/.omp."). Upstream source adds a `terminal` method only when the client advertises terminal auth, which Garuda doesn't. |
| `_meta` | `steering: {supported: true}` (an omp extension that Garuda ignores) |

omp didn't require `authenticate` before `session/new`. Garuda never calls it.

## Session options

`session/new` was sent with an absolute empty workspace and `mcpServers: []`.
It returned a session id in about 0.3 seconds, even though no credentials were
available.

| Option (`configOptions`) | Category | Current | Values |
|---|---|---|---|
| `mode` | `mode` | `default` | `default` ("Standard ACP headless mode"), `plan` ("Read-only planning mode that drafts a plan to a markdown file before any code changes") |
| `thinking` | `thought_level` | `off` | `off`, `auto` |

`modes` reported the same two ids, with `default` current. No `model` option
appeared. Upstream source adds one only when omp can list models, which it
couldn't with no credentials. After `session/new`, omp sent two `session/update`
notifications: `available_commands_update` (51 slash commands) and
`session_info_update`.

!!! note "`session/new` reaches the network"
    Inside `session/new`, omp runs its own model discovery. Its log recorded
    attempts to reach local model servers (Ollama on `127.0.0.1:11434`, LM Studio
    on `127.0.0.1:1234`, llama.cpp on `127.0.0.1:8080`), all blocked by the
    sandbox, and a rewrite of its model cache. Under a normal Garuda launch there
    is no sandbox and omp uses your real `HOME`. Model discovery then runs with
    your omp configuration and may contact provider model-list endpoints. This
    report didn't measure which endpoints those are. They are listing calls,
    not inference, but Garuda doesn't control them.

omp also writes its own state under `HOME/.omp`: an agent database, a model
cache, logs, a session lock, and one session file per `session/new` (it
reported `omp --resume <id>` as a recovery hint on SIGTERM). In a real launch
that is your `~/.omp`. Garuda doesn't read or clean it.

## Cancellation and cleanup

| Check | Result |
|---|---|
| `session/cancel` on an idle session | No reply (it is a notification). The process stayed alive, and `session/list` still answered and showed the session. |
| Process tree while idle | One process, the group leader. No children or descendants after `initialize` or `session/new`. |
| Close stdin after `initialize` | Exited 0 within 0.01 s; the process group was empty |
| Close stdin after `session/new` | Exited 0 within 0.05 s; the process group was empty |
| SIGTERM to the group after `session/new` | Exited 143 within 0.05 s; the process group was empty; nothing needed SIGKILL |
| Garuda `AcpRuntime` | `start` reached `idle` in about 1.3 s (including sandbox start-up). Idle `cancel` moved it to `closed`. `close` took 0.12 s, and no group members were left. |

These results apply to an idle omp. During a turn, omp may start tools, LSP
servers, PTYs, browsers or subagents. Whether all of them stay in Garuda's
owned process group is untested. Garuda's group cleanup is a guardrail, not
confinement.

## How it fits Garuda's runtime contract

The proved subset matches what Garuda's generic ACP path requires: ACP v1, a
`session/new` that accepts an absolute `cwd` and an `mcpServers` list, a
cancellation notification, and clean exit on stdin close or SIGTERM. Garuda
therefore ships `omp` as a built-in manifest with:

- launch command `omp acp`, bound at launch to the absolute path discovery
  resolved;
- version probe `omp --version` with pattern `^omp/(\d+\.\d+\.\d+...)`, so
  `garuda runtime inspect omp` shows the installed version. The manifest
  states that only 18.16.0 was checked. Like the other built-in vendors, there
  is no launch-time version gate.
- capabilities `prompt` and `cancel` (the generic ACP contract) and the
  standard mediation warning, plus a warning that names the untested subset;
- login flow `user-cli` (`omp login`) and no auth probe. Garuda never runs
  `omp token`, `omp auth` or `omp auth-broker`, and never reads `~/.omp`.

omp is absent from Garuda's proof-gated tables. Role model, effort and harness
mode options are refused (`PROVEN_OPTIONS`, `MODE_AUTHORITY`), and resume uses
a brief instead of `session/load` (`PROVEN_LOAD`).

## What was not tested

Each item below needs either a prompt (provider quota) or a request this report
did not send. None of them is claimed:

- **Prompted turns**: streaming, tool calls, diffs, stop reasons, usage
  reporting and error normalization.
- **Permission requests**: whether and how omp sends
  `session/request_permission` in ACP mode, and how its own
  `tools.approvalMode` default applies. The `--approval-mode` and
  `--auto-approve` flags belong to the interactive CLI. `omp acp` accepts no
  flags.
- **Mode and option selection**: `session/set_mode` and
  `session/set_config_option` (including whether `plan` is enforced as
  read-only).
- **No-edits or confinement claims**: none. omp runs its own `read`, `bash`,
  `edit`, `write`, browser and subagent tools with its own authority.
- **Cancelling a running turn**, and process cleanup while tools or subagents
  are active.
- **Session lifecycle methods**: `session/load`, `session/resume`,
  `session/fork`, `session/close` and `authenticate`.
- **Real-HOME behavior**: model discovery with real credentials, and which
  network endpoints `session/new` contacts.
- **Any other omp version, platform (only macOS arm64 was checked) or
  install method.**

## Re-run the check

The opt-in live handshake starts the installed omp with your real `HOME`, so
`session/new` runs omp's model discovery as described above. It sends no
prompt:

```bash
GARUDA_LIVE_HARNESS=omp pytest tests/test_acp_vendors.py -k live_harness -q
```

CI never needs omp. `tests/test_acp_omp.py` replays the committed capture
through the production discover-and-launch path.
