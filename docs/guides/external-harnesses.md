# External harnesses

Garuda can launch subscription-backed coding harnesses through the Agent Client
Protocol (ACP) while keeping its native model-and-tool loop as the default. The
shipped trusted catalog includes `claude`, `codex`, `cursor`, `opencode`, `pi`,
and `goose`.

!!! abstract "At a glance"
    - You install and log in to each vendor CLI yourself. Garuda launches it but
      never reads, copies, stores, or proxies your vendor OAuth credentials.
    - The harness edits and runs commands **with its own authority**. Garuda's
      permission rules, Docker and sandbox workspaces, and completion checks do
      not confine it.
    - Garuda records the session, approvals, lease, baseline, and workspace
      changes. "Completed" means the harness ended its turn, not that Garuda
      verified the result.
    - Quick recipes: [Level 5 · Advanced](../use-cases/advanced.md).

## Native and ACP behavior differ

| Behavior | Native runtime | ACP runtime |
|---|---|---|
| Controller | Garuda model loop | External harness |
| Tool permissions | Enforced by Garuda's permission engine | Harness requests can flow through the approval broker, but native tool rules don't confine the harness |
| Completion | Garuda's completion checks for the selected mode | The harness ended its turn; Garuda does not verify the result |
| Session evidence | Messages, events, baseline, delta, metrics | Normalized events, runtime segment, child record, baseline, and delta where available |
| Credentials | Provider API key for the selected model | Your vendor CLI login |

ACP execution doesn't place the harness inside a Garuda Docker or OS-sandbox
workspace. It runs as its own subprocess. When running untrusted code, use the
vendor's controls and an independently isolated workspace. See
[Safety and workspaces](safety-and-workspaces.md).

## Discover installed runtimes

```bash
garuda runtime list
garuda runtime inspect opencode
```

- Add `--json` for machine-readable health records.
- Discovery reports whether the executable resolves, its declared capabilities
  and version, setup guidance, and any login or quota information the vendor
  reports.
- Missing login or quota data stays `unknown`. Garuda doesn't infer it from
  credential files or assume unknown cost is zero.
- A catalog entry can be unavailable because its executable is missing, it is
  disabled in trusted global configuration, or its version probe fails.
  `garuda runtime inspect <id>` is the first troubleshooting step.

## Run a task directly on ACP

```bash
garuda run --workspace . --runtime opencode -t "Inspect the failing tests"
```

```mermaid
sequenceDiagram
  participant You
  participant Garuda
  participant H as Harness process
  You->>Garuda: garuda run --runtime opencode
  Garuda->>Garuda: resolve trusted manifest, take workspace lease, record baseline
  Garuda->>H: launch the exact executable found by discovery
  H->>Garuda: approval request
  Garuda->>You: y/N on a terminal (denied and recorded when headless)
  H-->>Garuda: updates, then turn ends
  Garuda->>Garuda: record normalized events and the ending delta
  Garuda->>H: retire the process
```

Unknown, disabled, or missing runtimes are refused. If an installed harness
fails to start and the workspace is unchanged, Garuda moves the session once
to `fallback_runtime` (native by default) and records the reason in the
session.

## Route the initial runtime

Initial selection uses the first applicable source:

1. an explicit `--runtime` (including `--runtime native`, which pins the
   native runtime);
2. a trusted global deterministic routing rule;
3. a project rule, only when global settings authorize project routing;
4. the optional trusted classifier;
5. the configured default runtime; and
6. built-in `native`.

