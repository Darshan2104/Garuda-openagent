# Agent definitions: a configurable Garuda harness

**Status:** Reviewed implementation contract — security-default decisions require approval

**Date:** 2026-10-01

**Plan:** [Agent definitions implementation roadmap](../plans/2026-10-01-agent-definitions-implementation.md)

**Related:** [Teams and sessions](2026-10-01-teams-and-sessions-design.md) — the
orchestration layer that runs these agents as roles, flow steps and consults.

**Target base:** `origin/main` at `3456f25`; re-audit against the then-current
main before each PR.

**Tracking:** epic [#141](https://github.com/Darshan2104/Garuda-openagent/issues/141).

## Scope

Garuda has two halves:

1. **The harness**: the loop around one model — instructions, memory, skills,
   tools, permissions, hooks, context management, limits and completion checks.
2. **Orchestration**: sessions, roles, flows and consults that run several
   harnesses (Garuda's own, Claude Code, Codex) together. The teams design
   covers this half.

This design makes the first half a component that a user can define in one
small file, with a working default for every part. The same definition runs
from the CLI, the SDK, `serve`, the web dashboard, a teams role, a flow step
or a consult. Leaving a part out inherits its selected base. With no versioned
definition, existing runs retain their post-H.0 behavior except the explicitly
listed security defaults. The reference below is a versioned example, not a
claim that new user memory, skill sources or subagent restrictions already exist.

External harnesses keep their own instructions, skills and memory (for
example `CLAUDE.md`). Garuda applies to them only the parts it can prove; see
[External harnesses](#external-harnesses).

## What exists today

Most building blocks already exist. The problem is that they are spread across
files with different rules, several fail silently, and nobody can see the
result.

| Part | Today on main | Problem |
|---|---|---|
| Profiles | `garuda/agents/defaults/*.yaml`, project `.agent/agents/` (YAML or `agent.md`) | 40 flat keys; YAML and `agent.md` parsers drift; no user-level profiles dir; no inheritance |
| System instructions | `system_prompt` replaces the whole default | Changing one line means copying the full built-in prompt |
| Memory | First of `AGENTS.md` / `GARUDA.md` at the workspace root, cut at 8,000 chars | No user memory; the cut is silent; no way to add files or durable context |
| Skills | `SKILL.md` index from `.agent/skills`, `.garuda/skills` and `skills_dirs` | No user skills dir; `allowed-tools` is advisory only |
| Tools | `tools:` list plus `tool_rules`, `path_rules`, `bash_rules` | Unknown tool names are dropped silently; no per-tool options |
| MCP | Project `.agent/mcp.json`, `.cursor/mcp.json` and global `~/.agent/mcp.json` merge | Project stdio servers launch commands without any user trust |
| Hooks | `before_tool`, `after_tool`, `session_start`, `session_end` shell commands | A failing or timed-out `before_tool` guard allows the call |
| Subagents | `invoke_subagent(profile=...)` runs any profile | The child's permission mode is not capped by the parent |
| Models | Model bindings, effort and thinking budget | Works; kept as is |
| Visibility | None | No command prints the effective agent or its assembled prompt |

### Defects verified on `3456f25`

Reproduced against the archived checkout unless marked as read from code.

1. **Setting `skills_dirs` replaces the system prompt with the workspace path.**
   `resolve_system_prompt` reuses the variable `base` for the workspace
   directory, so the final prompt starts with the path instead of the
   instructions. Reproduced: the prompt began with the workspace path.
2. **`agent.md` profiles silently drop fields.** `reasoning_effort`,
   `thinking_budget_tokens`, `enable_acceptance_contract`, `model_binding` and
   `collection` are not read, but they are still recorded as declared, so the
   mode preset also leaves them alone. Reproduced: `reasoning_effort: high`
   loaded as `None`, and `enable_acceptance_contract: false` loaded as `true`.
3. **Unknown keys and tool names are ignored.** `systemprompt:` or `max_turn:`
   loads as the default with no warning. A misspelled tool silently disappears
   from the toolset. Reproduced.
4. **A cloned repository can raise its own permissions.** A project
   `.agent/agents/build.yaml` with `permission_mode: yolo` replaces the
   built-in `build` profile, and plain `garuda run` then runs without approval
   prompts. The configuration guide says the repository "proposes, cannot
   self-authorize". Reproduced at the loader; the run path was read from code.
5. **Project MCP servers execute without trust.** A project `mcp.json` or
   `.cursor/mcp.json` stdio entry starts its command on every run. Only
   `tool_effects` is restricted to the global file. Read from code.
6. **Subagents can escalate.** `SubagentRunner.run` builds the child's
   `PermissionEngine` from the child profile alone. A `smart` build run can call
   `invoke_subagent(profile="harbor")` and get `yolo` with bash and write tools.
   CLI `--permission-mode` does not reach the child either. The child also
   skips the shared setup: no workspace MCP/tools resolution and no mode preset.
   Read from code; not exercised with a live model.
7. **A broken `before_tool` guard allows the call.** A hook that fails to start
   or times out returns "allow". Read from code.
8. **Project memory is cut silently** at 8,000 characters.

Defects 1–3, 6 and 8 are fixed in task H.0 regardless of the rest of this
design. Defects 4, 5 and 7 change security-relevant defaults and are handled by
H.3 and H.7 with a compatibility path. Prioritize the permission repair (H.0b)
and independent authority/guard hardening before the feature compiler. None of
the proposed security-default decisions is approval to implement code yet.

Additional code-read risks belong to existing owners, not new promises:
`ToolRunner.prepare` screens before hooks rewrite calls (H.7);
`_project_memory_block` follows filesystem links (H.4's root-bound reads); and
explicit/lazy tools are added after initial toolkit selection (H.6's final
admission). These need executable reproducers before being called fixed.

## Principles

- **Zero configuration works.** No file retains the packaged build behavior
  after bug fixes and approved security defaults. Migration must not silently
  turn on new memory/skills sources or change completion posture.
- **Every part is optional.** A definition states only what differs from what it
  extends.
- **One schema, one loader.** YAML and `agent.md` produce the same validated
  object. CLI, SDK, `serve`, web, flows and consults all resolve through one
  shared service.
- **Show the result.** Users can print the effective agent with the source of
  every value, and the exact prompt it will send.
- **Mistakes are loud.** Unknown keys, tools, skills, files and MCP servers
  refuse in a versioned definition instead of being dropped.
- **Authority follows provenance, not a name or path string.** Packaged and user
  definitions remain within the operator/entry-point ceiling. A project may narrow and add
  labelled text, but may not grant execution or widen permissions without a
  user trust record.
- **Delegation never widens.** A subagent, flow step or consult gets at most its
  parent's permissions.
- **Guardrails are labelled as guardrails.** Tool rules, domain allowlists and
  skill tool lists are not confinement; Docker is the confinement boundary.

## The agent definition

### Smallest useful definition

```yaml
# ~/.agent/agents/careful-coder.yaml
version: 1
extends: garuda/build
instructions:
  text: |
    Make the smallest change that fixes the problem.
    Run the narrowest relevant tests before finishing.
```

Everything else comes from `garuda/build`. `garuda agent new careful-coder`
writes this file.

### Full reference

Every section and field is optional. This is an illustrative version 1
definition. Packaged defaults are generated from the validated shipped profiles,
not maintained independently in this example. Omission inherits; explicit null
is allowed only for nullable fields. Empty selections disable the selected
capability; they do not accidentally mean "all".

```yaml
version: 1
name: careful-coder             # default: file name
description: Implements changes in small verified steps
extends: garuda/build           # packaged agents are garuda/<name>; default: none

model:
  binding: default              # an alias from user model_bindings
  effort: null                  # low | medium | high, when the model supports it
  thinking_budget_tokens: null
  max_output_tokens: null

instructions:
  mode: append                  # append to the extended agent's text | replace
  text: ""                      # inline instructions
  files: []                     # extra instruction files, bounded; missing refuses

memory:
  user: true                    # ~/.agent/AGENTS.md when it exists
  project: [AGENTS.md, GARUDA.md]
  project_mode: first           # first | all
  max_chars: 8000               # per file; a cut is reported, never silent
  context_pack: false           # add .context/ architecture, decisions, conventions
  notes: "off"                  # off | propose; quote YAML's boolean-like words

skills:
  from: [project, user, packaged]
  include: null                 # null: all discovered; []: none
  exclude: []
  load: index                   # index (read on demand) | full

tools:
  preset: inherit               # inherit | all | read-only | none
  add: []
  remove: []
  options: {}                   # typed, per tool; see "Tools"
  mcp: null                     # inherit authorized selection; [] disables all
  subagents: [explore, plan, reviewer]

permissions:
  mode: smart                   # readonly | smart | auto | yolo
  rules:
    tools: {}                   # existing tool_rules
    paths: {}                   # existing path_rules
    bash: {}                    # existing bash_rules

write_policy: null             # inherit ceilings; optional no-edits (teams C.10)

hooks:
  replace: false               # only user authority may remove mandatory guards
  before_tool: []               # {match, command, on_failure: block | allow}
  after_tool: []
  session_start: []
  session_end: []

context:
  max_tokens: 128000
  condenser: microcompact
  summarize_after_tokens: 8000
  max_tool_output_bytes: 30720

limits:
  max_turns: 200
  deadline_sec: null

completion:
  mode: interactive             # interactive | eval | rigorous presets
  verifier: true
  acceptance_contract: null     # null: as the mode preset decides

workspace:
  kind: local                   # local | docker
  docker: {image: ubuntu:22.04, network: false, memory: 2g, cpus: 2}

output:
  schema: null                  # optional JSON Schema file for the final result
```

Every field maps to an existing `AgentProfile` or `AgentConfig` value, except
`instructions.mode/files`, `memory.*` beyond the root file, `skills.from/exclude`,
`tools.preset/add/remove/options/subagents`, `hooks.*.on_failure` and `output`.
Those are the new behavior in this design.

### Inheritance

- `extends` names one agent. Chains are allowed up to depth four; a cycle refuses
  with `agent.extends_cycle`.
- Maps merge key by key; scalars and plain lists replace.
- `tools.add` and `tools.remove` apply to the extended agent's resolved tool list.
  `preset` replaces it first when it is not `inherit`.
- `instructions.mode: append` adds this agent's text after the extended agent's
  instructions. `replace` uses only this agent's text.
- Hooks append to the extended agent's hooks; to drop inherited hooks, set
  `hooks: {replace: true, ...}`.
- A name that exists in several places resolves by location precedence below.
  To change a packaged agent without copying it, extend `garuda/<name>`; a bare
  self-reference refuses. Use qualified `user/<name>` or `project/<name>` to
  disambiguate overrides; identity is canonical source plus content digest,
  not just a name.

Merge is structural first; authority is intersected afterward using provenance
of every inherited field. Project inheritance cannot launder a project command
or instruction file into user authority. A project cannot replace/remove
operator guards, add executable capabilities or widen the inherited/user
ceiling. A user-authored root definition can change defaults within the operator
ceiling. Migration previews safety tightening separately from formatting.

### Where definitions live

| Location | Authority | Notes |
|---|---|---|
| `garuda/agents/defaults/` (package) | Packaged | Referenced as `garuda/build`, `garuda/explore`, `garuda/plan`, `garuda/reviewer`, `garuda/harbor` |
| `<global_settings_path().parent>/agents/` | User | New; usually `~/.agent/agents/`. Available in every project |
| `.agent/agents/`, then `.garuda/agents/` | Project | Existing locations |
| `--agent-file PATH` | User request | One run only |
| SDK `AgentSpec` or dict | Caller code | Within the host application's ceiling; never accepted over HTTP |

Bare names resolve project, then user, then packaged, so existing project
overrides of `build` keep working. `garuda agent list` shows which file each
name came from. A project definition that shadows a user or packaged name is
marked as such in the run banner.

Both formats are supported. In `agent.md` the front matter holds the fields and
the body becomes `instructions.text`. Declaring both body and inline text
refuses instead of silently choosing one.

Names are bounded identifiers, not filesystem paths: reject traversal,
separators, absolute paths and unsupported namespace prefixes. User agents,
skills and memory all resolve relative to `global_settings_path().parent`,
including home compatibility and environment overrides. Read source files once
with bounded no-follow access; a project file, memory, skill or schema reference
cannot escape its project root through `..`, absolute paths or symlinks. Other
roots require explicit user authority. Instruction/schema paths resolve against
the declaring file's directory; project memory resolves against workspace root.

Safe YAML/front matter uses one strict field/type validator, rejects duplicate
keys (including legacy profiles), unsafe tags and excessive aliases/depth/size.
Unknown future versions refuse; quoted `"off"` is a string, not YAML false.
Unknown legacy non-security keys warn, but invalid security values/rules refuse.

### Legacy profiles

A definition without `version` is a legacy profile. It loads through the same
validator after a mechanical translation of the existing flat keys
(`system_prompt` → `instructions` with `mode: replace`, `tool_rules` →
`permissions.rules.tools`, and so on). For legacy profiles:

- Defects 1–3 are fixed (H.0). Unknown keys and tool names produce a warning
  naming the key, not a refusal.
- `garuda agent migrate PATH` previews the version 1 form; `--write` writes it
  with a backup and preserves the effective post-fix behavior. Any unavoidable
  security/strictness difference is shown before writing and needs confirmation.

Version 1 definitions refuse unknown keys, duplicate keys, unknown tools,
skills, MCP servers and missing instruction files.

## Prompt assembly

The system prompt is built from labelled sections in a fixed order, so it is
predictable and the stable prefix can be cached by providers:

1. Agent instructions (packaged base, then `extends` chain, then this agent).
2. User memory: `~/.agent/AGENTS.md` and accepted user notes.
3. Skills index (or full bodies when `load: full`).
4. Project memory files, labelled with their path as project instructions.
5. Accepted project notes.
6. Durable context pack files, when `context_pack: true`.
7. Runtime blocks Garuda already adds (state card, goals), unchanged.

Each section names its source. Project sections are labelled as project text;
they do not claim the authority of the agent's own instructions. Each file has a
character cap; a cut adds `memory.truncated` to the run preflight with the file
and the dropped size. The whole assembled instruction text has a cap
(default 32,000 characters). When it is exceeded, sections 6, 5 and 4 shrink in
that order; agent instructions are never cut, and a definition whose own
instructions exceed the cap refuses.

`garuda agent prompt NAME` prints the assembled prompt with section sizes, and
`--json` returns sections, sources, sizes and a prompt digest. The digest is
recorded in the session record so a run can be traced to its exact
instructions.

An immutable `ResolvedAgent`/`PromptPlan` is frozen before any model request,
hook, custom-tool import, MCP connection or container launch. Include source
digests, effective ceilings, tool identities/options, model binding, capability
requirements and prompt sections. `show`, `prompt`, `list` and `check` perform
pure local inspection: no code imports, skill scripts, MCP launches, probes or
inference. Dynamic MCP tool validation is unknown offline, never guessed.

The prompt command reproduces the static first-request system prompt for the
same workspace/configuration inputs. Runtime state cards, goals, resumes and
task-dependent blocks are separately marked; a standalone command cannot know
future session state. Capture the actual outbound system-message digest at
each request boundary. Inspection redacts secrets by default and never returns
raw prompts over HTTP or into aggregate logs; an explicit local `--raw` can
print authorized source text without persisting it. Report UTF-8 bytes,
characters and estimated tokens with the estimator named. HTTP agent errors
and warnings use source-free fixed messages and registry-validated codes,
including malformed YAML/Markdown, prompt inspection and setup fallback failures.
Setup consumes the inspector's existing byte/character/token counts and labels
all three metrics; it does not recompute prompts or expose their bodies.
Detailed source diagnostics remain local; secret-pattern redaction alone cannot
protect ordinary instruction or memory text in an exception.

Cap the complete prompt, including user memory and full skills. Trim only
optional memory sections at safe text boundaries with diagnostics; reject
oversized mandatory instructions, skills or runtime blocks. Enforce the model's
effective context budget after reserving output and safety margin, even if the
character cap passed. Labels prevent envelope breakout, not semantic injection
immunity. Project agent instructions/templates need content-bound trust as in
teams C.2; ordinary project memory/skills remain labelled untrusted input.

## Memory

Memory means four different things. Each has its own managed writer and review
boundary. Neither a prompt rule nor path screening prevents every host-shell
write: the managed service never lets a model publish its own instructions or
accepted memory, and Docker read-only mounts provide filesystem prevention when
required. Current-run inputs are frozen; modified sources invalidate reuse and
require renewed trust where applicable. Do not claim host guardrails make memory
physically immutable.

| Kind | Where it lives | Who writes it | Default |
|---|---|---|---|
| Instruction memory | `~/.agent/AGENTS.md`, project `AGENTS.md`/`GARUDA.md`, `memory.project` list | The user | On; files only load when they exist |
| Durable project context | `.context/architecture.md`, `decisions.md`, `discoveries.md`, `conventions.md` | Reviewed repository content | Off; `context_pack: true` |
| Session memory | Session store, resume and tagged sessions | Garuda (`ContextPackManager`, teams B.3/B.7) | As the teams design specifies |
| Reviewed notes | `~/.agent/memory.md`, `.agent/memory.md` | The user accepts proposals | Off; `notes: propose` |

### Reviewed notes

With `notes: propose`, the agent receives a `remember` tool. A call records a
proposal (text up to 500 characters, scope `user` or `project`, and the session
that proposed it) in the session store. It does not change any memory file.
`notes: "off"` disables both the tool and accepted-note prompt sections. Limit
proposals to ten per root task, shared by descendants; scope comes from the
owner's project/user binding, never a model-supplied path.

- `garuda memory review` lists pending proposals; the user accepts, edits or
  rejects each one. Accepted notes are appended to the scope's memory file.
- Proposals pass the existing redaction gate; anything that looks like a secret
  is rejected at proposal time.
- Headless runs can propose but never accept.
- The dashboard shows pending proposals read-only.

Use root/session-scoped proposal ids, bounded count and store-relative paths;
models cannot pick a destination file. Acceptance binds the exact proposal
digest and scope to an explicit local user decision. Lock, atomically publish
and journal the note/proposal transition so replay cannot append twice. Reject
symlink/foreign-project targets, stale approvals and changed proposals. Reviewed
notes are data, never execution/configuration authority. Secret detection is
best-effort, not a guarantee; previews and diagnostics avoid persisting rejected
content. Without these gates, `notes: propose` is unavailable, not fail-open.

This keeps the repository rule that only Garuda writes generated context and
that durable content is reviewed.

## Skills

- Sources: project `.agent/skills` (and `.garuda/skills`), user
  `~/.agent/skills` (new) and packaged skills. `skills.from` limits which
  sources apply; `include` and `exclude` filter by name.
- When two sources define the same skill name, project wins over user, user
  over packaged; `garuda agent show` lists the winner and the shadowed copies.
- A skill whose `allowed-tools` names a tool the agent does not have produces
  `skill.tool_not_granted` in `garuda agent check`, not only a log line.
- `allowed-tools` stays an instruction to the model. It is not enforced and is
  described as advisory. Enforcement is deferred.
- Project skills are project text. They load into the index like project memory
  and cannot grant tools or permissions.

## Tools

- `preset` picks a starting set: `all` (every built-in), `read-only` (the
  built-ins that do not write, run commands or use the network) or `none`.
  `inherit` keeps the extended agent's list.
- `add` and `remove` adjust it. Unknown names refuse with `agent.unknown_tool`.
- `options` sets typed per-tool settings, for tools that declare an option
  schema. The first set is bash command timeout and output cap, and allowed
  domains for `web_fetch`/`web_search`. Domain lists are guardrails, not network
  confinement. An unknown option refuses.
- `mcp` selects MCP servers by name from the permitted MCP configuration. An
  unknown name refuses.
- `subagents` lists the agents `invoke_subagent` may start. The tool's schema
  enumerates exactly these names; any other name refuses at the service, not
  only in the schema. Versioned defaults select the read-only agents; legacy
  selection remains compatible but is subject to H.0b's delegation ceiling.
- Project Python tools keep the existing `load_project_tools` gate.

The final toolkit, including explicit/custom tools and lazy `use_tool` wrappers,
must respect removal, parent ceiling and actual resolved target identity. A
wrapper cannot bypass permission/effect checks on the underlying MCP call.
Offline selection never connects an excluded server to discover its tools.

## Permissions and delegation

The four existing modes remain. Their nominal order
`readonly < smart < auto < yolo` is useful for display, **not sufficient for
authorization**: explicit tool/path/command rules can ask or deny independently.

- Compose immutable child, parent, operator, source and entry-point policy
  decisions for the exact operation: `DENY > ASK > ALLOW`. Do not merge
  allow-prefix lists or select only the lowest mode. Retain each policy source;
  approvals are evaluated once through the parent's installed broker/handler.
- A child inherits the already-owned environment/workspace/network boundary,
  remaining deadline and permitted tool/resource identities. It cannot acquire
  a new host workspace, reconnect a new MCP server, import project code or
  drop guards while constructing itself. Unknown custom/MCP effects deny
  under a read-only ceiling. Narrowing the profile is not grounds to reject an
  otherwise usable child; required incompatible capabilities refuse before launch.
- H.0b only repairs delegation. It reuses the parent's admitted resources;
  routing full top-level setup into a child before project-code trust exists is
  forbidden. H.8 later shares the pure resolver plus explicitly authorized
  activation. Collection, rigorous planner/critic, flow and consult delegates
  must use the same ceiling contract; dedicated read-only profiles can tighten it.
- H.6 enforces allowed targets, recursive depth (default two), root delegated
  launch count (default eight), child turn limits (at most 50 and parent's
  remaining allowance), shared capacity and deadline/cancellation propagation.
  Limits persist through resume when sessions exist; delegates cannot reset the
  root budget. A consult disallows all further delegation regardless of agent.
- CLI `--mode readonly`, server/dashboard operator caps and parent denials
  cannot be removed by choosing a named agent, SDK dictionary or model output.
  Only an explicit local operator policy can authorize a wider root ceiling.
  `--agent-file` is a source selection, not automatic executable trust.

## Hooks

The four events and stdin JSON stay. Policy is source-aware:

- A guard defaults to fail-closed for **both legacy and versioned** definitions.
  Spawn failure, nonzero guard exit, exception, malformed rewrite or timeout
  blocks with `hook.failed_blocked`. Keeping legacy guards fail-open by
  default would leave defect 7 unfixed. This security-default decision needs
  approval before code implementation.
- Optional notification hooks are explicitly non-authoritative. A user may
  grant per-hook `on_failure: allow` for an advisory hook; project inheritance
  cannot turn a blocking guard into an advisory hook or remove it.
- Parent/operator mandatory guards survive `hooks.replace`. Execute each
  guard once, bound total chain duration/output and process-group lifetime;
  timeout/cancellation reaps descendants before teardown. Uncertain death is
  quarantined, not ignored.
- A `before_tool` rewrite must be schema-valid and reauthorized for its final
  tool identity/arguments before execution, including sequential/parallel/lazy
  wrapper paths. Approval of old arguments never authorizes rewritten ones.
  On current main, permission screening precedes hooks; this gap is an additional
  code-read finding, not an already reproduced exploit.
- Project command hooks require content-bound trust and still operate within
  the owner workspace/isolation policy. Hook events contain bounded, redacted
  data; hook input is not permission to export secrets or reasoning.

## Authority

| Project input may | Project input cannot self-authorize |
|---|---|
| Supply labelled project memory/skills and trusted agent instructions | Increase operator/source/parent execution ceilings |
| Narrow tools, rules, budgets and isolation | Import Python tools, run hooks or launch MCP commands |
| Choose user-authorized model binding aliases | Define providers, endpoints or credential sources |
| Reference already-authorized MCP identities | Shadow their executable, URL, headers, environment or effects |
| Extend packaged/user agents with provenance retained | Remove mandatory guards or switch Docker to host/network |

`agents.project_ceiling` is user-only, default `smart`. The security cap
applies to legacy and versioned profiles and every entry point. A requested
project widening refuses with `agent.project_widening` before any launch.
A user can raise the project ceiling to `yolo`, but this never overrides a
parent/server/operator ceiling or authorizes project code by itself.

### Content-bound project code trust

Hash-bound per-entry trust exists in H.3 from its first release; there is **no
temporary broad global trust flag** masquerading as approve-once behavior.
Use a focused user-local trust store independent of teams orchestration, then
reuse/adapt it in teams C.2 instead of replacing its semantics.

Interactive approval binds canonical repository identity, source authority,
entry name, command/argv or URL, cwd, environment/header specifications, referenced
executable/script content digests, schema version and authorization type.
Use the exact approved resolved snapshot at activation. Changed entries, changed
scripts, symlink replacements or shadowing invalidate trust. Resolve environment
references only within user-authorized bindings; project text cannot request
arbitrary credential variables. Never persist resolved secrets. This is a
configuration/code-selection grant, not proof of a subprocess's behavior.

Prompt once per content-bound identity, not once globally. Headless runs skip
untrusted optional servers with `agent.untrusted_project_code`; an explicit
`tools.mcp` requirement refuses before model/custom-code activation. Filter
and authorize **before** importing tools, starting stdio servers or making HTTP
requests. Network transports additionally obey existing network/SSRF policy;
approval does not grant arbitrary network access or readable credentials.

User MCP entries keep user provenance; a project entry with the same name cannot
inherit their trust or `tool_effects`. Public inspection reports this collision.
The trust directory and records use owner-only, no-follow, locked atomic writes;
corrupt/missing/unsupported stores cannot create grants. H.3 uses canonical local
repository identity before teams B.1 exists, with an explicit verified migration
to its project ids later.

Compatibility is explicit: raise the project ceiling in user settings and grant
each project-code identity separately. There is no one-line option in this
design to trust every present/future repository MCP command. Existing broad
hook/custom-tool flags are shown as unsafe compatibility authority and never
satisfy the new per-entry MCP contract.

## Using an agent everywhere

| Entry point | How |
|---|---|
| CLI | `garuda run -t "TASK" --agent careful-coder`, `--agent-file ./agent.yaml` |
| Chat and `serve` | `--agent NAME`; `serve` accepts names only, never inline definitions over HTTP |
| SDK | `SoftwareAgent(agent="careful-coder")`, `SoftwareAgent(agent=AgentSpec(...))` or a dict |
| Teams role | `roles.coder: {harness: native, agent: careful-coder}` |
| Flow step | A step names a role; a native role's agent supplies its harness settings |
| Consult | A native consulted role uses its agent's model, instructions, memory and skills, but the consult profile's read-only tools and limits |

The SDK exposes the resolved object: `AgentSpec.load("careful-coder", workspace=...)`
returns the validated definition and provenance, and `.narrow(...)` returns a copy with stricter
values. Narrowing cannot widen permissions or add tools beyond the original.
It also cannot expand MCP targets, subagents, hooks, network, workspace access
or budgets, or disable required completion/schema checks. SDK callers remain
within the host application's policy. Named selections over HTTP have an
operator-owned allowlist and ceiling; choosing `garuda/harbor` cannot raise a
server's authority.

`prepare_agent_run` remains the one shared setup path. All entry points pass an
agent reference to it rather than profile names plus loose overrides.
Split pure resolution from side-effecting activation; preserve the existing
return projection while moving callers. Inspection never calls activation.
Freeze each run's inputs, not process-global mutable configuration; changing a
definition during chat/resume creates a new identified segment or refuses,
never silently changes the existing run.

## External harnesses

Claude Code and Codex are separate harnesses with their own instructions,
skills and memory. Garuda does not try to make them honor Garuda skills or
memory. For an ACP role, an agent definition may set only:

| Section | Applied as | Gate |
|---|---|---|
| `model` | Exact model id through `session/set_config_option` | Teams A.3 capability spike |
| `model.effort` | Effort config option | Only where A.3 proved it for the adapter version |
| `permissions` and `write_policy` | Broker ceilings | Existing broker |
| `instructions` | A labelled context block before the first task message, not a system prompt | A.3 proves prompt content blocks; marked as "user-supplied role instructions" |
| `tools.mcp` | `session/new.mcpServers` | Teams G.1 forwarding proof |

This table describes a runtime-specific resolved projection, not permission to
send a fully defaulted native agent to ACP. A native `agent:` reference stays
native; changing runtime requires a separately authorized ACP role/projection.
For ACP, validate only explicitly declared/inherited ACP-compatible fields;
do not inject native default memory/tools/hooks. `model.binding` is native-only;
ACP uses the teams role's exact advertised `model_id`. If both selections name
different models/effort, refuse `config.conflict`, never silently override.
Field-level support is required: generic `model` does not imply thinking/output
token budgets, and broker ceilings do not imply filesystem prevention.
External `readonly`, MCP/network and consulted-child isolation gates remain
the teams contracts. Any explicitly requested unsupported field refuses with
`agent.field_unsupported`, including unsupported inherited fields; never ignore it.
Instruction replacement is retained as structural intent during resolution and
inheritance. ACP refuses explicit/inherited `instructions.mode: replace` even
when the resulting text is empty, equals the native base, or starts with it.
Appending a child block does not erase an ancestor’s replacement request.
Native prompt assembly is unchanged; only appended context is projected to ACP.

## Compilation, completion and output

Every field has a typed owner/mapping and observable acceptance case: model
binding/effort, mode presets, condenser/context reserves, output shaping,
turn/deadline limits, completion flags and workspace settings. Validate numeric
finiteness/ranges and cross-field budgets before activation. Mode/profile/CLI
precedence preserves existing explicit-field behavior; permission authority is
intersected separately. An explicit user root flag may change posture, never a
parent/consult/operator requirement. Completion/schema constraints cannot be
disabled by a project or narrower delegate.

`workspace.kind` selects only an already-authorized environment. Existing tmux,
sandbox and remote workspace invocations stay supported through the legacy
projection; defining new workspace kinds is not part of H. A child cannot
reconstruct an environment from its own workspace block. Docker network/resource
settings can only narrow the owner grant.

The optional final-output schema is a bounded local JSON Schema with a pinned
supported dialect and validator; version 1 uses draft 2020-12. No remote `$ref`,
file reads outside its approved root, custom executable validators or unbounded
reference expansion. Compile locally without network; reject unsupported schema
features rather than ignoring them. Validate the actual final result before
terminal acceptance, retaining ordinary completion and trusted verification
gates. Schema failure permits at most two declared repair attempts within root
turn/deadline budgets, then fails with `agent.output_invalid`; raw invalid output
never becomes a successful artifact. Passing shape is not task verification.
This behavior has its own roadmap task, H.12, rather than an unowned schema field.
During staged delivery, schema recognition is not support: an explicitly
requested field with no shipped compiler/owner refuses before activation.
Inspection labels it unsupported. New packaged defaults are enabled only with
their owner implementation; legacy behavior is not silently changed in an
earlier PR just because a future field exists in the schema.

## Commands

| Command | Does |
|---|---|
| `garuda agent list` | Names, source, `extends` and description |
| `garuda agent show NAME [--json]` | The resolved definition with the source of every value |
| `garuda agent prompt NAME [--json]` | The assembled prompt with sections, sizes and digest |
| `garuda agent new NAME [--from AGENT] [--project]` | Writes a minimal definition (user dir by default) |
| `garuda agent check [NAME or PATH]` | Validates and prints diagnostics with fixes |
| `garuda agent migrate PATH [--write]` | Previews or writes the version 1 form of a legacy profile |
| `garuda memory review` | Accepts, edits or rejects note proposals |

The dashboard setup view (teams F.4) lists agents with source, resolved fields,
prompt digest and section sizes, and the conversation view shows which agent and
prompt digests each native execution used. Recorded execution bindings preserve compiled
agent identities across session metadata changes and inner runs; historical unbound
measurements remain unattributed. ACP execution rows show attempted host request digests, recorded runtime/role
definition identity and unknown internal system prompts. Recorded matching runtime-tenure references connect sending executions to
segments; historical or inconsistent references stay unassociated and tenures
without measurement evidence remain unknown. Full #173 acceptance requires its
fixture, HTTP privacy and live-Chrome audit.
The dashboard does not edit definitions.

## Diagnostics

Codes join the teams diagnostic registry (C.4): `agent.unknown_field`,
`agent.unknown_tool`, `agent.unknown_skill`, `agent.unknown_mcp_server`,
`agent.extends_cycle`, `agent.instructions_too_large`,
`agent.instruction_file_missing`, `agent.field_unsupported`,
`agent.project_widening`, `agent.untrusted_project_code`,
`agent.delegation_narrowed`, `memory.truncated`, `skill.tool_not_granted` and
`hook.failed_blocked`. Each code has a one-line fix shown by the CLI, `--json`
and the dashboard.
H.0/H.3/H.7 expose these codes independently of C.4; later wiring reuses the
same vocabulary. Add `agent.path_invalid`, `agent.source_changed`,
`agent.output_invalid`, `agent.budget_invalid` and `agent.delegation_exhausted`.

## Non-goals

- Automatic managed publication of agent-authored instructions/accepted memory.
- A template language for instructions; files and text are inserted as written.
- Enforcing skill `allowed-tools`.
- Making Claude Code or Codex load Garuda skills, memory or hooks.
- Changing the model during a run.
- A plugin marketplace or remote agent registry.
- Editing definitions from the dashboard.
- Removing `settings.yaml`, legacy profile keys or environment variables.

## Decisions to record in implementing PRs

1. One agent definition schema and loader serves YAML, `agent.md`, files and SDK
   objects; legacy profiles translate into it.
2. Bare-name precedence is project, user, packaged; `garuda/<name>` always means
   the packaged agent.
3. Version 1 definitions refuse unknown keys, tools, skills, MCP servers and
   missing instruction files; legacy profiles warn.
4. Delegated runs get at most their parent's effective permissions.
5. A project definition cannot exceed the user's project ceiling (default
   `smart`) or start project MCP commands without a trust record.
6. A failing legacy or versioned guard blocks; explicit user advisory hooks
   can fail open. Final rewritten tool calls are independently authorized.
7. Agents may propose notes; only the user accepts them into memory.
8. External harnesses accept only the proved subset of a definition; other
   sections refuse.
9. Project code trust is content-bound from its first release; broad flags
   cannot serve as per-entry approve-once records.
10. Inspection is pure and redacted by default. Actual outbound prompt identity
    is recorded separately from task-independent inspection.
11. Managed note acceptance is digest/scope-bound and crash/replay safe; host
    memory guards are not filesystem confinement.
12. Output schemas are locally compiled, bounded and enforced before acceptance;
    passing shape cannot establish verification.

## Acceptance boundary

This design is implemented when every entry point resolves agents through the
one shared service; static inspection matches the frozen plan and actual outbound
prompt digests include runtime state; verified defects have regression tests
that fail on the refreshed pre-fix base; project
widening, untrusted MCP and delegation escalation refuse at the service boundary;
and no surface describes tool rules, domain lists or skill tool lists as
confinement.
