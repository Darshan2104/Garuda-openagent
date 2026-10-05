# Agent definitions implementation roadmap

**Status:** Reviewed roadmap — security defaults await approval; no code PRs started

**Date:** 2026-10-01

**Design:** [Agent definitions: a configurable Garuda harness](../design/2026-10-01-agent-definitions-design.md)

**Related roadmap:** [Teams and sessions](2026-10-01-teams-and-sessions-implementation.md)

**Target base:** `origin/main` at `3456f25`

**Tracking:** epic [#141](https://github.com/Darshan2104/Garuda-openagent/issues/141).
Task issues: H.0a #146, H.0b #142, H.0c #150, H.1 #159, H.2 #161, H.3 #143,
H.4 #162, H.5 #163, H.6 #164, H.7 #144, H.8 #166, H.9 #171, H.10 #172,
H.11 #173, H.12a #160, H.12b #165.

## Objective

Make Garuda's own harness a component that users define in one small file, with
a packaged default for every part, and that every entry point and the teams
layer run the same way.

The work is one set, **Set H**, in four lanes:

1. fix verified defects (H.0), shippable immediately after authorization;
2. one schema, loader and resolved view (H.1–H.3);
3. the configurable parts: prompt and memory, skills, tools, hooks, SDK and
   notes and compilation/output contracts (H.4–H.9, H.12);
4. the bridge to teams roles and the dashboard (H.10–H.11).

## Delivery rules

The teams roadmap's delivery rules and test authoring gate apply unchanged. In
addition:

- Behavior with no definition files and no new settings stays identical, except
  for the H.0 defect fixes and the security defaults that H.3 and H.7 change
  with a stated compatibility path.
- Every new field is resolved in `garuda/agents/` and consumed through
  `prepare_agent_run`; no entry point parses definitions itself.
- Each PR adds the fields it implements to `garuda agent show` and the
  reference docs in the same PR.
- Recognized but unimplemented fields refuse activation with unsupported-field
  diagnostics; inspection is honest about availability. New default sources
  turn on with their owner PR, not when schema placeholders first land.

## Dependency map and canonical PR order

The security lane is independent of teams **and** the feature schema. Do not
leave project execution exposed until inheritance/UI work is finished. All
security-default changes below require explicit approval before code PRs.

| Lane | PR sequence | Prerequisites / release contract |
|---|---|---|
| Independent fixes | H.0b, H.0a, H.0c | Current-main reproduction; no new project-code discovery |
| Independent security | H.3, H.7 | Approved default changes; legacy setup/broker boundary, no H.1/teams dependency |
| Schema | H.1 | H.0a/H.0b, H.3/H.7 security contracts; pure resolution before activation |
| Core compilation | H.12a | H.1; model/context/limits/completion/workspace mapping |
| Inspection | H.2 | H.1/H.12a; report implemented fields, no runtime activation |
| Prompt/memory | H.4 | H.1/H.2/H.3; H.0c diagnostics reused |
| Skills/tool surfaces | H.5, H.6 | H.1/H.3; H.6 uses H.0b ceilings and H.7 final-call reauthorization |
| Final-output schema | H.12b | H.12a/H.4/H.6; ordinary completion gates retained |
| Entry points | H.8 | H.2/H.4/H.5/H.6/H.7/H.12; one frozen resolved agent |
| Reviewed notes | H.9 | H.4/H.8; teams B.3 for managed session persistence |
| Teams native bridge | H.10 native PR | H.8, teams C.1/C.3; G.2 for consult enforcement |
| Teams ACP projection | H.10 ACP PR | Native bridge and A.3/G.1 exact-version field gates |
| Dashboard | H.11 | H.2/H.8, teams F.1/F.4; raw prompts excluded |

H.0–H.8 and H.12 require no teams implementation. H.3 provides content-bound
local trust immediately, with a verified adapter/migration to teams C.2/B.1
later; a broad flag is not an interim implementation. H.10 cannot precede the
tool, prompt and entry-point compiler it promises to run. H.9 remains optional.
Implementation uses the then-current main; re-audit each cited path before a
PR.

### Evidence gate

Each code PR first reproduces its regression against the refreshed base without
paid inference, then repairs the public owning boundary. Retain independent
expected values, filesystem/process effects and captured model requests. Do not
count a configuration attribute or mock callback as proof of enforcement.

Security proofs include both a denial and a permitted control, nested delegates,
rewritten calls, lazy MCP wrappers, server operator ceilings, file replacement
and changed trust hashes. Report live integrations actually run/not run.
H.0b needs no live model: ScriptModel can drive the real delegation/tool path.
Full pytest/ruff and relevant docs checks remain implementation merge gates.

## Set H — agent definitions

### H.0a Prompt, `agent.md` and unknown-key fixes

**Files:** `garuda/agents/loader.py`, `garuda/agents/md_loader.py`, profile tests.

- `resolve_system_prompt` keeps the instructions when `skills_dirs` is set; the
  workspace path variable gets its own name.
- One field table drives both the YAML and `agent.md` parsers, so they cannot
  drift. `declared_fields` is computed from that table for both.
- Unknown profile keys and unknown tool names log a warning naming the key or
  tool and the file.
- Reject duplicate keys and invalid security values instead of normalizing an
  ambiguous policy. Expected parsed values come from independent fixtures,
  not the implementation's field table.

**Acceptance:**

- A profile with `system_prompt` and `skills_dirs` produces a prompt that starts
  with the instructions and contains the skills index. Fails on `3456f25`.
- An `agent.md` profile with `reasoning_effort: high` and
  `enable_acceptance_contract: false` resolves to those values through
  `prepare_agent_run`. Fails on `3456f25`.
- A parametrized test over every profile field loads the same value from YAML
  and `agent.md`.
- `systemprompt:` and an unknown tool each produce exactly one warning.

### H.0b Delegation ceiling for subagents

**Files:** `garuda/core/subagent.py`, parent run wiring,
`garuda/core/permissions.py` and existing subagent/permission owner tests.

- Pass the parent's actual effective engine/installed broker and admitted
  tool/resource identities, not just its nominal mode or pre-broker handler.
- Compose per-operation child/parent decisions with `DENY > ASK > ALLOW`;
  ask once through the parent. Do not union allow-prefix rules or assume the
  lowest mode is equivalent to intersection.
- Reuse the owned workspace/environment and only already-admitted resources.
  No new project MCP connection, hooks, Python import or top-level discovery
  is added while fixing permissions. Shared setup parity is H.8, not H.0b.
- Tighten the child's declared mode/rules without refusing a usable child merely
  because its requested mode was wider. Refuse required unavailable capabilities
  before activation. Nested children keep the original ancestry ceiling.

**Acceptance:** ScriptModel drives `invoke_subagent(profile="harbor")` through
the real tool runner. A parent ASK rule on a concrete harmless test operation
asks once through its actual handler; approval and denial have distinct observed
effects. Do not assume every `smart` bash call asks (ordinary commands can allow).
CLI/mode readonly, denied path, denied tool and parent ASK versus child allow-prefix
remain enforced; the filesystem sentinel is unchanged on denial. Exercise a
grandchild and direct runner entry so schema restrictions are not the enforcer.
A sentinel MCP executable never starts simply because a child is constructed.
These tests fail on the pre-fix code for the intended reason; no paid model run.

### H.0c Visible memory truncation

**Files:** `garuda/agents/loader.py`, preflight/banner output, tests.

A project memory file over the cap adds `memory.truncated` with the file and
dropped size to the run preflight and `--json` events.

**Acceptance:** a 9,000-character `AGENTS.md` produces the diagnostic once; a
small file produces none.

### H.1 Schema v1, single loader and inheritance

**Files:** new `garuda/agents/spec.py` (schema and validation),
`garuda/agents/resolve.py` (locations, `extends`, legacy translation), loader
callers.

- `AgentSpec` is the validated version 1 object; `AgentProfile` becomes a
  projection used by existing code until callers move.
- Legacy profiles translate mechanically; version 1 refuses unknown keys,
  duplicate keys, unknown tools, skills and MCP names, and missing instruction
  files.
- `extends` with map merge, list replace, `tools.add/remove`, instruction
  append/replace and hook append; depth four; cycles refuse.
- Locations: project, user `~/.agent/agents/` (new), packaged as `garuda/<name>`.
- `garuda agent migrate PATH [--write]` previews and writes with a backup.
- Namespaces/paths, no-follow bounded source reads, safe YAML/front matter and
  inheritance provenance follow the design. All user roots derive from
  `global_settings_path().parent`; explicit source selection does not grant code.
- Pure resolution produces immutable `ResolvedAgent`/`PromptPlan` before any
  activation. Legacy translation preserves old implicit source selections;
  versioned memory/skill/subagent defaults are not silently opted into.

**Acceptance:**

- Every packaged profile translated to version 1 resolves to the same
  `AgentConfig`, tools, rules and prompt as before (golden comparison).
- Migrate-then-load equals load for every packaged and fixture profile;
  repeating `--write` is a no-op.
- Each refusal code has one test through the public loader.
- A project `build` still overrides the packaged one; `garuda/build` always
  means the packaged one.
- Qualified-source and self-cycle cases, traversal/symlink references, ambiguous
  body/front matter, future versions and duplicate keys refuse independently.
  Migration previews intentional safety changes; atomic no-clobber writes
  preserve unrelated user bytes and require confirmation for behavioral deltas.

### H.2 Resolved view

**Files:** new `garuda agent` CLI group, provenance tracking in the resolver,
CLI reference.

`list`, `show [--json]`, `prompt [--json]`, `check` and `new`. `show` reports the
source of every value (packaged, user, project, extended agent, CLI). `prompt`
prints static sections, sizes and digest. Runs record the actual outbound system
message digest including runtime blocks. Inspection never imports project code,
connects MCP, executes hooks/probes or calls models. Secrets are redacted by
default; only an explicit local `--raw` returns authorized source text.

**Acceptance:** for fixture definitions, `show --json` equals the configuration
`prepare_agent_run` uses, and `prompt --json` equals the system message the
first model request carries for matching inputs without dynamic state (captured
at the ScriptModel boundary). A runtime-state case records a distinct actual
digest rather than pretending offline inspection predicted it. Sentinel code/
servers never start under any inspection command. `new` output passes `check`,
loads and refuses to overwrite an existing file. Redacted JSON contains no secret.

### H.3 Independent project authority hardening

**Dependencies:** approved security-default decision, not H.1 or teams C.2.
**Files:** legacy shared setup, profile source provenance, MCP configuration,
focused local trust store and security guides.

- Enforce user-only `agents.project_ceiling`, default `smart`, for both legacy
  and versioned profiles across CLI/SDK/chat/serve/web; operator/readonly/parent
  ceilings remain stricter. Widening refuses before code/model activation.
- Authorize project MCP commands/URLs per repository and resolved entry/content
  hash, including referenced scripts, cwd and authorized environment/header
  specifications. Filter before stdio spawn or network access. Never inherit
  user trust/effects merely because a project shadows the server name.
- Use the same content-bound record for versioned project instruction overrides
  and executable hook/tool grants; labelled ordinary memory is not such a grant.
- Interactive approval creates an owner-only, locked/no-follow atomic record;
  changed bytes/entry/script/identity invalidate it. Headless runs skip optional
  untrusted servers with diagnostics; explicitly required servers refuse.
- Trust and activation consume the same immutable resolved bytes. No temporary
  `trust_project_mcp: true` global bypass. Later teams trust reuse preserves
  existing grant semantics with a verified migration.

**Compatibility:** release notes identify the project ceiling and MCP tightening.
A user may raise the ceiling but must still trust each code identity. No one-line
setting approves all future repository commands.

**Acceptance:** legacy project `yolo` is refused before any MCP launch/model call.
A real sentinel MCP process and local HTTP listener receive no launch/request
headlessly without trust; a permitted control works after the exact grant.
Changed entry/script, symlink replacement, same-name user/project shadow and
corrupt trust store cannot reuse a grant. Test readonly operator versus permissive
named profile/CLI selection. Approval never persists resolved secrets.

### H.4 Prompt assembly and memory sources

**Files:** new `garuda/agents/prompt.py`, memory file resolution, tests.

Fixed section order, labels and per-file and total caps from the design; user
memory `~/.agent/AGENTS.md`; `memory.project` lists with `first` or `all`; and
`context_pack`. Agent instructions are never cut.

**Acceptance:** golden assembled prompts for zero-config, user memory, `all`
mode and over-budget cases; the zero-config prompt is byte-identical to
the post-H.0 compatibility fixture when no new sources are opted in. H.3/H.7
warning differences are separate diagnostics, not prompt changes. An over-long
agent instruction or mandatory full-skill block refuses, including cases where
the character cap passes but the effective token budget does not.
Project files cannot escape roots through symlinks/paths; changed trusted source
bytes cannot be substituted after preview. Truncation reports bounded reads,
not invented exact dropped counts when the full content was not read.

### H.5 Skills sources and filters

**Files:** `garuda/skills/loader.py`, resolver, tests.

User `~/.agent/skills`; `from`, `include`, `exclude`, `load`; shadowing reported
by `show`; `skill.tool_not_granted` reported by `check`.

**Acceptance:** precedence and filter matrix through `agent show`; the advisory
wording stays in the prompt; an unknown `include` name refuses in version 1.

### H.6 Tools

**Files:** tool registry, `Tool` option schema, `garuda/tools/subagent.py`,
tests.

- `preset`, `add`, `remove` with refusal of unknown names.
- An optional option schema on `Tool`; first adopters bash (timeout, output
  cap) and `web_fetch`/`web_search` (allowed domains, labelled guardrail).
- `tools.mcp` selection by server name.
- `tools.subagents` enumerated in the tool schema and enforced in the runner.
- Enforce final tool removals even for explicit/custom tools and lazy MCP
  wrappers; check actual target identity/effect/arguments, not just `use_tool`.
- Bound nested depth, root launch count, remaining turns/deadline and cancellation
  using the design's finite defaults. Never reconstruct a wider child workspace
  or reset a root budget by recursively invoking another profile.

**Acceptance:** an unlisted subagent name refuses at the runner even if the
model sends it; a bash option timeout applies to a real subprocess; a blocked
domain returns a tool error.
Add nested/busy/exhausted/cancelled controls and removal of an explicitly supplied
tool. A lazy MCP write cannot bypass a parent's denied actual tool/effect; an
allowed wrapper control still works. No unselected server is connected merely
for discovery.

### H.7 Independent guard-hook hardening

**Dependencies:** approved guard-default decision; no H.1/teams dependency.
**Files:** `garuda/plugins/hooks.py`, final tool admission and existing hook tests.

Fail closed for legacy and versioned guards; only explicit user-authorized
advisory hooks may allow on failure. Preserve mandatory parent/operator guards.
Bound per-hook timeout to 300 seconds and total chain budget to the root deadline.
Reap the whole hook process group on timeout/cancellation, not just the shell.

Validate and reauthorize the final rewritten tool identity/arguments after hooks.
Unchanged approved calls need no duplicate prompt; changed calls need their own
decision. A blocked, malformed or unknown call cannot reach dispatch. Apply this
to sequential, parallel and lazy-MCP target paths. The rewrite gap is a separate
code-read finding and needs a reproducer before an implementation claim.

**Acceptance:** real subprocess hooks cover missing executable, nonzero exit,
timeout and surviving grandchild; programmatic exception blocks too. A hook that
rewrites an approved read into a denied write leaves the sentinel unchanged.
A permitted unchanged control executes once; explicit advisory failure warns.
The log contains code/source and redacted evidence, not arbitrary event secrets.

### H.8 SDK and entry points

**Files:** `garuda/sdk/`, `garuda/interfaces/` entry points, shared setup.

`SoftwareAgent(agent=...)` accepts a name, `AgentSpec` or dict; `AgentSpec.load`
and `.narrow`; `--agent-file`; `serve` and web accept names only.

**Acceptance:** CLI, SDK, `serve` and chat produce the same `show --json` and
prompt digest for one definition. `.narrow` cannot widen. An inline definition
over HTTP refuses.
Named HTTP selections obey the server's operator allowlist/ceiling, including
permissive packaged names; caller-supplied workspace/trust settings cannot grant
authority. Freeze each session definition: source changes on resume require
refusal or a new identified segment. Test concurrent sessions without shared
mutable specs and cancellation cleanup before releasing resources.

### H.9 Reviewed notes

**Dependencies:** H.4 and teams B.3. **Files:** `remember` tool, proposal store,
`garuda memory review`, redaction.

**Acceptance:** a proposal never changes a memory file until accepted; headless
runs cannot accept; a secret-shaped proposal is rejected; accepted notes appear
in the next run's prompt section 2 or 5.
Proposal id/digest/scope are validated; locked atomic acceptance is crash/replay
safe and cannot append twice, target a symlink or accept another project's note.
Managed publication is prevented without approval; host-shell immutability is
not claimed. Redaction is best-effort, with rejected content absent from logs.

### H.10 Teams bridge

**Dependencies:** H.8 (including H.12), teams C.1 and C.3; teams A.3 for `effort` and
instructions on ACP roles; teams G.1 for `tools.mcp` on ACP roles.

- Teams `roles.<name>.agent` for native roles.
- The ACP subset table from the design; other sections refuse with
  `agent.field_unsupported`.
- Native consulted roles combine the agent's model, instructions, memory and
  skills with the consult profile's tools and limits.
- Native and ACP projections are distinct. Do not expand native defaults and
  then reject every ACP role for implicit memory/hooks; reject explicit or
  inherited unsupported requests field by field. ACP model ids use the role,
  not native binding aliases. Conflicting model/effort selectors refuse.
- G.2 consult profiles deny hooks, MCP, network, tags and further delegation
  even when the selected agent grants them. Preserve user memory privacy and
  explicit project-instruction trust under the teams contracts.
- Incompatible required output/terminal contracts refuse consult admission;
  neither silently drops the agent requirement nor enables task checks inside
  the read-only question/answer child.

**Acceptance:** a native role runs with its agent's prompt digest; an ACP role
with `skills` refuses; a consulted native role cannot use a tool outside the
consult profile even if its agent grants it.
Exercise default ACP projection success, unsupported explicit/inherited fields,
model conflicts, fallback identities and unchanged readonly/Docker gates. The
native agent source digest becomes part of the execution identity.

### H.11 Dashboard

**Dependencies:** H.2, teams F.1 and F.4.

Setup view lists agents with source, resolved fields, prompt digest and section
sizes; conversations show recorded native execution identities and their actual
system-prompt digests. Historical unbound measurements remain unattributed. ACP
outbound attribution remains open under #173. Read-only.

Agent HTTP errors/warnings use source-free fixed messages and registry-validated
codes. Exercise malformed YAML/Markdown and legacy warnings through the
production Setup route, plus static-prompt and outer inspector failures. Local
inspection keeps detailed diagnostics. Section metadata and the existing table
include labeled UTF-8 bytes, Unicode chars and estimated tokens from the same
static inspector. Unicode unit/browser fixtures distinguish bytes from chars.
Segment attribution and the complete parent acceptance remain separate #173 work.

**Acceptance:** production-written fixtures render; live Chrome remains the
browser gate.

### H.12 Compilation and final-output schema

**H.12a dependencies:** H.1. **Files:** typed resolver-to-runtime compiler,
model/context/workspace/completion owners and public boundary cases.
Map and validate model/effort/output caps, context reserve/condenser/shaping,
turn/deadline limits, mode/declared-field precedence, completion flags and the
already-authorized environment. No schema field is accepted without a mapping
and observable case. Preserve legacy tmux/sandbox/remote entry points rather
than silently translating them to local. Required owner verification cannot be
disabled by a project or delegate; inspect without instantiating clients.

**H.12b dependencies:** H.12a/H.4/H.6. Pin the draft-2020-12 validator, bound
local schema size/reference expansion, disable remote/file-escape `$ref` and
custom validators, and enforce schema against actual terminal output before
acceptance. At most two repair attempts use the root's remaining budget. Failure
is `agent.output_invalid`, never a successful artifact or verification pass.

**Acceptance:** independent resolver/request/environment observations cover
every full-reference field and invalid cross-field budget. Schema examples cover
valid output, malformed/wrong-shape output, remote/escaping references,
unsupported features, resource exhaustion and repair-budget exhaustion. A valid
shape with failed task/check evidence still fails its ordinary completion gate.

## Documentation work by lane

- `use-cases/customize.md` (Level 3) is rewritten around `garuda agent new`,
  `extends` and `garuda agent prompt`.
- `guides/configuration.md` gains an "Agents" section and the authority table;
  the "Where settings live" table adds `~/.agent/agents/`, `~/.agent/skills/`,
  `~/.agent/AGENTS.md` and memory files.
- `guides/safety-and-workspaces.md` documents the project ceiling, MCP trust
  and the delegation rule.
- The CLI reference documents `garuda agent` and `garuda memory`.
- `ARCHITECTURE.md` and `MODULES.md` describe the resolver and prompt assembler.
- Record decisions 1–12 from the design in `.context/decisions.md` in the PR that
  makes each true.

## Risks

| Risk | Mitigation |
|---|---|
| H.3/H.7 tighten project permissions, code trust and guards | Explicit approval/release notes; user ceiling plus per-entry grants; fail-open only for explicitly advisory user hooks |
| Validation breaks existing profiles | Legacy unknown non-security keys warn; ambiguous keys/security values refuse explicitly |
| Translation changes behavior | Golden comparison of every packaged profile before and after |
| Prompt assembly changes cached prefixes or wording | Zero-config prompt is byte-identical to the base; fixed order |
| Project text gains authority through memory or skills | Labelled project sections; no permission, tool or execution grants from project text |
| Notes become a prompt-injection store | Digest/scope-bound acceptance, atomic replay-safe publication, bounded labelled data; redaction is best-effort |
| ACP users expect Garuda skills to apply | Refusal with `agent.field_unsupported` and a docs note |
| Delegation widens capabilities through MCP/setup or rule merging | Reuse admitted resources in H.0b; compose actual decisions; no blanket rejection of usable narrowed children |
| Inspection executes project code or leaks memory | Pure frozen resolver, bounded source reads and redacted output; raw local output is explicit |
| Schema field exists without enforcement | H.12 typed mappings and terminal validation with finite repair budgets |
| Parent/server ceiling bypassed by profile name or rewritten tool | Admission checks actual final identity/arguments against owner policies |

## Completion definition

Set H is complete when every entry point resolves agents through the shared
service, `agent show` and `agent prompt` match what runs use, each verified
defect has a regression test that fails on `3456f25`, H.3's refusals and
H.0b's delegation ceiling are tested at the service boundary, and the
documentation describes shipped behavior.
H.12 must own every accepted field and terminal schema check. The static
inspection view and actual request digest are distinct where runtime state
differs. All security defaults require recorded approval; safe deferral is not
completion, and no live/paid model test is implied by documentation review.
