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
| `run_state.py` | `prepare_run` (assembly: tool filtering, buffer, context bootstrap, subagent wiring, deadline) and `RunState` (what the loop reads and writes, plus result building). |
| `steering.py` | Every message the harness injects between turns: budget notices, the budget-review and final-turn nudges, repetition and failure-streak detection. Notes are *queued*, never appended mid-turn — see its docstring for why. |
| `tool_runner.py` | Executing one call or a concurrent read batch: permissions, hooks, output shaping/buffering, event ordering. `PARALLEL_SAFE_TOOLS` lives here and the fan-out is bounded by `AgentConfig.max_parallel_reads`. |
| `metrics.py` | Per-turn model latency, tool latency and wall-clock, compaction and checkpoint time, cache-hit rate. Rolls up onto `AgentResult.metadata["metrics"]` and emits one `turn_metrics` event per turn. |
| `completion.py` | The `task_complete` gate: acceptance contract, side-effect sweep, verification, and the yield-breaker that stops it livelocking. |
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
| `subagent.py` | Forked child runs. |
| `action_memo.py` | Session memory of what has already been asked, so a repeated read is answered rather than re-run. Filesystem reads stop being memoized while a background task is live — it writes between calls, and no call marks that. `bash_background` reports its own exits so caching resumes; a raw `cmd &` reports nothing, so it suspends caching for the session. |

## `tools/` — what the agent can do

`protocol.py` defines the contract (`ToolContext` in, `ToolResult` out);
`registry.py` + `__init__.py::build_toolkit` assemble the set a profile asks for.

Execution: `bash.py` (a command that backgrounds something also records the
process group it leaves behind, so the pre-completion sweep has an exact handle
on it), `background.py`, `tmux.py`.
Files: `files.py`, `edit.py`, `multi_edit.py`, `search.py` (ripgrep-backed),
`diagnostics.py` (post-edit syntax + lint).
Reading: `documents.py`, `image_read.py`.
Network: `web.py` — SSRF guard, redirect re-validation, and pinned-IP connect.
Agent-facing state: `todo.py`, `goal.py`, `contract.py`, `task_complete.py`.
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
heartbeat TTL with audited stale takeover, corrupt leases fail closed, user
files never touched; TTLs validated positive/finite, locking fail-closed when
`fcntl` is unavailable or `flock` fails) plus worktree isolation keys and
creation hooks. `run_agent_task` acquires the mutating lease for the workspace
before resolving the environment, heartbeats for the whole run, and releases
last (also on cancellation) — concurrent `run_agent_task` runs on one workspace
are refused, never interleaved. Interactive paths that call `agent.run`
directly (dashboard chat, CLI chat, SDK `Conversation`) take no lease yet; see
`BACKLOG.md`.
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
is a new session with a fresh baseline, so the prior session's work reads as
preexisting. `runtime/handoff.execute_handoff(workspace=...)` carries the delta
into a handoff; `garuda runtime handoff --confirm` passes its workspace. The
`.context/` pack files the harness syncs into the workspace appear in the delta
as session changes.

## `context/` — fitting the conversation in the window

`manager.py` holds history and decides when to act. `shaper.py` caps and shapes
tool output, `condenser.py` compacts (`microcompact` by default) and demotes
pruned history into buffers, `summarizer.py` produces the summaries.
`schemas.py` validates the generated `current-task.md`/`handoff.md` files
(versioned frontmatter, bounded fields, unknown fields round-trip, unknown
versions rejected). `pack.py` is the single writer: it compiles both files
deterministically from the state card, session record, and git evidence
(byte-identical recompilation, budgeted briefs that keep provenance), writes
atomically, and refuses any other target. `RunState` syncs the pack at the
checkpoint boundary and re-syncs after every compaction; resume restores the
persisted working state first, so compaction and restart preserve pack facts.
`redact.py` validates packs (size, workspace-relative paths, no-reasoning
markers) and best-effort redacts secret patterns (full PEM blocks, tokens,
credential assignments) recursively over every string and key; the writer
scrubs automatically, rebuilds from the scrubbed map, and blocks unsafe
handoffs, while durable repository files are unreachable by construction
(strings in, never paths).

## `agents/` — profiles

