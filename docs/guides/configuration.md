# Configuration

!!! abstract "At a glance"
    - **Global** settings in `~/.agent/settings.yaml` are trusted: models,
      collection, routing, runtimes, and the switches that allow project code.
    - **Project** settings under `.agent/` can't enable Python tools or hooks,
      trust their own routing rules, or define runtime commands. They can
      still pick permissive profiles, start MCP server commands, and turn on
      collection.
    - Command-line flags beat profiles, which beat run-mode presets, which beat
      configuration defaults.

## Where settings live

| File | Scope | Holds |
|---|---|---|
| `~/.agent/settings.yaml` | All projects, trusted | Models and bindings, collection, routing, runtime manifests, `trust_project_hooks`, `load_project_tools`, `agents.project_ceiling`, `capacity` |
| `~/.agent/mcp.json` | All projects, trusted | MCP servers, including per-tool `tool_effects` |
| `.agent/settings.yaml` | One project | Project defaults, proposed routing rules, `runtime_refs` aliases, classifier opt-out |
| `.agent/mcp.json` | One project | Project MCP servers (`tool_effects` here is ignored) |
| `.agent/agents/`, `.agent/skills/` | One project | Profiles and skills |
| `AGENTS.md` or `GARUDA.md` | One project | Instructions added to every run |

Set `GARUDA_GLOBAL_SETTINGS` to read global settings from another path.

```mermaid
flowchart LR
  subgraph trusted["Trusted: your home directory"]
    g["~/.agent/settings.yaml<br/>~/.agent/mcp.json"]
  end
  subgraph project["Untrusted: the repository"]
    p[".agent/ and AGENTS.md"]
  end
  g -- "can allow" --> hooks["Project hooks and Python tools"]
  g -- "can trust" --> routes["Project routing rules"]
  p -- "proposes, cannot self-authorize" --> hooks
  p -- "proposes" --> routes
```

## Project home

Garuda discovers project configuration in `.agent/`; `.garuda/` remains a
backwards-compatible alias.

```text
.agent/
├── agents/       # YAML or agent.md profiles
├── skills/       # <name>/SKILL.md
├── tools/        # opt-in Python tools
├── mcp.json      # project MCP servers
└── settings.yaml # project defaults
```

Root project memory (`AGENTS.md`, `GARUDA.md`) must not self-authorize project
tools or hooks; those trust decisions live in global settings. Recipes for each
file are in [Level 3 · Teach it your project](../use-cases/customize.md).

## Models

Set `GARUDA_MODEL`, or pass `--model provider/model` for one run. The provider
credential must match the model. LiteLLM handles inference routing, retries,
prompt caching, streaming, and reasoning settings.

The reasoning model is resolved in this order, first match wins:

1. `--model` or `--reasoning-model` on the command line;
2. `GARUDA_REASONING_MODEL`, then the older `GARUDA_MODEL`;
3. model bindings: one chosen explicitly through the SDK's `model_binding`,
   then the profile's, the project's, and global settings';
4. the built-in default, `openrouter/deepseek/deepseek-v4-flash-0731`.

The optional collection model follows the same order with
`--collection-model` and `GARUDA_COLLECTION_MODEL`. The `[garuda] reasoning=…`
line at startup names the model and where it came from.

### Bounded collection model

A bound collection model does nothing unless collection is enabled. Global
settings set the default; a profile or project `collection:` block can
restate `enabled`, `profile`, and `handoff`, but numeric budgets may only
narrow the global ceiling.

- The reasoning model stays the controller and must explicitly call
  `delegate_collection`.
- The worker can't edit the workspace or complete the parent task.
- `--no-collection` removes the role and its tool entirely.

