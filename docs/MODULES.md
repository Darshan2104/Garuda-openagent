# Garuda — Module Map

What each package owns and the file to open first. Read
[ARCHITECTURE.md](ARCHITECTURE.md) for how these fit together at run time.

The historical build-order plan (39 modules across 8 phases, all complete) is in
[archive/2026-07-17-MODULES-build-phases.md](archive/2026-07-17-MODULES-build-phases.md).
It is provenance, not a map.

## `garuda/types.py`

The shared vocabulary: `Message`, `ToolCall`, `ToolResult`, `ExecResult`,
`AgentConfig`, `AgentResult`, and the default system prompt. Everything imports
from here; it imports from nothing in the package. Keep it dependency-free — a
module-level import in `types.py` becomes a cycle everywhere.

## `core/` — the loop and its gates

`DefaultAgent` used to be one ~500-line `run()` doing setup, turns, and completion.
It is now four collaborators; read `loop.py` for control flow, the others to change
a behaviour.

| File | Owns |
|---|---|
| `loop.py` | `DefaultAgent`: the turn loop, and nothing else. Model call → tool step → repeat. Re-exports the constants callers import from here. |
| `run_state.py` | `prepare_run` (assembly: tool filtering, buffer, context bootstrap, subagent/collection wiring, deadline) and `RunState` (what the loop reads and writes, plus result building). |
| `acceptance.py` | C.5 attributed verification: runs `user-config`, `trusted-project` and `user-request` checks after a session, writes a receipt each (authority, command, code fingerprint, exit code, bounded redacted output), voids a check that changes the tree, withholds a pass when the session changed that check's own test infrastructure, and derives verification; agent-suggested checks only ever set the self-check. |
| `steering.py` | Every message the harness injects between turns: budget notices, the budget-review and final-turn nudges, repetition and failure-streak detection. Notes are *queued*, never appended mid-turn — see its docstring for why. |
| `tool_runner.py` | Executing one call or a concurrent read batch: permissions, hooks, output shaping/buffering, event ordering. `PARALLEL_SAFE_TOOLS` lives here and the fan-out is bounded by `AgentConfig.max_parallel_reads`. |
| `metrics.py` | Per-turn model latency, tool latency and wall-clock, compaction and checkpoint time, cache-hit rate. Rolls up onto `AgentResult.metadata["metrics"]` and emits one `turn_metrics` event per turn. |
| `completion.py` | The `task_complete` gate: acceptance contract, side-effect sweep, verification, and the yield-breaker that stops it livelocking. |
| `termination.py` | The run-scoped terminal-tool strategy boundary, plus the adapter that preserves the default `task_complete` gate. |
| `collection.py` | Structured collection requests/reports, the child coordinator and completion gate, sibling trace linkage, and the collection-worker tool ceiling: trusted effect provenance plus profile/request/network/permission intersection. These are guardrails, not confinement. |
| `modes.py` | Run postures. The presets that map one `mode` onto a coherent gate set. |
| `rigorous.py` | `RigorousAgent`: plan → execute → critic, with repair rounds. `create_agent()` picks between this and `DefaultAgent`. |
| `verifier.py` | The completion gate. Decides whether `task_complete` is accepted. |
| `evidence.py` | Two independent classifications of a shell command: whether it can actually fail (a check that cannot fail is not proof), and whether it writes anything (`is_side_effect_free`, which gates concurrency at the gate). Close to opposites in practice — the commands that prove the most are the ones that write. |
| `contract.py` | Acceptance criteria derived from the task statement, pinned across compaction. |
| `side_effects.py` | Sweeps agent-started background processes before verification. Tracks each launch by its process group — the command text is a fallback, since a process can rename itself — and re-probes after every signal so absence is confirmed rather than assumed. |
| `permissions.py` | `PermissionEngine`: allow/deny/ask per tool, path, and command. Guardrails, not confinement. |
| `sessions.py` | On-disk session store; `meta.json` writes are locked and atomic. |
| `events.py` | Append-only JSONL event log, crash-safe. |
| `buffer.py` | Session buffers holding large tool output and compacted history. |
| `bootstrap.py` | One-shot environment probe folded into the first-turn prompt. |
| `subagent.py` | Forked child runs, capped by the parent's effective permissions (`DelegatedPermissionEngine`) and admitted tools. |
| `action_memo.py` | Session memory of what has already been asked, so a repeated read is answered rather than re-run. Filesystem reads stop being memoized while a background task is live — it writes between calls, and no call marks that. `bash_background` reports its own exits so caching resumes; a raw `cmd &` reports nothing, so it suspends caching for the session. |

## `tools/` — what the agent can do

`protocol.py` defines the contract (`ToolContext` in, `ToolResult` out) and the
conservative `ToolEffect` vocabulary. Missing declarations resolve to `UNKNOWN`;
collection policy trusts declarations only from actual built-ins or an exact MCP
tool authorized by the user-owned global config. Project tools cannot self-bless.
`registry.py` + `__init__.py::build_toolkit` assemble the set a profile asks for.

Execution: `bash.py` (a command that backgrounds something also records the
process group it leaves behind, so the pre-completion sweep has an exact handle
on it), `background.py`, `tmux.py`.
Files: `files.py`, `edit.py`, `multi_edit.py`, `search.py` (ripgrep-backed),
`diagnostics.py` (post-edit syntax + lint).
Reading: `documents.py`, `image_read.py`.
Network: `web.py` — SSRF guard, redirect re-validation, and pinned-IP connect.
Agent-facing state: `todo.py`, `goal.py`, `contract.py`, `task_complete.py`.
Collection: `collection.py` — trusted parent `delegate_collection` and internal
child-only `submit_collection`; neither is registered as an ordinary built-in.
Large output: `buffer_tools.py`. Discovery: `discovery.py` (token-lean MCP),
`project_loader.py` (opt-in `.agent/tools/*.py`). Delegation: `subagent.py`.

## `workspace/` — where commands run

`protocol.py` is the `Environment` interface every tool talks to. Implementations:
`local.py`, `docker.py`, `remote.py`, `tmux.py` (persistent panes with marker
polling), selected by `factory.py`.