Global `~/.agent/settings.yaml` owns runtime manifests, rules, defaults, and the
classifier. A project may define aliases to trusted runtime IDs or opt out of
the classifier, but can't provide an executable command or broaden global
authority. The classifier receives bounded task metadata and approved
candidates, not file contents or tools, and its choice is revalidated. Details:
[Initial runtime selection](configuration.md#initial-runtime-selection).

Selection only runs the version and login probes of runtimes it could choose:
the one you named, or the targets of your rules, classifier candidates, and
default and fallback runtimes. A plain native run with none of these starts no
vendor CLI. Results are reused for 60 seconds from an owner-only cache beside
your global settings that keeps conclusions, never probe output.
`garuda runtime list` always probes every runtime.

## Hand off a native session

Preview first. Without `--confirm`, nothing changes:

```bash
garuda runtime handoff --session latest --to opencode
garuda runtime handoff --session latest --to opencode --confirm
```

Add `--workspace` when an older native session recorded a relative workspace
and you aren't running from that directory.

The confirmed transaction is one-shot:

1. pause the native source;
2. checkpoint its state and capture the workspace delta;
3. start the exact target executable found during the pre-check;
4. persist the target segment and acknowledgement, then record its child;
5. close the native source;
6. send the bounded handoff package as the target's first prompt; and
7. close and retire the target after its turn.

!!! warning "Acknowledgement is the point of no return"
    A failure **before** acknowledgement rolls back to the native source. A
    failure **after** it is a target failure: Garuda closes the target and
    records the failure, but doesn't undo changes the target may have made. The
    session belongs to the target after acknowledgement in either case.

Handoff packages contain bounded task state, evidence references, workspace
delta context, and redacted metadata. They never contain vendor credentials,
raw secrets, or an unbounded copy of the transcript.

## Resume, recover, reclaim, and support

Every runtime command that takes `--session` accepts a full ID, the unique
prefix printed by `garuda sessions`, or `latest`. Ambiguous and missing
references fail before preview, lease acquisition, or mutation.

```mermaid
stateDiagram-v2
  [*] --> native
  native --> external: handoff --confirm
  external --> native: reclaim, once the target is proven stopped
  native --> native: resume
```

```bash
garuda runtime recover --session latest --json
garuda runtime reclaim --session latest
garuda runtime resume --session latest -t "Continue after the external run"
garuda runtime support --session latest
```

| Command | Does |
|---|---|
| `recover` | Classifies interrupted state as `resumable`, `rolled_back`, or `external`. An `external` session refuses native resume and another handoff while the target may still be acting |
| `reclaim` | Returns ownership to native. Refuses while a workspace lease, a live recorded child, or an active target state says the harness may still be running. It repairs ownership; it does not roll back external changes |
| `resume` | Continues a native session through the normal run lifecycle |
| `support` | Prints a redacted bundle (runtime lanes, kind tallies, timing metrics, scrubbed metadata) without raw event payloads |

## Dashboard, SDK, and service surfaces

- **Dashboard:** the Runtimes board lists and inspects catalog entries, previews
  and prepares handoffs, shows workspace deltas, and runs recovery. Dashboard
  chat still starts the native loop.
- **SDK:** `SoftwareAgent(runtime="opencode")` runs one ACP turn and returns an
  `AgentResult`. `Conversation` can start on ACP and switch ACP-to-ACP through
  the transactional handoff path, but refuses an in-process native-to-ACP
  switch; use the CLI handoff for that.
- **Service:** the JSON-RPC server exposes runtime list, inspect, handoff,
  recover, and support methods through the same trusted catalog. Request
  payloads can't provide launch manifests.

## Consult transport (planned)

Consults between roles are planned, not shipped. For an external harness to
*start* a consult, Garuda would give it one MCP tool through
`session/new.mcpServers`. That is enabled only when all three of these are
proven for the exact adapter version:

| Requirement | Claude adapter 0.85.0 | Codex adapter 2.1.1 |
|---|---|---|
| Forwarding: the adapter starts the server and lists its tools | Supported (the probe server received `initialize` and `tools/list`) | Supported |
| Permission provenance: a tool-permission request names the server and tool in structured fields | Unknown: proving it needs a prompted call | Unknown |
| Quiescence: a documented way to pause the caller's workspace operations while a snapshot is taken | Unknown: not documented | Unknown: not documented |

Because two requirements are unknown, external harnesses can't start
consults yet. They can still be consulted, and Garuda's native roles can
consult any role. The opt-in check sends no prompt:

```bash
python scripts/capture_consult_transport.py --name claude --out tests/fixtures/consult -- npx -y -p @agentclientprotocol/claude-agent-acp claude-agent-acp
```

## Vendor setup

=== "Claude Code"

    - Launch command: `claude-agent-acp` from
      `@agentclientprotocol/claude-agent-acp`.
    - Install Node.js 20+, install and log in to Claude Code, then install the
      ACP package globally and verify `claude-agent-acp --version`.
    - Authentication stays in the Claude Code login. Don't set
      `ANTHROPIC_API_KEY` when you intend to use subscription billing.

=== "Codex"

    - Launch command: `codex-acp` from `@agentclientprotocol/codex-acp`.
    - Install Node.js 20+ and the Codex CLI, run `codex login`, install the ACP
      package, and verify `codex-acp --version`.
    - Authentication stays in the Codex CLI login.

=== "Cursor Agent"

    - Launch command: `agent acp`, a Cursor Agent CLI subcommand.
    - Install and authenticate Cursor Agent, then verify `agent --version`.
    - Garuda answers `session/request_permission`. Unsupported blocking
      extensions such as `cursor/ask_question` and `cursor/create_plan` fail
      closed with a method-not-found response.

=== "OpenCode"

    - Launch command: `opencode acp` from the `opencode-ai` package.
    - Install and authenticate OpenCode, then verify `opencode --version`.
    - Authentication stays in OpenCode's own configuration.

=== "Pi"

    - Launch command: `pi-acp` from the `pi-acp` package.
    - Install the package, authenticate Pi, then verify `pi-acp --version`.

=== "Goose"

    - Launch command: `goose acp`, a Goose CLI subcommand.
    - Install and authenticate Goose, then verify `goose --version`.

## Usage and limit sources

Garuda can only show a usage or limit value that a vendor documents and that
was read without a prompt or a credential file. This table records what the
installed CLIs offered when checked. **Supported** means read successfully;
**declared** means documented or present but not exercised; **unknown** means
there is no documented source, or proving one needs a prompt.

| Source | Claude Code 2.1.285 | Codex CLI 0.159.3 |
|---|---|---|
| Login status | Supported: `claude auth status --json` (`loggedIn`, `authMethod`, `subscriptionType`) | Supported: `codex login status` (text) |
| Opaque account id for binding a limit | Supported: `orgId` from `claude auth status --json` (organization-level) | Supported: `accountId` from `codex app-server` `account/rateLimits/read` |
| Plan usage windows (used fraction, window, reset) | Unknown: no documented non-interactive interface (`/usage` is interactive) | Supported: `codex app-server` `account/rateLimits/read`, a 300-minute and a 10,080-minute window, each with `usedPercent` and `resetsAt` |
| Limit-reached signal | Unknown | Declared: `rateLimitReachedType` field (not observed at a limit) |
| Limit data from the ACP adapter | Unknown: none advertised | Unknown: none advertised |
| Per-turn or cumulative token usage | Unknown: needs a prompt | Unknown: needs a prompt |

For native API providers, LiteLLM 1.87 forwards provider rate-limit headers
(`x-ratelimit-*`, and every header as `llm_provider-*`) in a response's
`_hidden_params["additional_headers"]`. That is declared, not exercised, since
proving it needs a paid call.

Codex's `getAuthStatus` app-server method can return an auth token when asked.
Garuda never calls it. A limit or usage value without a supported source stays
`unknown`, and an `unknown` or account-unbound limit never triggers a
fallback.

**Limit observations and quota fallback.** A refresh re-runs the documented status
reads (for Codex: the app-server's read-only `account/read` and
`account/rateLimits/read`; never a prompt, a credential file or `getAuthStatus`) and
records the reading with its source, the exact version, the time and a **local digest** of
the official account id (a salted hash kept only in your Garuda home; names are never
stored). Reaching a limit appends one `limit_event` to the usage ledger, however often it is
observed. Such an event may make a harness count as exhausted for **fallback** only when
all of these hold at selection time: the source is proved for this exact version; the
reading names an account and the harness is logged into the same one now; it is at most 60
seconds old; and it reports the limit reached with an explicit reset time still ahead.
Anything else, including a changed account, version or login, a stale reading, an unknown
or past reset, or a source with no account id, is shown as history and leaves the harness
eligible. An unknown reset time never bans a provider. (Enabling the `harness.limit_reached`
fallback reason is a separate step; until then limit observations are display evidence.)

To record another version (no prompt is sent):

```bash
python scripts/capture_usage_sources.py --out tests/fixtures/usage
```

## Environment and protocol limits

- Shipped manifests declare no credential-file login probe, so login often
  shows as `unknown`.
- Adapter subprocesses get a minimal environment: `PATH`, `HOME`, and `LANG`.
  Provider API-key variables and custom vendor config-directory variables are
  not forwarded.
- Garuda drives its own ACP subset: initialize, session creation, prompt,
  cancel, normalized updates, and permission requests. Vendor-specific model
  lists, modes, images, and blocking extensions outside that subset aren't
  supported automatically.
- Discovery binds launch to the resolved absolute executable. Replacing the
  file at that path can still replace what runs; executable binding is not a
  sandbox.
- Repository tests use strict fake ACP servers. Real vendor compatibility checks
  are opt-in because they need an installed, authenticated CLI and may consume
  subscription quota.

## Exercised adapter capabilities

Advertising a method is not proof that it works. This table records what the
adapters did when exercised, captured without sending a prompt (so no model
turn and no subscription quota was used). **Supported** means it was exercised
and worked; **declared** means it is advertised but not proven here;
**unknown** means it is not advertised or needs a prompt to prove.

| Capability | Claude adapter `@agentclientprotocol/claude-agent-acp` 0.85.0 | Codex adapter `@agentclientprotocol/codex-acp` 2.1.1 | When not supported |
|---|---|---|---|
| Model selection | Supported: `session/set_config_option` id `model`; values are aliases such as `opus`, `sonnet`, `haiku`, `default` | Supported: id `model`; values are exact model ids | Refuse before the prompt |
| Effort selection | Supported: id `effort` (category `thought_level`): `default`, `low` … `max` | Supported: id `reasoning_effort`: `low` … `ultra` | Refuse before the prompt |
| `session/close` | Supported | Supported | Close the process |
| `session/load` | Declared; an unprompted session isn't persisted, so load is unproven | Declared; load of an unprompted session returned an internal error | Start a linked session from a brief |
| `session/resume` | Declared | Declared | Start a linked session from a brief |
| Stdio MCP servers in `session/new` | Supported (an `mcpServers` list is accepted) | Supported | No MCP tools for that session |
| HTTP MCP servers | Declared | Declared | Not used |
| `usage_update` accounting | Unknown: needs a prompt | Unknown: needs a prompt | Usage shows as `unknown` |

Both adapters also advertise permission modes that bypass approval (Claude
`bypassPermissions`, Codex `agent-full-access`). Garuda never selects these
on its own.

The captures are kept as redacted fixtures under `tests/fixtures/acp/`. To
record another version, run the opt-in capture. It sends no prompt:

```bash
python scripts/capture_acp_capabilities.py --name claude --out tests/fixtures/acp/claude -- npx -y -p @agentclientprotocol/claude-agent-acp claude-agent-acp
python scripts/capture_acp_capabilities.py --name codex --out tests/fixtures/acp/codex -- npx -y -p @agentclientprotocol/codex-acp codex-acp
```

Proving `session/load`, `session/resume` and usage reporting needs one small,
separately approved prompted session per adapter version.

`garuda run --resume S` on a session of an ACP runtime uses the agent's own
`session/load` only for an adapter version on which it was exercised (none
of the two above yet). Otherwise — or when the agent stops declaring
`loadSession` at the handshake — it starts a new, linked session on the same
runtime with `S`'s brief attached (`resume_mode: brief`). `--as RUNTIME`
continues on another runtime the same way.

## Custom ACP servers

Register any compatible stdio ACP server in trusted global settings. An entry
with a shipped ID replaces that shipped manifest:

```yaml
# ~/.agent/settings.yaml
runtimes:
  - runtime_id: my-agent
    kind: acp
    command: [my-agent-acp]
    version: "1"
    capabilities: [prompt, cancel]
    version_args: [my-agent-acp, --version]
    setup: Install my-agent-acp and authenticate with its own CLI.
disabled_runtimes: [codex]
```

Project `.agent/settings.yaml` may add `runtime_refs` aliases to these IDs but
can't declare commands. Generic adapters get no vendor-specific guarantees
beyond Garuda's supported ACP subset.

Garuda can also serve its native runtime as the agent side of a stdio ACP
editor session:

```bash
python -m garuda.acp.server --workspace /path/to/workspace
```

`--driver echo` is reserved for protocol tests.

## Optional live compatibility check

Run one handshake-only smoke check for an installed harness:

```bash
GARUDA_LIVE_HARNESS=opencode pytest tests/test_live_harness.py -q
```

The vendor CLI must already be installed and logged in. An unavailable CLI is
an explicit skip; an installed but unauthenticated or incompatible CLI fails the
opted-in check. CI doesn't set this variable or consume a subscription.
