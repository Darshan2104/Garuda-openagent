# External harnesses

Garuda can launch subscription-backed coding harnesses through the Agent Client
Protocol (ACP) while keeping its native model-and-tool loop as the default. The
shipped trusted catalog includes `claude`, `codex`, `cursor`, `opencode`, `pi`,
and `goose`.

You install and authenticate each vendor's official CLI or ACP adapter yourself.
Garuda launches that command but never reads, copies, persists, or proxies the
vendor's OAuth credentials. Subscription use and quota remain governed by the
vendor.

## Native and ACP behavior differ

| Behavior | Native runtime | ACP runtime |
|---|---|---|
| Controller | Garuda model loop | External harness |
| Tool permissions | Enforced by Garuda's native permission engine | Harness requests can flow through the approval broker; harness authority is not confined by native tool rules |
| Completion | Garuda completion gates apply according to mode | A completed turn means the harness ended; Garuda does not verify the result |
| Session evidence | Messages, events, baseline, delta, metrics | Normalized events, runtime segment, child record, baseline, and delta where available |
| Credentials | Provider API key for the selected model | User-owned vendor CLI login |

ACP execution is not a way to place the vendor harness inside a Garuda Docker or
OS-sandbox workspace. The harness is launched as its own subprocess and performs
edits and commands with its own authority. Use the vendor's controls and an
independently isolated workspace when running untrusted code. See
[Safety and workspaces](safety-and-workspaces.md).

## Discover installed runtimes

List the trusted catalog and discovery health:

```bash
garuda runtime list
garuda runtime inspect opencode
```

Add `--json` for machine-readable health records. Discovery reports whether the
configured executable resolves, plus declared capabilities, version, setup
guidance, and vendor-reported login or quota information when available. Missing
login or quota data remains `unknown`; Garuda does not infer it from credential
files or assume that unknown cost is zero.

A runtime can be present in the catalog but unavailable because its executable
is missing, it is disabled in trusted global configuration, or its version probe
fails. `garuda runtime inspect <id>` is the first troubleshooting step.

## Run a task directly on ACP

Select the runtime explicitly:

```bash
garuda run --workspace . --runtime opencode -t "Inspect the failing tests"
```

Garuda resolves the trusted manifest before starting, acquires the workspace
lease, records a session and baseline, launches the exact executable found by
discovery, relays supported approval requests, records normalized events and the
ending delta, then retires the child process. Unknown, disabled, or unavailable
runtimes refuse instead of silently falling back to native.

In a headless run, an approval request that needs a user answer is denied and
audited. On an interactive terminal, Garuda can ask for a `y/N` decision. A
reported `completed` status means the ACP agent ended its turn; it is not a
native verification verdict.

## Route the initial runtime

Initial selection uses the first applicable source in this order:

1. explicit `--runtime`;
2. a trusted global deterministic routing rule;
3. a project rule only when global settings authorize project routing;
4. the optional trusted classifier;
5. the configured default runtime; and
6. built-in `native`.