`sandbox.py` / `sandbox_policy.py` build the OS sandbox (bubblewrap on Linux,
Seatbelt on macOS) — read the `sandbox_policy.py` docstring before touching it, it
records which confinement actually holds. `shell.py` is the opt-in persistent
shell; `paths.py` and `health.py` are path safety and liveness. `lease.py`
issues mutating-workspace leases (one live mutating owner, read-only sharing,
expired holders retained after parent death without descendant-cleanup receipts,
read-only registration preserving prior bindings, issuing-owner release removing
ordinary holders, pid/start identity/process group/epoch via `runtime/ownership.py`,
creator identity proved before publication, existing-session reacquisition refused,
heartbeat/release bound to the issuing instance/process and full retained owner,
workspace and mode (copied epochs and forked/ambiguous handles refuse),
`validate_issued` checking existing authority under the lock without renewal
for delegation/borrowed admission,
recovery-facing inspection retaining expired/dead-parent ownership until explicit
removal (runtime and project-key recovery refuse before changes),
corrupt/symlinked/future-version leases fail
closed, user files never touched; storage through `runtime/strict_store.py`:
owner-only, no-follow lock, atomic fsynced writes) plus worktree isolation
keys. `confined_acp.py` (C.8a) — read-only ACP roles run only in Docker: a preflight proves the source and `.git` are unwritable, scratch is writable, the user is unprivileged and no socket, home or credential store is mounted; the adapter then runs in the user's image with the workspace mounted read-only at `/workspace`. No proof means `workspace.readonly_unenforced`, never a host substitute. `no_edits.py` (C.10) — the no-edits guardrail: a bounded, no-follow manifest of the workspace (ignored files, modes, symlinks, change times) and of the repository's refs, HEAD, hooks, config and staged entries by content, taken before a run and compared after every descendant exits; any difference or an incomplete manifest withholds outputs. `worktrees.py` (B.5) decides where a session edits — `shared`, its own
linked worktree on `garuda/<session>` (create-only branch, sanitized Git,
source HEAD and dirty fingerprint recorded, uncommitted source edits not
carried over) or `auto` — and `run_agent_task` calls it before the lease so the
lease, baseline and delta bind to that directory; a refused launch discards
the new worktree and a resumed session returns to its own. `merge_session`
(`garuda sessions merge`) locks integration per repository, snapshots the
worktree, previews with `merge-tree`, writes the merged tree to a scratch
directory, runs each required check in Docker against it read-only (no check
or no Docker refuses; a tree change voids the evidence), revalidates the
destination and publishes only `refs/garuda/integration/<id>` by
compare-and-swap. A worktree is a separate place to edit, not confinement. `runtime/capacity.py` holds one finite slot pool per
runtime (`native`, `claude`, `codex`, …) shared by every launch through
`WorkspaceLeaseGuard`; ceilings come from `capacity:` in the global settings,
and a key without one is not limited. Reusing a capacity holder id refuses
live/unknown ownership replacement; exact-owner retries are idempotent, and
release validates the full owner identity and epoch. `run_agent_task` acquires the mutating lease for the workspace
before resolving the environment, heartbeats for the whole run, and releases
last (also on cancellation) — concurrent `run_agent_task` runs on one workspace
are refused, never interleaved. The interactive paths that call `agent.run`
directly — CLI chat, dashboard chat, SDK `Conversation` and recipes — hold the
same `WorkspaceLeaseGuard` for their whole life (read-only for a `readonly`
posture), race each turn against its heartbeat, and release last on close.
`diff.py` holds the git mechanics of the authoritative delta: baseline
commit/status/fingerprints, per-file added/modified/deleted/renamed/untracked
with preexisting dirt flagged separately, bounded diff text recoverable from
disk, and ACP hints reconciled against filesystem truth and never applied. It
reads `git status --porcelain=v1 -z` and `git diff --name-status -z --relative`
scoped to the workspace (so quoted, non-ASCII, and space-padded names and
repo-subdirectory workspaces map to real files), hashes symlinks by link text,
never opens FIFOs/devices, and raises on any git failure for a repository
baseline rather than reporting an empty delta. A non-repo workspace records an
explicit `unsupported_nonrepo` baseline.
`evidence.py` is the shared session boundary over it. `host_backed()` is the one
workspace-kind classification: `local`, `sandbox`, `tmux`, and `docker` (host
workspace bind-mounted) are attributable; `remote` is recorded
`unsupported_nonlocal`; unknown kinds fail closed. `begin_session_evidence()`
persists the baseline before an environment is resolved or a prompt is sent and
returns the loader the verifier consumes; `finish_session_evidence()` persists
the final delta. Entry points that persist a session use it — `run_agent_task`
(CLI `run`, `serve`, SDK `SoftwareAgent`), interactive `garuda chat`, and
dashboard chat — and refuse to start (session marked failed) when the baseline
cannot be recorded; a completion whose recorded baseline or delta cannot be read
is rejected by the verifier, and a finish/close that cannot persist it marks the
session failed. The verifier's verdict depends on the delta being readable, not
on its contents: `task_complete` carries no file-change claim to check it
against, so the delta is attached as `workspace_delta` evidence on the
verification event. Unsupported attribution is reported as such, never as an
empty "nothing changed" delta. SDK `Conversation` and `garuda recipe run`
persist no session and run without a baseline (see `BACKLOG.md`). A resumed run
is a new session that inherits the resumed session's recorded baseline
(`inherit_from=`, recorded as `baseline_inherited_from`) only when the workspace
is exactly as that session left it: `finish_session_evidence` records the end
state (`final_workspace_state`: HEAD, status, dirty-file fingerprints), and the
resume's fresh capture must equal it at the same resolved path
(`baseline_workspace`). A pull, a teammate's commit, or a human edit between
sessions, a session Garuda never saw end (SIGKILL), or a dirty submodule/nested
repository (fingerprinted by kind only) falls back to a fresh capture with
`baseline_inherit_refused`, so work nobody can attribute to the session is never
credited to it. A cancelled or failed run records its end state after teardown
(`record_end_state`), since it is the run most often resumed. Known limits,
both toward refusing or narrow: fingerprints hash content, not mode, so a
`chmod` between sessions on an already-dirty file goes unnoticed; a
session-end hook that writes into the workspace makes every resume refuse. `runtime/handoff.execute_handoff(workspace=...)` carries the delta
into a handoff; `garuda runtime handoff --confirm` passes its workspace. The
Generated pack files live in the session store, so they never appear in the
workspace delta.