```yaml
# ~/.agent/settings.yaml
models:
  strong:
    model: openrouter/example/reasoning
  fast-reader:
    model: openrouter/example/fast-reader
model_bindings:
  default:
    reasoning: strong
    collection: fast-reader
model_bindings_default: default
collection:
  enabled: true
  profile: explore
  handoff: brief            # brief or none; full is rejected
  budget:
    max_jobs_per_run: 8
    max_parallel_jobs: 3
    max_turns_per_job: 12
    max_tokens_per_job: 30000
```

How a collection job behaves:

- It gets its own collection-model context and event log.
- A `brief` handoff contains the bounded working-state card and referenced
  buffer pointers, not the parent transcript.
- Requests may narrow workspace paths and web-source URL prefixes. Evidence
  outside those scopes, or pointing at a missing buffer, is rejected.
- The parent receives a bounded JSON report. The full child trace is stored
  under the parent session's `subagents/` directory.
- If an ordinary attempt ends after gathering evidence, Garuda makes one
  terminal-only attempt with only `submit_collection` available. The same
  report and evidence validation applies.

The non-mutating toolkit and path checks are guardrails. Use a read-only mount
or an isolated snapshot when strict write confinement is required.

## Model transports

!!! note "For contributors"
    Direct transports join `garuda/model/transports.py` only with all six
    admission artifacts: a vendor-support citation (https, public docs), a
    capability declaration, a test double, an opt-in integration test, cost
    semantics where unknown stays unknown, and migration notes. Private
    endpoints, reverse-engineered subscription paths, and copied OAuth material
    are never admissible. Subscription-backed agents stay behind ACP runtimes
    instead.

## MCP

Garuda resolves `.agent/mcp.json` or YAML, `.garuda/` compatibility files,
`.cursor/mcp.json`, and global `~/.agent/mcp.json`.

- A server from a project file (including `.cursor/mcp.json`) starts only after
  you trust it with `garuda mcp trust`. The grant covers that exact entry and
  any repository script it runs, in that repository; untrusted servers are
  skipped with `agent.untrusted_project_code`, and a profile that requires one
  refuses. `--mcp-config FILE` and the global file are yours and need no grant.
- Project and global servers merge by default; trusted project entries win name
  collisions. `GARUDA_MCP_MERGE=0` turns merging off.
- Above the direct-tool threshold (`GARUDA_MCP_MAX_DIRECT_TOOLS`), Garuda
  exposes lazy `search_tool` and `use_tool` meta-tools.
- `--mcp-config FILE` picks an explicit config for one run.

```bash
garuda mcp list --no-connect
garuda mcp list
garuda run -t "Use the issue tracker" --mcp-config custom-mcp.json
```

Collection workers treat MCP tools as effect-unknown by default. You can
authorize one exact remote tool for collection in the **global**
`~/.agent/mcp.json` entry. The same `tool_effects` key in project configuration
is ignored, so a repository can't grant its own tools collection authority.

```json
{
  "mcpServers": {
    "docs": {
      "command": "docs-mcp",
      "tool_effects": {
        "fetch_document": "read_only"
      }
    }
  }
}
```

This declaration is a trusted guardrail assertion, not confinement or proof
about the remote implementation. Side-effecting and unknown tools stay
unavailable to collection workers, and lazy `use_tool` calls re-check the
selected tool.

## Initial runtime selection

`garuda run` picks the runtime a session starts on from the first source that
applies:

```mermaid
flowchart LR
  a["--runtime ID"] --> b["Trusted global rule"] --> c["Project rule<br/>if trust_project_routes"] --> d["Classifier<br/>if enabled"] --> e["default_runtime"] --> f["native"]
```

Naming a runtime with `--runtime`, including `--runtime native`, pins it. Omit
the flag to let the later sources choose.
Global rules, defaults, project-route trust, and the classifier live in
`~/.agent/settings.yaml`:

```yaml
# ~/.agent/settings.yaml
routing:
  default_runtime: native
  fallback_runtime: native
  trust_project_routes: false
  rules:
    - id: readonly-review
      priority: 100
      runtime: codex
      when:
        agents: [reviewer]
        modes: [readonly]
        marker_files: [pyproject.toml]
```