Global `~/.agent/settings.yaml` owns runtime manifests, rules, defaults, and the
classifier. A project may define aliases to trusted runtime IDs or opt out of the
classifier, but cannot provide an executable command or broaden global
authority. See [Initial runtime selection](configuration.md#initial-runtime-selection).

The classifier receives bounded task metadata and approved candidates, not file
contents or tools. Its choice is revalidated against runtime capabilities. A
timeout, malformed answer, low confidence, unavailable candidate, or capability
mismatch uses the configured default according to policy.

## Hand off a native session

Preview first. Without `--confirm`, the command does not switch ownership:

```bash
garuda runtime handoff --session latest --to opencode
```

Execute the reviewed handoff explicitly:

```bash
garuda runtime handoff --session latest --to opencode --confirm
```

Use `--workspace` when an older native session recorded a relative workspace and
the command is not being run from that directory.

The confirmed transaction is one-shot:

1. pause the native source;
2. checkpoint its state and capture the workspace delta;
3. start the exact target executable found during the pre-check;
4. persist the target segment and acknowledgement, then record its child;
5. close the native source;
6. send the bounded handoff package as the target's first prompt; and
7. close and retire the target after its turn.

A failure before acknowledgement rolls back to the native source. A failure
after acknowledgement is a target failure: Garuda closes the target and records
the failure, but does not overwrite changes the target may already have made.
The session belongs to the target after acknowledgement in either case.

Handoff packages contain bounded task state, evidence references, workspace
delta context, and redacted metadata—not vendor credentials, raw secrets, or an
unbounded copy of the transcript.

## Resume, recover, reclaim, and support

Every runtime command that accepts `--session` supports a full ID, the unique
prefix printed by `garuda sessions`, or `latest`. Ambiguous and missing
references fail before preview, lease acquisition, or mutation.

Classify and recover interrupted state:

```bash
garuda runtime recover --session latest --json
```

Recovery reports `resumable`, `rolled_back`, or `external`. A session owned by
an external runtime refuses native resume and another handoff while that target
may still be acting.

After proving the target is stopped, return ownership to native:

```bash
garuda runtime reclaim --session latest
garuda runtime resume --session latest -t "Continue after the external run"
```

Reclaim refuses while a workspace lease, live recorded child, or active target
state indicates the harness may still be running. It is an ownership repair,
not a rollback of external changes.

Generate a redacted diagnostic bundle without copying raw event payloads:

```bash
garuda runtime support --session latest
```

The bundle contains runtime lanes, kind tallies, timing metrics, and scrubbed
session metadata suitable for troubleshooting.

## Dashboard, SDK, and service surfaces

The dashboard's Runtimes board lists and inspects catalog entries, previews and
prepares handoffs, shows workspace deltas, and runs recovery. Dashboard chat
still starts the native loop; the runtime board operates on persisted sessions.

`SoftwareAgent(runtime="opencode")` executes one ACP turn and returns an
`AgentResult`. `Conversation` can start on ACP and switch ACP-to-ACP through the
transactional handoff path. It deliberately refuses an in-process native-to-ACP
switch; use the persisted CLI handoff for that transition.

The JSON-RPC service exposes runtime list, inspect, handoff, recover, and support
methods through the same trusted catalog. Request payloads cannot provide
runtime launch manifests.

## Vendor setup

### Claude Code

- Launch command: `claude-agent-acp` from
  `@agentclientprotocol/claude-agent-acp`.
- Install Node.js 20+, install and log in to Claude Code, then install the ACP
  package globally and verify `claude-agent-acp --version`.
- Authentication remains in the Claude Code login. Do not set
  `ANTHROPIC_API_KEY` when you intend to use subscription billing.

### Codex

- Launch command: `codex-acp` from `@agentclientprotocol/codex-acp`.
- Install Node.js 20+ and Codex CLI, run `codex login`, install the ACP package,
  and verify `codex-acp --version`.
- Authentication remains in the Codex CLI login.

### Cursor Agent

- Launch command: `agent acp`, a Cursor Agent CLI subcommand.
- Install and authenticate Cursor Agent, then verify `agent --version`.
- Garuda answers `session/request_permission`. Unsupported blocking extensions
  such as `cursor/ask_question` and `cursor/create_plan` fail closed with a
  method-not-found response.

### OpenCode

- Launch command: `opencode acp` from the `opencode-ai` package.
- Install and authenticate OpenCode, then verify `opencode --version`.
- Authentication remains in OpenCode's own configuration.

### Pi

- Launch command: `pi-acp` from the `pi-acp` package.
- Install the package, authenticate Pi, then verify `pi-acp --version`.

### Goose

- Launch command: `goose acp`, a Goose CLI subcommand.
- Install and authenticate Goose, then verify `goose --version`.

## Environment and protocol limits

- Shipped manifests declare no credential-file login probe, so login commonly
  appears as `unknown`.
- Adapter subprocesses receive a minimal environment containing `PATH`, `HOME`,
  and `LANG`. Provider API-key variables and custom vendor config-directory
  variables are not forwarded.
- Garuda drives its owned ACP subset: initialize, session creation, prompt,
  cancel, normalized updates, and permission requests. Vendor-specific model
  lists, modes, images, and blocking extensions outside that subset are not
  automatically supported.
- Discovery binds launch to the resolved absolute executable. Replacing the
  file at that path can still replace what runs; executable binding is not a
  sandbox.
- Deterministic repository tests use strict fake ACP servers. Real vendor
  compatibility checks are opt-in because they require an installed,
  authenticated CLI and may consume subscription quota.

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
cannot declare commands. Generic adapters receive no vendor-specific guarantees
beyond Garuda's supported ACP wire subset.

Garuda can also serve its native runtime as the agent side of a stdio ACP editor
session:

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
reported as an explicit skip; an installed but unauthenticated or incompatible
CLI fails the opted-in check. CI does not enable this variable or consume a
subscription.