`setup.py::prepare_agent_run` is the shared chokepoint for every entry point.
`loader.py` reads YAML profiles (and records which fields were declared, so mode
presets don't override authored intent); `md_loader.py` + `frontmatter.py` read
OpenCode-style `agent.md`. Built-ins in `defaults/`: `build`, `plan`, `explore`,
`harbor`, `reviewer`.

## `model/` — provider access

`protocol.py` is the `Model` interface. `litellm_model.py` is the real
implementation (streaming, tool calls, reasoning effort, prompt caching, retries);
`governor.py` caps per-provider concurrency; `script_model.py` is the deterministic
test double — prefer it over mocks.

## `runtime/` — harness boundary

`protocol.py` is the `AgentRuntime` interface every harness implements (native or
external — never place a harness behind `Model`). `events.py` is the normalized
event vocabulary with session/turn correlation. `fake.py` is the deterministic
test double with scripted scenarios; every adapter must pass the shared suite in
`tests/test_runtime_conformance.py`. `registry.py` resolves trusted global
harness manifests (the only place that authorizes an executable) plus
project-level aliases that reference and narrow but never self-authorize; one
instance per job, never process-global. `session.py` is the unified session
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
records its child, the source closes, and only then does an optional `deliver`
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
runtime/session as its own process-group leader together with two process
identities (boot id plus start time from `/proc/<pid>/stat` on Linux; `ps -o
lstart= -o ucomm=` elsewhere — never a name the process can rewrite, such as
Node's `process.title`): the child's and its owning Garuda process's.
Recovery refuses while a live workspace lease names the session or the
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
evidence that classification does not consume yet. Only `AcpRuntime(store=…)`
(or `bind_session` after a handoff acknowledgement) and
`HandoffTransaction(store=…)` record children and switch cancels; `garuda run
--runtime <acp>` and `garuda runtime handoff --confirm` are the production
callers. Without a store the adapter logs a warning and records nothing.

## `interfaces/` — entry points

`main.py` (CLI argument surface), `headless.py` (`garuda run`), `cli.py` + `tui.py`
(interactive chat), `runtime_cli.py` (`garuda runtime list|inspect|handoff|recover`
plus the `run --runtime <acp>` launch path), `run_guard.py` (run invariants shared
by native runs, ACP runs, and CLI handoffs: the workspace lease with heartbeat and
race, and the P0.17 broker approval path), `server.py` + `jobs.py` (job-queue server: submit/status/
events/result/cancel), `session.py` (multi-turn state shared by CLI and SDK),
`runner.py` (assembles a run and owns workspace teardown).

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

## Everything else

| Package | Owns |
|---|---|
| `config/` | `agent_home.py` — all `.agent/` discovery. `recipes.py` — YAML multi-step workflows. `agents/setup.py::build_runtime_catalog()` is the one trusted runtime-registry builder (`prepare_runtime_catalog(workspace)` feeds it the global and project files; `acp/catalog.py::shared_registry` is a thin adapter over it): it reads global manifests and disablement, treats project refs as non-authoritative (authority attempts refuse, malformed advice warns), executes no probes (discovery runs only through `RuntimeCatalog.discover()`), and is the required CLI/SDK launch gate. |
| `mcp/` | `config.py` (merge + allowlist), `client.py` (per-run server manager). |
| `skills/` | `loader.py` — progressive disclosure, `allowed-tools` validation. |
| `sdk/` | `software_agent.py`, `conversation.py` — the library surface, both with a `runtime` selector (native default, unchanged path) plus ACP single-turn runs and held multi-turn harness sessions. ACP harnesses resolve through the shared registry (unknown/disabled refused), tenures attach to persisted unified sessions with baselines, agent approvals relay to an optional handler, and `switch_runtime` runs the handoff transaction between held harnesses. |
| `interfaces/web/` | The `garuda web` dashboard: read past runs, and talk to an agent. `security.py` — Host/Origin/token gate (its own, because `interfaces/server.py` blanket-refuses browsers). `http.py` — threaded stdlib server + static. `routes.py` — pure `dispatch`, so routes test without a socket. `reads.py` — the read model over `SessionStore` + the trajectory reader, plus the `ReaderCache` whose lock spans `refresh()` (a shared reader appends its tail twice otherwise). `tail.py` — byte-offset tailing; the offset only advances past the last newline, because `EventStore.append` is not atomic and consuming a torn line desyncs the cursor permanently. `live.py` — conversations: the workspace allowlist, the permission ceiling, the turn lifecycle and cancellation. `grounding.py` — an uploaded file or fetched page becomes a workspace file the agent reads with its own tools; URLs go through `tools/web.py`'s SSRF-vetted fetcher, never a second one. `approvals.py` — parked approvals; the only thread-and-loop code here, so read its docstring before touching it. `static/` — no-build frontend; `views_trajectory.js` is the trace view, where a turn renders as in/thinking/says/does, tool colour carries the *family* rather than the outcome, and the turn timeline and gate lane are one CSS grid so alignment cannot drift. Checked by executing it in Chrome — see `tests/browser/`.  `runtimes.py` — pure runtime controls (picker, health, handoff preview/prepare, diff timeline, recovery reports) behind the `/api/runtimes` and `/api/runs/<id>/{handoff,diff,recover}` routes. |
| `observability/` | `trajectory.py` — rebuilds turn structure from an `events.jsonl` (one reader for local sessions and Harbor trials alike); `tracing.py` — spans. `lanes.py` — cross-runtime lanes over unified segments (identity, native refs, authority, cursors, handoff/recovery) plus a versioned, whitelisted, secret-scrubbed export; historical logs read with zero lanes. The adapter attaches its tenure and advances cursors when bound to a session store, so lanes populate from execution. `runtime_metrics.py` — per-adapter phase timings with triage buckets (native accounting untouched; constructed at the shared runtime boundary for every run); `support.py` — redacted support bundles with kind tallies and recursive scrub, never payloads, behind a documented CLI/SDK entry point. |
| `plugins/` | `hooks.py` — lifecycle hooks. |
 | `acp/` | `protocol.py` — the owned ACP v1 wire subset (JSON-RPC + newline-delimited framing; one line, complete or partial, is bounded at 16 MiB; numeric version pin, typed transport failures). `client.py` — one managed agent subprocess: minimal child env, process-group launch, stderr diagnostics off the protocol stream, deadlines, drained cancel notifications, queued agent-initiated requests, launch-after-close refusal, reader buffer cap, and guaranteed reap. `authority.py` — capability negotiation (recorded intent from Garuda extension fields, not an enforced boundary) assigning exactly one owner per tool family (strict policies refused, safe agent-sandbox defaults; every construction/restore path validates all families present with exactly one valid owner), with snapshot round-trips into session capability records. `normalize.py` — the stateful per-session ACP normalizer over v1 `sessionUpdate` payloads (message/thought chunks as content blocks, tool calls whose content carries diffs, v1 stop reasons; unknown kinds kept as informational events): causal ordering (calls before their updates), partials preserved exactly once, turn-close vs session-terminal rules, raw records as redacted session-local diagnostics. `adapter.py` — the generic `AcpRuntime` every adapter runs the shared conformance suite through (launch, version-checked handshake, negotiation, streaming prompt, v1 `session/request_permission` answered once with `allow_once`/`reject_*` or `cancelled` — never widened to `allow_always` — refusal of unadvertised client methods, cancel, close). `fake_agent.py` — the deterministic `python -m` test server with capability/streaming/approval/diff/malformed/slow/exit/resume/mismatch profiles (the public set is pinned by name in tests, unknown profiles rejected); stdio only, isolated from workspaces and credentials. `broker.py` — the one approval path: engine ceilings applied to native and ACP requests, parked approvals with timeout and disconnect denial, every outcome persisted to the session (audit-write failure denies), attachable answerers (interactive prompt or headless deny-all), strict-policy gaps reported before anything runs; `run_agent_task` installs the session broker on the engine so facade runs share it. `catalog.py` — built-in stubs plus discovery (executable, probed version, login state, capabilities, setup guidance) that never logs in, installs, or reads tokens; malformed trusted settings refuse discovery rather than clear disablement, disabled built-ins remain visible, and `builtin/` holds Claude Code/Codex user-authenticated adapter manifests, which `agents/setup.py` merges into the trusted catalog under global overrides; `adapter_for_discovered` binds the executable a discovery record accepted into the launch argv, with no second `PATH` lookup. |
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