- Every non-empty condition in a rule must match. Higher priority wins, then
  declaration order.
- Rules can match agent, mode, tags, required capabilities, workspace kind,
  permission ceiling, language, marker files, bounded task text, and paths.
- Unknown or executable-shaped fields fail closed.
- A project may propose declarative rules in `.agent/settings.yaml`. They are
  only recommendations unless global settings set
  `trust_project_routes: true`. Project rules can't contain task regular
  expressions or runtime commands.

### Classifier

The classifier is consulted only when no explicit choice or rule selects a
runtime:

```yaml
# ~/.agent/settings.yaml
routing:
  default_runtime: native
  classifier:
    enabled: true
    model_role: collection        # or reasoning
    allow_reasoning_fallback: false
    minimum_confidence: 0.75
    candidates: [native, codex]   # omit to offer every configured runtime
    max_output_tokens: 256
    timeout_sec: 20
    on_failure: default
```

- **Input:** the task text, agent, mode, language and marker-file names, and
  the approved candidates with their capabilities. No tools and no file
  contents.
- **Output:** revalidated. An invalid, low-confidence, incapable, or timed-out
  answer uses `default_runtime`.
- **Model:** the `collection` model of the global default binding (or the alias
  named by `model_binding`). Without one, classification is skipped unless
  `allow_reasoning_fallback` is true. It is also skipped when no model binding
  is configured at all.
- **Cost:** one attempt, with thinking and reasoning settings dropped so
  `max_output_tokens` holds.
- **Project opt-out:** `.agent/settings.yaml` may only disable it, with
  `routing: {classifier: false}`.