`snapshot_proto.py` is the A.5 spike (#154); `worktrees.py` builds on it. It
snapshots a work tree (tracked edits, deletions, untracked non-ignored files)
with sanitized Git: no inherited `GIT_*`, no system/global config, fsmonitor
and hooks off, files read without following symlinks and hashed with
`hash-object --no-filters` into a temporary index — never `git add`. The
manifest is compared before and after; submodules, sparse checkouts, custom
filters and symlinked parents refuse. `detached_repository` writes the
snapshot into a fresh repository with its own metadata (for consults);
`allocate_branch` has one winner; `preview_integration` uses `merge-tree
--write-tree`; `publish_integration` moves only `refs/garuda/integration/<id>`
by compare-and-swap; `run_confined_check` runs a check in Docker against the
read-only detached copy.

## `context/` — fitting the conversation in the window

`manager.py` holds history and decides when to act. `shaper.py` caps and shapes
tool output, `condenser.py` compacts (`microcompact` by default) and demotes
pruned history into buffers, `summarizer.py` produces the summaries.
`schemas.py` validates the generated `current-task.md`/`handoff.md` files
(versioned frontmatter, bounded fields, unknown fields round-trip, unknown
versions rejected). `pack.py` is the single writer: it compiles both files
deterministically from the state card, session record, and git evidence
(byte-identical recompilation, budgeted briefs that keep provenance), writes
atomically, and refuses any other target. The pack lives in the session's own
directory in the session store, never the workspace (B.3). `RunState` syncs the pack at the
checkpoint boundary and re-syncs after every compaction; resume restores the
persisted working state first, so compaction and restart preserve pack facts.
`redact.py` validates packs (size, workspace-relative paths, no-reasoning
markers) and best-effort redacts secret patterns (full PEM blocks, tokens,
credential assignments) recursively over every string and key; the writer
scrubs automatically, rebuilds from the scrubbed map, and blocks unsafe
handoffs, while durable repository files are unreachable by construction
(strings in, never paths).

`brief.py` (B.7) builds a bounded, redacted brief of one session — task,
state, changed files, checks (fingerprinted against the tree they ran on and
rendered stale when the workspace differs) and the end of its final output —
and renders briefs in one escaped, line-quoted, data-labelled envelope inside
one shared budget, reporting what it trimmed. `tags.py` resolves `--with`,
`--with-id` and `@name` tags (names only in the current project; another
project only by full id with the user's grant), records the link on both
sessions and writes an immutable, text-free receipt for a cross-project grant.

## `agents/` — profiles

`fallbacks.py` (C.9) walks a role's fallback chain once before start, skipping a candidate only for `harness.cli_missing` or a freshly checked `harness.logged_out` (never an unknown login or limit), records primary, taken entry and reasons on the role plan, and refuses with every reason when none can start.

`spec.py` and `resolve.py` (H.1) are the version 1 agent definition and its one resolver: a field table that drives validation, the legacy translation and the `AgentProfile` projection; locations (project, user, packaged; `garuda/`, `user/`, `project/` qualified); bounded no-follow reads contained in each location; `extends` up to depth four with cycle refusal; key-by-key merge, appended or replaced instructions and `tools.add`/`remove`; provenance for every value; and activation that refuses recognized-but-unsupported fields. `compile.py` (H.12a) gives every accepted field its runtime owner and checks budgets (output reserve and margin inside the context window, summarize threshold below it, tool-output bounds), refuses a project switching off a required gate, and narrows Docker limits only downwards. `skills/sources.py` (H.5) discovers skills from project, user (`<global home>/skills`, new) and packaged sources with project-first precedence and recorded shadowing, filters by `from`/`include`/`exclude`, reads each file bounded and no-follow inside its source, and reports advisory `allowed-tools` gaps; legacy profiles keep their project-only sources. `prompt.py` (H.4) assembles the static system prompt as an immutable `PromptPlan` of labelled sections in a fixed order (instructions, user memory, skills, project memory, context pack), with per-file and total character caps and the token budget, trimming only optional memory at paragraph boundaries and refusing oversized mandatory sections for version 1; memory files are read inside the workspace without escaping. Tool settings (H.6) are `tools.preset`, `options` and `subagents`: presets come from each tool's declared effect, `options_schema` on a tool validates its options, `ToolContext.option`/`removed_tools` carry them into execution (including behind `use_tool`), and `core/subagent.py` enforces the allowlist, depth, shared launch budget, single child, and the parent's turn and deadline limits. `output_schema.py` (H.12b) loads, bounds and compiles an agent's final-output JSON Schema at resolution; `CompletionGate` validates the `task_complete` `result` first, allows two repair turns, and fails with `agent.output_invalid` (`TerminalDecision.failure`), while `AgentResult.output` holds only a validated result. `context/notes.py` (H.9) holds reviewed-note proposals (locked, journaled, digest-bound, symlink- and project-checked; accepted notes become labelled prompt sections), `tools/remember.py` is the propose-only tool, and `interfaces/memory_cli.py` is the terminal-only review. `spec_api.py` and `selection.py` (H.8) add `AgentSpec` (a frozen, digest-identified result of pure resolution that hands out copies; `narrow` only tightens) and the operator checks for named selections over `serve`/web; `setup.resolve_profile` is the one pure resolution step before `prepare_agent_run` activates (and closes on cancellation what it started), shared by every entry point and by delegates. `inspect.py` (H.2) backs `garuda agent list/show/prompt/check/new`: pure inspection with every value's source, the static prompt by section (`loader.system_prompt_sections`, joined exactly as a run joins it) and its digest, redaction unless `--raw`, and diagnostics from the shared registry; `setup.static_agent_config` is the one config builder `prepare_agent_run` and `show` share. `load_profile` and `md_loader.py` are thin adapters over it; `migrate.py` backs `garuda agent migrate`. `setup.py::prepare_agent_run` is the shared chokepoint for every entry point.
`loader.py` reads YAML profiles (and records which fields were declared, so mode
presets don't override authored intent); `md_loader.py` + `frontmatter.py` read
OpenCode-style `agent.md`. Built-ins in `defaults/`: `build`, `plan`, `explore`,
`harbor`, `reviewer`.

## `model/` — provider access

`protocol.py` is the `Model` interface. `litellm_model.py` is the real
implementation (streaming, tool calls, reasoning effort, prompt caching, retries);
`governor.py` caps per-provider concurrency; `script_model.py` is the deterministic
test double — prefer it over mocks. `transports.py` is the admission registry
for direct transports: citation, capabilities, double, opt-in test, cost
semantics, and migration notes required; private endpoints never admissible.
Import-time admission checks only the shipped record; that the named integration
test exists is a repository check (`assert_repository_admission`, run in CI), so
an installed wheel without `tests/` still imports.
`config.py` owns role specs/bindings, provenance, compat translation, and
collection-narrowing; `factory.py` builds one client per role per run. The
reasoning client owns the parent loop. An enabled collection client runs only
after `delegate_collection`, with its own `ContextManager`, limits, terminal
contract, and sibling event log; ordinary `invoke_subagent` stays on reasoning.
`agents/setup.py::prepare_agent_run` returns `PreparedNativeRun` and is the
single resolution point for every entry point.

## `runtime/` — harness boundary

`protocol.py` is the `AgentRuntime` interface every harness implements (native or
external — never place a harness behind `Model`). `events.py` is the normalized
event vocabulary with session/turn correlation. `fake.py` is the deterministic
test double with scripted scenarios; every adapter must pass the shared suite in
`tests/test_runtime_conformance.py`. `registry.py` resolves trusted global
harness manifests (the only place that authorizes an executable) plus
project-level aliases that reference and narrow but never self-authorize; one
instance per job, never process-global. `session_state.py` records a session's
process, work, outcome and verification as four separate fields (a gate pass is
a self-check; `crashed` is derived from the owner's liveness, never stored), with
a mapping table for legacy `status` values. `roles.py` (C.3) turns a `garuda.yaml` role into one exact plan (runtime, model id, effort, permissions, profile, config digest, provenance) and maps ACP model/effort to `session/set_config_option` ids only for adapter identities proven in `PROVEN_OPTIONS`; the adapter checks each value against the agent's `session/new` options and its reply before the first prompt. `resume.py` (B.7) decides how
`--resume` continues — native transcript, the ACP agent's own `session/load`
(only for adapter versions in `PROVEN_LOAD`), or a new linked session started
from a brief — and refuses a live owner. `session.py` is the unified session
schema (runtime segments, baseline, handoff, cursors) plus legacy migration;
`SessionStore` publishes through the atomic locked meta path, so a failed
migration or write leaves the original readable. `native.py` adapts the native
loop behind the boundary without changing task semantics — imported directly,
never re-exported, so the product boundary pulls no `core` imports into
`garuda.runtime` itself. `handoff.py` is the switch transaction: boundary-only
pause, checkpoint, capture, generate, target start, acknowledged ownership
transfer, with rollback and cancellation returning to one resumable owner and a
typed event per move. `execute_handoff` runs the full flow against real
runtimes with the session store recording prepared/acknowledged/failed, so
success transfers single ownership and target failure keeps the source
promptable. Acknowledgement is one locked write (handoff `acknowledged` plus the
target appended as the active segment); a target with `bind_session` then
binds its child record, the source closes, and only then does an optional `deliver`
send the package as the target's first prompt. A delivery failure is a target
failure (`HandoffDeliveryError`, `target_state: failed`), never a rollback over
the target's changes; `record_target_outcome` records how the owner ended. `recovery.py` classifies restarts from persisted records
(resumable, rolled-back, ambiguous, external — an ACP-owned session the native
loop must not resume; `require_native_resumable` is the check every native resume
path applies; `reclaim_native` is the explicit way back once the target is
recorded stopped), reaps orphan agent children with
verification, appends cancellations at turn/switch/process boundaries, and
never invents success — a bare exit proves nothing. It runs on resume only
(`run_agent_task --resume`, after this run's workspace lease is taken, and
`NativeGarudaRuntime.resume`); a fresh run has nothing to recover. A child is
a signal candidate only when `record_child` persisted it against a known
runtime/session (or `record_prepared_child` persisted it `prepared` for the
prepared handoff's `target_runtime`, kept in the append-only `prepared_targets`) as its own process-group leader together with two process
identities (boot id plus start time from `/proc/<pid>/stat` on Linux; `ps -o
lstart= -o ucomm=` elsewhere — never a name the process can rewrite, such as
Node's `process.title`): the child's and its owning Garuda process's.
Recovery refuses while a retained workspace lease names the session (including
expired/dead-parent ownership without cleanup receipts), or the
recorded owner is still alive, audits the trail and classifies before any
signal, then SIGKILLs the group only if the pid's current identity still
matches and polls (bounded, `REAP_TIMEOUT_SEC`) until it is dead or a zombie.
Gone children are retired `exited`, recycled PIDs `reused` (never signalled),
killed ones `reaped`. Indeterminate liveness, identity, or owner probes,
missing checkpoints, native identity mismatch, or invalid ACP authority
snapshots refuse the resume. This is a guardrail, not a sandbox: descendants
that left the child's process group are not found, and a crash between
`AcpProcess.close` and the retiring write leaves a `live` record that only the
identity check keeps from misfiring. Cancellation audits are best-effort
before the cancel and surfaced afterwards as `CancellationAuditError` (a
handoff cancel cleans up first and ends FAILED when its audit write fails), so
a store outage never keeps work running. The `cancellations` list is audit
evidence that classification does not consume yet. A handoff target is
recorded from launch: `execute_handoff` names it on the `prepared` record and
hands it a `PreparedChildRecorder` (`AcpRuntime.record_launch_with`), which
writes a `prepared` child right after `launch()`, refreshes its identity after
the ACP handshake (a launcher's exec changes the command half), retires it on a
failed start, and is promoted in place to `live` by `bind_session`; a crash
before the refresh leaves a mismatched identity that recovery never signals.
Only `AcpRuntime(store=…)` (or a launch recorder, then `bind_session`) and
`HandoffTransaction(store=…)` record children and switch cancels; `garuda run
--runtime <acp>` and `garuda runtime handoff --confirm` are the production
callers. Without a store or a launch recorder the adapter logs a warning and records nothing.
 `router.py` ranks runtimes deterministically from capability, budget,
 availability, and quality history with logged rationale; unknown costs never
 read free, pins win or fail loudly, and switching needs confirmation at a
 boundary. `selection/`
is the P1 initial-only selector (issue #77): explicit → profile pin → first
trusted deterministic rule → optional classifier (#80) → configured
default → built-in native, with bounded side-effect-free trait detection,
trusted-only task regexes, pre-start validation, persisted rationale, and
startup fallback only on an unchanged baseline. `selection/classifier.py`
makes at most one tool-free, single-attempt call on the collection binding
(the reasoning binding only when trusted global config allows it) over an
approved candidate table fixed before the call, then revalidates the strict
JSON answer; malformed, unknown, low-confidence, incapable, timed-out, or
failed answers use the configured default. The record keeps binding role,
model, input digest, sanitized output, latency, usage, and cost (unknown
stays `null`). The CLI passes one trusted
catalog through selection and launch, so the selected id chooses the executor
rather than being audit-only; an ACP start fallback retains the same session
and can transfer only to native. It shares no type names
with `router.py` (P2 #49) and exposes no handoff API.

`approval_channel.write_answer(expect_digest=...)` plus the dashboard's `/api/inbox` and `POST /api/sessions/<id>/approvals/<approval>` (D.5) let a person answer any session's parked approval through the broker's file channel, bound to the request digest they saw, with one winner and no direct runtime answer. `web/static/views_sessions.js` (D.4) renders that model as the Sessions and flow views. `core/setup_view.py` (F.4) is the Setup view's read-only model: configuration, harness (cached login only) and state diagnostics with copyable fixes, role table, flow list with packaged examples marked and missing roles named, and value provenance. `core/usage_stats.py` (F.3) aggregates ledger records over rolling UTC windows without ever mixing units (native calls, ACP turns and left-out snapshots; unknown cost kept apart from known) and writes the fixed-schema CSV/JSON export; it is a report and no routing input. `web/providers.py` (F.2) builds the provider and limit cards from discovery, the cached login conclusions, the limit snapshots and the ledger (known/stale/unknown limits with source and time, active/expired/historical events, version and account flags, observed use through Garuda only) and the refresh that re-runs the documented status reads. `core/conversation.py` (F.1) is the conversation read model behind the run page's Models used panel: ledger rows by work type × harness × model (flow step roles as work types, a labelled session-metrics fallback for pre-ledger sessions), the selected model apart from the reported one, snapshots apart from billable usage, ACP turns apart from calls, lanes, and tag/resume links in both directions. `observability/limits.py` and `acp/limit_probe.py` (E.2) record account-bound limit observations from proved status sources (Codex app-server reads, no prompt) — snapshots replaced atomically, one deduplicated `limit_event`, a local salted account digest — and `LimitStore.decide` says whether an exhaustion may trigger fallback (fresh, same account and version, future reset); nothing else may. `observability/ledger.py` (E.1) is the idempotent usage ledger (monthly JSONL under the store lock, keyed records, schema-only fields, cursors for cumulative sources, retention); `observability/usage.py` feeds it from native event stores (and reconciles after a crash, and records `fallback_start`), `observability/acp_usage.py` turns adapter usage reports into snapshots or proved deltas, and `model/accounting.py` records the auxiliary model calls (summarizer, buffer_query, contract, verifier, critic) the loop does not make itself. `core/read_model.py` (D.3) is the one read model of sessions, queue entries, pending approvals and flows that `garuda sessions --json` and the dashboard's `/api/sessions*` both render (pure reads; unreadable records become visible `unknown`; usage and cost stay `unknown` until Set E); `web/tail.tail_records` and `routes.sse_frames` stream a session's log with byte-offset event ids so a reconnect resumes exactly. `bg_sessions.py` (D.2) is `run --bg` and the hidden `__worker`: canonical runtime admission uses the shared trusted catalog while retaining the original reference for downstream capability resolution; shared setup also resolves effective roles and records their original harness reference, preserving default-role permissions; the worker checks the queued lane/session against launch arguments, effective garuda.yaml configuration and receipt before identity publication or selection. Dynamic role fallback refuses in background mode until a selected runtime decision can be persisted. This check does not snapshot referenced runtime/agent/MCP files or prevent later concurrent configuration edits. The launcher writes the session (state `queued`), an owner-only launch file and the queue entry, then starts a detached worker that records its pid, start identity and command first, waits for its turn without a workspace lease, runs the ordinary `run_task` path with the preassigned session id, and releases its claim on ordinary owner-controlled exits; activated worker death leaves the slot quarantined; `cancel` removes a queued entry or SIGTERMs the worker's group only after its recorded identity matches the live process; `worker.log` is bounded. `queue.py` (D.1, promoted from the A.4 spike #153) is the durable FIFO session queue, bound to user, harness, session, frozen configuration and full worker identity. Queue version 3 delegates cross-store intent/selection/release to `queue_journal.py`; `queue_slots.py` fences protected reservations in shared capacity version 2. Intent precedes reservation, committed selection precedes process-local adoption, activation precedes the runner, and a durable capacity fence blocks captured tickets before claim removal and slot release. The fence retains activation history. Direct dispatch requires the full unrevoked adopted ticket; ordinary activation rechecks adoption under its file lock and after publication before returning launch authority, so failed release intent cannot resurrect retained handles or captured tickets. Ordinary capacity callers retain queue slots even after owner death. Ordinary reservations also remain counted after parent death, since their records lack descendant cleanup receipts; ordinary and queue admission both refuse automatic reclaim, and matched dead/unknown ordinary owner retries cannot activate. Recovery never launches: confirmed-dead pre-activation work reconciles; activated or ambiguous dispatch stays quarantined pending descendant cleanup evidence. Heartbeat/release/requeue match the complete owner; implicit authority stays in the claiming instance/process. Exact enqueue retries retain admitted bindings and FIFO; pending transactions cannot be rebound. Enqueue and ticket creation both require dispatch-ready scope labels to match their declared user/harness pair, preventing alternate FIFO lanes and refusing preexisting mismatches before selection. Workspace refusal restores original durable sequence regardless of callback order. Construction, `entries()` and `snapshot()` are pure reads; pending transactions are visible without repair. `queue_legacy.py` projects old records for diagnosis, archives exact private source bytes before safe migration, refuses version 1 work or version 2 claims without binding/activation evidence, and admits only empty records or fully bound ordered version 2 waiters. Nonempty capacity version 1 records refuse mutation without reservation-origin evidence; empty ones archive before upgrading. `strict_store.py` owns descriptor-bound publication and private backup validation. Full launch supervision and cleanup receipts remain D.2 requirements.

## `interfaces/` — entry points

`main.py` (CLI argument surface), `headless.py` (`garuda run`), `cli.py` + `tui.py`
(interactive chat), `runtime_cli.py` (`garuda runtime list|inspect|handoff|recover`
plus the `run --runtime <acp>` launch path), `run_guard.py` (run invariants shared
by native runs, ACP runs, and CLI handoffs: the workspace lease with heartbeat and
race, complete issuing-parent validation before delegation and borrowed
capability creation/acquisition/start/race, and the P0.17 broker approval path), `server.py` + `jobs.py` (job-queue server: submit/status/
events/result/cancel), `session.py` (multi-turn state shared by CLI and SDK),
`runner.py` (assembles a run and owns workspace teardown), `onboarding.py` (C.4: `garuda doctor` — config with provenance, only the harnesses roles use, executable/version/login/capabilities, leases, worktrees — `garuda init` / `init --project` proposals that write only after confirmation, `config show`), `session_service.py`
(B.6: the one session lifecycle every native entry point follows — workspace,
capacity then lease, session record and baseline, approval broker, environment,
then reap, teardown, persist and release last; `garuda chat`, dashboard chats and
the SDK `Conversation` open through `open_session`, and `run_agent_task` uses the
same workspace, lease and quarantine steps around its resume recovery. Background
processes that cannot be proven dead quarantine the session: the lease stays
held with its heartbeat stopped, runtime capacity remains reserved, and the pids
are recorded. Quarantine is sticky across subsequent release/finalizer calls;
a borrowed step propagates it to its parent and retains its own reservation,
including cleanup after revocation. Ordinary reaped completion releases as
before; supervised recovery receipts remain separate work).

## `flows/` — sequential role steps (C.6a)

`engine.py` runs a `garuda.yaml` flow in one workspace: the parent flow session
holds the workspace lease from the first step to the last and lends each step
a revocable `LeaseCapability` (`interfaces/run_guard.py`), so nothing outside
the flow takes the workspace between steps and no step keeps it. Per step
attempt it resolves typed inputs, journals the intent (fsynced) before launch,
runs the step as its own session, applies the no-edits guardrail, takes
declared outputs only from the structured-output envelope, and writes an
immutable receipt. An intent without a receipt is quarantined by `recover`,
never replayed; `resume` continues past receipted steps only. `artifacts.py`
stores bounded artifacts once (owner-only, no symlinks) and refuses forged,
escaping, symlinked or stale inputs. `review.py` (C.7) parses the bounded review block (`verdict:` plus `- [severity]` findings; invalid output stops the flow, a blocker or major finding requests changes), drives the retry loop — only the reviewed step reruns with the findings, `max_rounds: N` meaning at most N+1 pairs — and enforces independence: the reviewer's actual runtime and model may not match the reviewed role's, its fallbacks' or its consults'. Results read `review_approved` / `review_changes_requested`, never verification. A `parallel` group (C.8b) runs every member at once on one detached A.5 snapshot repository, never the workspace: native members run `readonly`, external ones must be `readonly` roles whose Docker confinement is preflighted before any member launches, and the source and snapshot must both be unchanged afterwards. `packaged.py` (C.6b) ships `plan-only`, `pair` and `plan-build-review` (scout, planner and reviewer steps `no-edits`), lets a same-name configured flow replace one, and lists the roles a flow needs when some are missing. `launch.py` starts native steps through
`run_agent_task` and ACP steps through `run_acp_task`, each borrowing the flow's
lease.

## `eval/` — measurement, outside the agent

`harbor_adapter.py` (Harbor benchmark integration; pins `mode="eval"`),
`harbor_environment.py`, `ablation.py` (per-variant config matrix),
`atif_export.py`, `dashboard.py`, `costs.py` (four-tier cost resolution),
`pricing.py` (the versioned price snapshot `costs.py` resolves against, so a
reported cost does not move when an upstream table does).
`contract_matrix.py` (P1.10: every adapter against the common scenarios from
declared capabilities — scenario coverage derives from manifest/runtime
capabilities plus harness behaviors, vendor rows run stand-ins and are
labeled simulated, support needs zero failures plus a PASS; per-adapter JSON
reports; the shared lifecycle itself lives in `garuda/runtime/conformance.py`).
`live_harness.py` (P1.11: opt-in single-prompt smoke runs against installed
harnesses with exact reporting, fixture workspaces, fake-agent script path
so linked checkouts work; CI never sets the gate variable).
`harness_matrix.py` (P1.12: model × harness comparison fed by real eval runs,
unknown costs/completions kept unknown, true medians, validated ingestion,
hashed prompts, per-cell trial counts, and handoff rates).
`dual_model.py` (P1 #81: paired model schema and release gates; committed
fixtures are synthetic contract examples, never evidence that a live release
gate has passed. `paired_result_from_agent_result` and `save_paired_results`
turn native event trails into reproducible, metadata-bound paired-trial reports;
only collected paired trajectories may support a rollout claim).
`paired_report.py` (the read-only `garuda eval dual-model report` service:
loads terminal native sessions only, requires the pinned task mix and
provenance, emits aggregate trial records without raw events, and refuses
incomplete pairs or an accidental report overwrite. Its input validation and
session-to-record projection deliberately share this one 338-line trust
boundary, rather than allowing a second entry point to bypass the refusals).

## Everything else

| Package | Owns |
|---|---|
| `diagnostics.py` | One `Diagnostic(code, message, fix)` type and the registry of stable codes; every code has a fix template, and tests match codes, never text. |
| `consult/` | `service.py` (G.2) — the consult engine for native askers: authorize (grants from the user file, no nesting, ancestry), atomically reserve a durable request id bound to the payload digest (`state.py`), resolve the target role, reserve capacity without waiting, quiesce the asker, capture an independent detached snapshot repository, check isolation, run a read-only child (`garuda/consult` profile; Docker-confined ACP only), reap before release (quarantine otherwise), validate with `NoEditsGuard`, persist a text-free receipt, then release. `envelope.py` labels the answer as untrusted data; `limits.py` resolves the `consults:` bounds; `errors.py` holds the stable `consult.*` codes. `tools/consult.py` is the asker's tool (`limited_to(targets)`). For ACP askers (G.3): `transports.py` is the exact-adapter gate table (forwarding, permission provenance and quiescence, each `supported`/`declared`/`unknown`; an identity is exposed only when all three are supported **and** implemented — none today), `broker.py` the per-session `0700`-directory Unix socket bound to a rotating 256-bit token, the asker's process identity and a fixed session (a request names only target, question, brief and request id), `handshake.py::AcpConsultHost` the `mcpServers` offer and structured-identity permission auto-allow used by `acp/adapter.py` (`attach_consult`), and `interfaces/consult_mcp.py` the stdio MCP server exposing only `consult`. The token is registered with `context/redact.register_secret` before use and never reaches the child, the store, logs or exports. `agents/inspect.py::dashboard_rows` (H.11) is the Setup view's agent list (source, declared settings with provenance, definition and static prompt digests, section sizes in UTF-8 bytes/characters/estimated tokens; no text, redacted, one broken definition does not hide the rest; source-free errors/warnings through `dashboard_problem`, registry-validated codes shared with the setup fallback, detailed diagnostics kept local; registry-known unsupported fields show structural names and provenance without values, and are not activated) and `core/conversation.py::agent_info` the current session identity and recorded native execution bindings with their actual system-prompt digests. Shared `agents/setup.py` carries the compiled agent name/digest into `AgentConfig`; `core/run_state.py` snapshots a new native execution id, and `core/loop.py` attaches that binding to each actual system-message measurement, including inner runs without session-start events. Historical unbound native measurements stay unattributed; the bounded read model retains native compatibility aggregates and reads ACP request measurements from the existing ACP event trail. ACP rows label attempted request text and unknown internal system prompts. `observability/agent_binding.py` snapshots matching active runtime references and validates recorded tenure indices/identities; native runtime annotates its EventStore before the driver, and run preparation copies the annotation. ACP snapshots after identity publication/bind. Conversation rows retain known execution identities even when tenure association is unknown, with per-tenure counts and explicit unknown/omitted provenance. `agents/role_agent.py` (H.10) binds a role's `agent:` to the plan that will run: native digest in the identity, the ACP projection (effort, stricter permissions, labelled instructions; every other request refuses `agent.field_unsupported`) and `consult_spec`, a consulted native role's definition narrowed to the consult profile. Native RolePlan holds the immutable bound AgentSpec outside record/repr; shared role_agent.native_spec supplies it to CLI selection and consult narrowing, rejecting conflicting explicit agent files before activation. Native admission also compares the agent binding’s trusted reasoning model id with the role’s exact model selector, refusing contradictions/missing aliases before activation. The shared resolver retains explicit/inherited instruction replacement intent independently of text; ACP refuses replacement even for empty/default-prefixed text, while native assembly remains unchanged. `view.py` (G.4) is the read-only row model of a root task's consults (state plus receipts, status and evidence normalised, unknown kept unknown); `core/conversation.py` rolls consulted children into their asker once, keyed by ledger record, with `origin` as a grouping dimension beside `call_purpose`; `core/usage_stats.py` adds `by_origin` over original events; `core/setup_view.py` lists grants, ceilings and per-adapter transport status; `flows/engine.py::_independence` records the review decision with actual launched and consulted identities. |
| `config/` | `agent_home.py` — all `.agent/` discovery. `garuda_yaml.py` (C.1) — the strict v1 `garuda.yaml` schema (roles, harnesses, flows, checks, consult limits, sessions; safe unique-key YAML, full-path errors, v1 bounds, no `authority` key) and its layering (package < user < trusted project < CLI: whole-definition replacement, intersected ceilings, accumulated deduplicated checks, user-only harnesses/keep_days/fallbacks/consult grants, `config.conflict` for legacy keys and explicit mismatches), plus `garuda config migrate`. `project_trust.py` (C.2) — reads the project `garuda.yaml` once without following symlinks, parses the hashed bytes, and withholds its checks and native model strings unless those exact bytes are trusted in the #143 content-bound store (`garuda config trust`, interactive only). `recipes.py` — YAML multi-step workflows. `agents/setup.py::build_runtime_catalog()` is the one trusted runtime-registry builder (`prepare_runtime_catalog(workspace)` feeds it the global and project files; `acp/catalog.py::shared_registry` is a thin adapter over it): it reads global manifests and disablement, treats project refs as non-authoritative (authority attempts refuse, malformed advice warns), executes no probes (discovery runs only through `RuntimeCatalog.discover()`), and is the required CLI/SDK launch gate. |
| `mcp/` | `config.py` (merge + allowlist), `client.py` (per-run server manager). |
| `skills/` | `loader.py` — progressive disclosure, `allowed-tools` validation. |
| `sdk/` | `software_agent.py`, `conversation.py` — the library surface, both with a `runtime` selector (native default, unchanged path) plus ACP single-turn runs and held multi-turn harness sessions. ACP harnesses resolve through the shared registry (unknown/disabled refused), tenures attach to persisted unified sessions with baselines, agent approvals relay to an optional handler, and `switch_runtime` runs the handoff transaction between held harnesses. |
| `interfaces/web/` | The `garuda web` dashboard: read past runs, and talk to an agent. `security.py` — Host/Origin/token gate (its own, because `interfaces/server.py` blanket-refuses browsers). `http.py` — threaded stdlib server + static. `routes.py` — pure `dispatch`, so routes test without a socket. `reads.py` — the read model over `SessionStore` + the trajectory reader, plus the `ReaderCache` whose lock spans `refresh()` (a shared reader appends its tail twice otherwise). `tail.py` — byte-offset tailing; the offset only advances past the last newline, because `EventStore.append` is not atomic and consuming a torn line desyncs the cursor permanently. `live.py` — conversations: the workspace allowlist, the permission ceiling, the turn lifecycle and cancellation. `grounding.py` — an uploaded file or fetched page becomes a workspace file the agent reads with its own tools; URLs go through `tools/web.py`'s SSRF-vetted fetcher, never a second one. `approvals.py` — parked approvals; the only thread-and-loop code here, so read its docstring before touching it. `static/` — no-build frontend; `views_trajectory.js` is the trace view, where a turn renders as in/thinking/says/does, tool colour carries the *family* rather than the outcome, and the turn timeline and gate lane are one CSS grid so alignment cannot drift. Checked by executing it in Chrome — see `tests/browser/`.  `runtimes.py` — pure runtime controls (picker, health, handoff preview/prepare, diff timeline, recovery reports) behind the `/api/runtimes` and `/api/runs/<id>/{handoff,diff,recover}` routes. |
| `observability/` | `trajectory.py` — rebuilds turn structure from an `events.jsonl` (one reader for local sessions and Harbor trials alike); `tracing.py` — spans. `lanes.py` — cross-runtime lanes over unified segments (identity, native refs, authority, cursors, handoff/recovery) plus a versioned, whitelisted, secret-scrubbed export; historical logs read with zero lanes. The adapter attaches its tenure and advances cursors when bound to a session store, so lanes populate from execution. `runtime_metrics.py` — per-adapter phase timings with triage buckets (native accounting untouched; constructed at the shared runtime boundary for every run); `support.py` — redacted support bundles with kind tallies and recursive scrub, never payloads, behind a documented CLI/SDK entry point. |
| `plugins/` | `hooks.py` — lifecycle hooks. |
 | `acp/` | `protocol.py` — the owned ACP v1 wire subset (JSON-RPC + newline-delimited framing; one line, complete or partial, is bounded at 16 MiB; numeric version pin, typed transport failures). `catalog.py` carries optional bound role definition metadata into the adapter; `adapter.py` records each attempted outbound request hash/character count with a fresh sending execution id through the source-free `outbound_prompt` runtime event. This measures host request text, not the external internal system prompt. `client.py` — one managed agent subprocess: minimal child env, process-group launch, stderr diagnostics off the protocol stream, deadlines, drained cancel notifications, queued agent-initiated requests, launch-after-close refusal, reader buffer cap, and guaranteed reap. `authority.py` — capability negotiation (recorded intent from Garuda extension fields, not an enforced boundary) assigning exactly one owner per tool family (strict policies refused, safe agent-sandbox defaults; every construction/restore path validates all families present with exactly one valid owner), with snapshot round-trips into session capability records. `normalize.py` — the stateful per-session ACP normalizer over v1 `sessionUpdate` payloads (message/thought chunks as content blocks, tool calls whose content carries diffs, v1 stop reasons; unknown kinds kept as informational events): causal ordering (calls before their updates), partials preserved exactly once, turn-close vs session-terminal rules, raw records as redacted session-local diagnostics. `adapter.py` — the generic `AcpRuntime` every adapter runs the shared conformance suite through (launch, version-checked handshake, negotiation, streaming prompt, v1 `session/request_permission` answered once with `allow_once`/`reject_*` or `cancelled` — never widened to `allow_always` — refusal of unadvertised client methods, cancel, close). `fake_agent.py` — the deterministic `python -m` test server with capability/streaming/approval/diff/malformed/slow/exit/resume/mismatch profiles (the public set is pinned by name in tests, unknown profiles rejected); stdio only, isolated from workspaces and credentials. `broker.py` — the one approval path: engine ceilings applied to native and ACP requests, parked approvals with timeout and disconnect denial, every outcome persisted to the session (audit-write failure denies), attachable answerers (interactive prompt or headless deny-all), strict-policy gaps reported before anything runs; `run_agent_task` installs the session broker on the engine so facade runs share it. `approval_channel.py` (B.8) — the file-backed channel: each parked approval is published owner-only and fsynced, bound to session, approval id, request digest, nonce, expiry and a ceiling fingerprint; an answer appears by exclusive `link` (complete or absent, one writer); replayed, foreign, modified, symlinked, partial or expired answers and a ceiling change deny; one exclusive decision record per approval, and a delivery reserved but never acknowledged (a crash) is never resent. `login_probe.py` — the documented login check run exactly as the manifest says (closed stdin, minimal env, timeout, bounded output), concluding authenticated / logged out / failed / timeout / unrecognized / no probe, with a 60 s cache of conclusions only. `catalog.py` — built-in stubs plus discovery (executable, probed version, login state, capabilities, setup guidance) that never logs in, installs, or reads tokens; malformed trusted settings refuse discovery rather than clear disablement, disabled built-ins remain visible, and `builtin/` holds Claude Code/Codex user-authenticated adapter manifests, which `agents/setup.py` merges into the trusted catalog under global overrides; `adapter_for_discovered` binds the executable a discovery record accepted into the launch argv, with no second `PATH` lookup. |
## Working on it

ACP adapters use stable v1 NDJSON with bidirectional JSON-RPC. The shared
client rejects malformed records, sends an explicit failure for unhandled
agent-to-client requests, and passes absolute session roots and content blocks
to installed vendor adapters; it does not silently fall back to the legacy
private framing.

```bash
pip install -e ".[dev]"
pytest tests/ -v
ruff check garuda/

garuda run -t "List files in the current directory"          # interactive posture
garuda run -t "..." --mode eval --model openrouter/deepseek/deepseek-v4-flash-0731   # full gate stack
```