See [External harnesses](external-harnesses.md#route-the-initial-runtime) for
runtime discovery and execution behavior.

## Permissions and hooks

Use profile permission rules as guardrails, and a Docker workspace when you
need a real confinement boundary. Project hooks and project Python tools stay
opt-in because cloned repository code must not self-authorize execution:

```yaml
# ~/.agent/settings.yaml
trust_project_hooks: true
load_project_tools: true
```

Hooks are shell commands that receive a JSON event on stdin. A `before_tool`
hook is a guard and **fails closed**: exit 0 allows the call, exit 2 blocks it,
and anything else (another exit code, a command that can't start, a timeout)
blocks it too, logged as `hook.failed_blocked`. Each hook runs in its own process
group, which is killed on timeout:

```yaml
# ~/.agent/settings.yaml
hooks:
  before_tool:
    - match: "bash"
      command: "./guard.sh"
      timeout: 10            # seconds, at most 300 (default 30)
  after_tool:
    - match: "*"
      command: "./log-call.sh"
```

Set `on_failure: allow` on a hook in your global file to make it advisory; a
project's hooks always fail closed. If a guard rewrites a call, the rewritten
call is permission-checked again before it runs.

Profile rule syntax (`tool_rules`, `path_rules`, `bash_rules`) is shown in
[Create your own agent profile](../use-cases/customize.md#create-your-own-agent-profile).
Read [Safety and workspaces](safety-and-workspaces.md) before enabling project
code, permissive modes, network access, or host-backed execution.

## Runtime capacity

Limit how many runs of one runtime may be active at once, across every way of
starting one (CLI, SDK, dashboard, server, handoffs):

```yaml
# ~/.agent/settings.yaml
capacity:
  native: 2
  claude: 1
  codex: 1
```

A run that finds its runtime full is refused right away rather than queued. A
runtime with no entry isn't limited. Only the global settings file can set this.

## Agent definitions

An agent definition says how one Garuda agent behaves: its instructions,
tools, permissions, limits and model. The smallest useful one changes one
thing about a packaged agent:

```yaml
# ~/.agent/agents/careful-coder.yaml   (or .agent/agents/ in a project)
version: 1
extends: garuda/build
instructions:
  text: |
    Make the smallest change that fixes the problem.
```

`garuda run --agent careful-coder` then uses everything from the packaged
`build` agent except the extra instructions, which are appended to its own.

- A bare name is looked up in the project (`.agent/agents/`, then
  `.garuda/agents/`), then in your user directory (`~/.agent/agents/`), then
  among the packaged agents. `garuda/build` always means the packaged one;
  `user/<name>` and `project/<name>` pick a location explicitly.
- `extends` chains up to four levels. Settings merge key by key; lists
  replace; `tools: {add: [...], remove: [...]}` edits the parent's tool list
  and `preset: none` starts from an empty one; `instructions.mode: replace`
  drops the parent's text. A definition that extends itself refuses —
  extend `garuda/<name>` to change a packaged agent.
- Version 1 is strict: unknown fields, unknown tools, a missing instruction
  file and duplicate keys refuse, each with a code such as
  `agent.unknown_field`. A field Garuda recognizes but does not support yet
  (for example `memory:` or `hooks:`) refuses with `agent.unsupported_field`
  rather than being ignored.
- Numbers are checked before the agent starts: the output reserve plus the
  safety margin must fit inside `context.max_tokens`, `summarize_after_tokens`
  must be below it, and a deadline must be positive (`agent.invalid_budget`).
  A project definition cannot turn off `completion.verifier`, nor the
  acceptance contract in the `eval` or `rigorous` modes that require it
  (`agent.required_gate`), and its
  `workspace.docker` limits can only narrow what you granted.
- `garuda agent show NAME` prints every effective value and where it came
  from; `garuda agent prompt NAME` prints the system prompt the first request
  will send, section by section, and its digest. Both redact secrets unless
  you pass `--raw`, and neither starts an MCP server, a hook or a model. A run
  records the digest of the system message it actually sent; it differs from
  the static one once runtime blocks such as the environment snapshot are
  added.
- `memory:` chooses what else goes into the system prompt, after the agent's
  own instructions and in this order: your `~/.agent/AGENTS.md` (`user`, on
  by default for version 1 definitions), the skills index, the project's
  memory files (`project: [AGENTS.md, GARUDA.md]`, `project_mode: first` or
  `all`), and with `context_pack: true` the `.context/` architecture,
  decisions, discoveries and conventions files. Each file is cut at
  `max_chars` (8000) with a `memory.truncated` notice; the whole prompt is
  capped at `max_total_chars` (32000) and must fit the model's token budget,
  trimming the context pack first and then project memory. The agent's own
  instructions are never cut — a definition whose instructions do not fit
  refuses. Project memory must stay inside the repository.
- Files without `version` are legacy profiles and keep working unchanged.
  `garuda agent migrate PATH` shows the version 1 form and confirms it
  resolves to the same agent; `--write` replaces the file and keeps a backup.

## Roles and flows: garuda.yaml

`garuda.yaml` is a new, optional file that names **roles** — which harness and
exact model to use for which job — and the flows and checks built on them.
Nothing that works today needs it, and `settings.yaml` keeps working
unchanged beside it.

- The **user** file lives next to your settings, normally `~/.agent/garuda.yaml`.
- A **project** file `garuda.yaml` at the repository root may add checks and
  flows and narrow your roles.

```yaml
# ~/.agent/garuda.yaml
version: 1
defaults: {role: coder}
harnesses:
  claude-code: {allowed_models: [<exact-id>], max_parallel: 2}
  codex: {allowed_models: [<exact-id>]}
roles:
  planner:  {harness: claude-code, model_id: <exact-id>, permissions: smart, write_policy: no-edits}
  coder:    {harness: codex, model_id: <exact-id>, effort: high,
             fallback: [{harness: claude-code, model_id: <exact-id>}], consult: [reviewer]}
  reviewer: {harness: claude-code, model_id: <exact-id>, permissions: smart, write_policy: no-edits}
consults: {max_per_session: 5, timeout_sec: 600, max_answer_chars: 8000}
sessions: {isolation: auto, keep_days: 30}
```

How the layers combine (package default, then user, then project, then the
command line):

- A role or flow with the same name **replaces** the whole definition below it.
- Permission ceilings and consult limits **intersect**: the stricter one wins.
- Checks **accumulate**; an identical check is listed once.
- `harnesses`, `sessions.keep_days`, `fallback` chains and `consult` grants are
  **yours only**: a project file may keep a subset or remove them, never add.
- `--runtime` or `--model` on the command line skips an implicit
  `defaults.role`.

The file is strict: unknown or duplicate keys, YAML tags, values out of range
and an `authority` key are refused with the full path of the value, before
anything starts. A `settings.yaml` key such as `routing` in a `garuda.yaml`, a
`max_parallel` that disagrees with `capacity`, or a `--runtime` that
contradicts the selected role is refused as `config.conflict`.

`garuda config migrate` previews the user `garuda.yaml` that carries what
`settings.yaml` already says (trusted runtimes as `harnesses`, `capacity` as
`max_parallel`); `--write` applies it, keeping a backup of any existing file.
It never removes anything from `settings.yaml`, and running it twice changes
nothing.

A project file can narrow your roles without asking. It cannot make Garuda
run something it chose — its `checks`, or a model string for the `native`
harness — until you trust it: `garuda config trust` shows exactly what the
file would run and asks. Trust covers those exact bytes in that repository;
any change to the file needs trust again, and a symlinked `garuda.yaml` is
refused. Until then those values are ignored and the run says so
(`config.project_untrusted`). A role a project adds may not ask for more
than `agents.project_ceiling` allows, and a headless run can never create
trust.

`garuda run --role coder` (or `defaults.role`, unless you pass `--runtime` or
`--model`) runs as that role. A native role sets the model, reasoning effort,
permission mode and profile exactly as the matching flags would; a
`--permission-mode` you pass and the role's ceiling combine to the stricter
of the two. Model ids are used exactly as written, never matched loosely.

For an ACP harness, Garuda sets the agent's own model and effort options
(`session/set_config_option`) before the first prompt, and only on an adapter
version where that was proven (today Claude Code adapter 0.85.0 and Codex
adapter 2.1.1). The agent must offer the exact id in its session; a missing or
changed id, or an unproven adapter, refuses before anything is prompted. A
role with no model or effort runs on any adapter version. The role, the ids
set and the adapter version are recorded on the session.

### Fallbacks

A role's `fallback` list (your user file only; a project file may only remove
entries) is tried once, before the run starts. Garuda moves past the role's
own harness, or an entry, only when its CLI is not installed
(`harness.cli_missing`) or its documented login check says you are logged
out (`harness.logged_out`). A login check that fails, times out or answers
something unexpected is not a reason to move on. Once a prompt is sent there
is no fallback, and trust, configuration or confinement errors refuse rather
than fall back. The session records which harness was planned, which one ran
and why; if none can start, the run refuses and lists every reason.

## Environment variables

| Variable | Purpose |
|---|---|
| `GARUDA_REASONING_MODEL` | Reasoning model; beats `GARUDA_MODEL` |
| `GARUDA_MODEL` | Default model selection (reasoning role) |
| `GARUDA_COLLECTION_MODEL` | Collection model, used only when collection is enabled |
| `GARUDA_GLOBAL_SETTINGS` | Global settings path |
| `GARUDA_SESSIONS_DIR` | Session-store location |
| `GARUDA_MCP_MERGE` | Disable project/global MCP merge with `0` |
| `GARUDA_MCP_MAX_DIRECT_TOOLS` | Direct MCP schema threshold |
| `GARUDA_MODEL_MAX_CONCURRENCY` | Per-provider model-call cap |
| `GARUDA_TOKEN_PRICES` | Reproducible price overrides |
| `GARUDA_SERVE_TOKEN` | JSON-RPC server bearer token |
| `GARUDA_TRACING` | Enable OTLP tracing |
