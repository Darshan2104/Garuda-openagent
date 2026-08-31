# ACP orchestration issue catalog

This is the canonical issue payload for the
[ACP orchestration roadmap](2026-08-acp-orchestration.md). Create each heading
marked **Epic** as a GitHub parent issue, then attach the following child issues
in listed order. P0 work is a hard dependency for P1 work.

## P0 Epic: repository standards and documentation

**Outcome:** contributors, users, and external harnesses have one current source
of truth for architecture, operating rules, context, and product documentation.

### P0.1 Documentation migration and navigation

**Scope:** retain one concise root README; move benchmark, browser-test, and
archive guidance under `docs/`; add a navigable docs index.

**Acceptance criteria:** no nested README remains; every deleted README has an
equivalent docs page; root README links to installation, use, configuration,
development, evaluation, and roadmap pages; all relative links resolve.

**Verification:** markdown-link check and manual navigation from a fresh clone.

**Non-goals:** a documentation website generator or a branding redesign.

### P0.2 Contributor contract and durable context pack

**Scope:** add `AGENTS.md`, committed `.context/` files, and gitignore policy for
runtime task/handoff files.

**Acceptance criteria:** `AGENTS.md` distinguishes `AGENTS.md` from agent-profile
`agent.md`; it describes testing, safety, dirty-worktree, and documentation
rules; durable context has explicit ownership; transient files are ignored.

**Verification:** repository status confirms only transient context is ignored;
new contributors can identify current architecture and change commands without
opening archived RFCs.

### P0.3 Documentation contract checks

**Scope:** add automated Markdown-link validation, a check that blocks new nested
README files, and a generated-test-status policy instead of hand-maintained counts.

**Acceptance criteria:** CI fails on broken local docs links and unexpected nested
README files; release documentation does not claim an unverified test count.

**Dependencies:** P0.1, P0.2.

**Security:** validation must not fetch arbitrary remote URLs in a privileged CI job.

## P0 Epic: runtime foundation

**Outcome:** one stable abstraction represents Garuda's native loop and external
coding harnesses without treating an agent as an inference model.

### P0.4 Define `AgentRuntime` protocol and event vocabulary

**Scope:** add typed runtime capabilities, lifecycle states, request/response
contracts, normalized events, and typed adapter failures.

**Acceptance criteria:** protocol covers discover, health/auth status, start,
resume, prompt, cancel, permission response, close, version, and native session
ID; every event has session/turn correlation and a stable discriminant; no ACP
types leak into the generic protocol.

**Tests:** deterministic protocol conformance tests using a fake runtime.

**Non-goals:** ACP process management or UI work.

### P0.5 Runtime registry and declarative harness configuration

**Scope:** define global/project harness configuration, executable allowlisting,
capability overrides, and precedence rules.

**Acceptance criteria:** configuration contains commands and non-secret metadata
only; project configuration cannot self-authorize a new executable; resolved
runtime/version/capabilities are inspectable; malformed configuration fails closed.

**Dependencies:** P0.4.

**Security:** never import environment secrets or copy credentials from another
harness home.

### P0.6 Unified session persistence and migration

**Scope:** extend session persistence with runtime identity, native session IDs,
capability snapshot, workspace baseline, handoff state, and event cursors.

**Acceptance criteria:** old native sessions continue to resume; writes remain
atomic and locked; a failed migration leaves the prior session readable; unknown
future versions fail with an actionable error.

**Dependencies:** P0.4.

**Tests:** round-trip, crash-mid-write, concurrent-meta-write, and migration
fixtures.

### P0.7 Native Garuda runtime bridge

**Scope:** adapt current `DefaultAgent`/`RigorousAgent` execution into
`NativeGarudaRuntime` without changing native task semantics.

**Acceptance criteria:** CLI, SDK, server, and web routes exercise native runs
through the new boundary; existing mode, permission, event, resume, and verifier
tests remain green.

**Dependencies:** P0.4, P0.6.

**Non-goals:** changing prompt construction or completion gates.

## P0 Epic: context and transactional handoff

**Outcome:** an agent can hand a task to another compatible runtime without
copying opaque vendor state or losing verified work.

### P0.8 Versioned context schemas

**Scope:** define YAML frontmatter and Markdown bodies for `current-task.md` and
`handoff.md`, including task, acceptance criteria, changed files, evidence,
blockers, next action, source runtime, and redaction metadata.

**Acceptance criteria:** schemas are human-readable, versioned, bounded, and
validated; unknown fields round-trip safely or are rejected by version policy.

**Dependencies:** P0.6.

### P0.9 Context compiler and checkpoint integration

**Scope:** implement the single-writer `ContextPackManager` using existing state
cards, session persistence, Git evidence, and verifier results.

**Acceptance criteria:** durable files are never overwritten by runtime updates;
generated files update atomically; prompt briefs are budgeted and cite source
paths; compaction does not discard context-pack facts.

**Dependencies:** P0.8, P0.6.

**Tests:** deterministic rendering/golden fixtures and crash recovery.

### P0.10 Handoff transaction state machine

**Scope:** implement boundary-only pause, source checkpoint, diff/evidence
capture, handoff generation, target startup, acknowledgement, and rollback.

**Acceptance criteria:** no two active mutating owners exist; target failure keeps
source resumable; cancellation during switch lands in a recoverable state; every
transition emits a typed event.

**Dependencies:** P0.9, P0.4.

### P0.11 Context validation and secret redaction

**Scope:** enforce size limits, file-path validity, secret-pattern redaction,
safe command-output summaries, and no-reasoning policy.

**Acceptance criteria:** sensitive values cannot reach `.context/` or normal
handoff views; an unsafe handoff blocks switching with an actionable event;
redaction does not silently alter durable repository documentation.

**Dependencies:** P0.8.

**Tests:** known-secret fixtures, oversize payloads, path escapes, and false
positive review cases.

## P0 Epic: ACP client foundation

**Outcome:** Garuda can safely supervise any conforming ACP agent through a
single, well-tested process boundary.

### P0.12 ACP subprocess client and JSON-RPC lifecycle

**Scope:** integrate the Python ACP SDK or a minimal version-pinned client;
launch configured subprocesses; implement initialization, sessions, prompts,
cancel, close, deadlines, and process cleanup.

**Acceptance criteria:** stdin/stdout are framed safely; stderr is captured as
diagnostics without corrupting protocol messages; unexpected exit, timeout, and
invalid JSON become typed errors; subprocess groups are always reaped.

**Dependencies:** P0.4, P0.5.

**Security:** command comes from the resolved allowlist, not prompt text; child
environment is minimized and secret forwarding is explicit.

### P0.13 ACP capability negotiation and execution authority

**Scope:** negotiate client/agent capabilities and choose exactly one owner for
file editing, terminal execution, MCP, and approval interaction.

**Acceptance criteria:** unsupported strict policy is refused; an authority map
is persisted in the capability snapshot; Garuda never duplicates an agent's tool
call; safe defaults prefer the agent's native sandbox when Garuda cannot mediate.

**Dependencies:** P0.12, P0.5.

### P0.14 ACP event normalizer

**Scope:** transform ACP items, deltas, tool calls, diffs, approvals, and errors
into the Garuda event vocabulary while retaining raw protocol records only in
session-local diagnostics.

**Acceptance criteria:** UI/SDK consumers can render native and ACP events with
one reader; event ordering and terminal-state rules are explicit; partial output
is preserved without duplicate final messages.

**Dependencies:** P0.12, P0.4.

### P0.15 Fake ACP agent and conformance suite

**Scope:** add a deterministic ACP test server able to emulate capabilities,
streaming, approvals, diffs, malformed frames, cancellation, resume, and exits.

**Acceptance criteria:** every adapter runs the same lifecycle contract suite;
tests require no subscription or network; protocol fixtures pin supported ACP
version behavior.

**Dependencies:** P0.12–P0.14.

## P0 Epic: safety and repository state

**Outcome:** a multi-harness run cannot silently overwrite another run, hide
pre-existing changes, or bypass the declared safety posture.

### P0.16 Workspace lease and worktree isolation

**Scope:** add exclusive mutating-workspace leases, read-only sharing rules,
lease heartbeats, stale lease recovery, and worktree guidance/creation hooks.

**Acceptance criteria:** concurrent mutation of one workspace is refused; stale
owners recover safely; parallel worktrees have isolated session/context state.

**Dependencies:** P0.6, P0.10.

### P0.17 Permission broker and approval lifecycle

**Scope:** map Garuda policy ceilings to ACP requests, surface pending approvals,
enforce timeouts/heartbeat denial, and report unenforceable adapter capabilities.

**Acceptance criteria:** every allow/deny/timeout is persisted; strict modes do
not downgrade silently; UI, CLI, and SDK use the same decision path.

**Dependencies:** P0.13, P0.6.

### P0.18 Baseline and authoritative diff manager

**Scope:** capture initial commit/status/fingerprint, calculate session delta,
reconcile ACP diff hints with filesystem/Git truth, and expose changed files to
handoff and verifier flows.

**Acceptance criteria:** pre-existing dirty changes are preserved and clearly
separated; renamed/deleted/untracked files are represented; large diffs are
bounded but recoverable from disk.

**Dependencies:** P0.16, P0.14.

### P0.19 Cancellation, recovery, and source-session preservation

**Scope:** define cancellation at turn/switch/process boundaries and recovery
flows for crash, disconnected UI, malformed ACP stream, and target startup fail.

**Acceptance criteria:** no orphan child process; source native session remains
resumable after handoff failure; terminal event state is never ambiguous.

**Dependencies:** P0.10, P0.12, P0.17.

## P1 Epic: harness adapters and subscription discovery

**Outcome:** users can select installed and authenticated harnesses without
giving Garuda subscription credentials.

### P1.1 Harness manifest, discovery, and health model

**Scope:** add built-in manifests plus generic ACP command configuration;
discover executable, adapter version, login status, capabilities, and warnings.

**Acceptance criteria:** discovery performs no login, install, or token read;
unknown versions are displayed; unavailable adapters explain setup steps; users
can disable a discovered runtime.

**Dependencies:** P0.5, P0.12.

### P1.2 Claude Code and Codex ACP adapters

**Scope:** ship tested manifests and setup guidance for official/registry ACP
adapters that launch user-authenticated Claude Code and Codex.

**Acceptance criteria:** each adapter passes the common suite; subscription
access is delegated to the user's CLI; no private HTTP endpoint is used; version
and capability limitations are documented.

**Dependencies:** P1.1, P0.15–P0.19.

### P1.3 Cursor Agent and OpenCode adapters

**Scope:** add manifests, capability handling, and opt-in integration coverage
for Cursor Agent and OpenCode ACP paths.

**Acceptance criteria:** non-ACP fallback is not silently selected; adapter
version incompatibility is actionable; switching and cancellation conform to the
same contract as P1.2.

**Dependencies:** P1.1, P0.15–P0.19.

### P1.4 Pi, Goose, and generic ACP registry adapters

**Scope:** support Pi and Goose plus user-configured/registry ACP agents through
one generic path.

**Acceptance criteria:** adding a compatible agent needs configuration rather
than code when its capability set is standard; names/commands are allowlisted;
generic adapters cannot claim vendor-specific guarantees.

**Dependencies:** P1.1, P0.13, P0.15.

### P1.5 Authentication and subscription UX

**Scope:** present login/availability/quota state without exposing tokens or
misrepresenting subscription coverage.

**Acceptance criteria:** login opens only explicit user-requested vendor flow;
quota is shown only when the harness provides it; missing credentials never
trigger secret discovery; docs state that vendor policy governs subscription use.

**Dependencies:** P1.2–P1.4.

## P1 Epic: product surfaces

**Outcome:** all Garuda entry points use the same runtime/session model.

### P1.6 CLI runtime selection and handoff commands

**Scope:** add runtime listing, capability inspection, explicit selection,
session resume, handoff preview/confirm, and recovery commands.

**Acceptance criteria:** CLI defaults remain backward compatible; each command
renders safe diagnostics in text and JSON; no switch occurs without explicit
confirmation unless an approved policy authorizes it.

**Dependencies:** P1.1, P0.10, P0.19.

### P1.7 Web dashboard runtime controls

**Scope:** add runtime picker, health status, capability/warning display,
approval cards, handoff preview, diff timeline, and failed-switch recovery UI.

**Acceptance criteria:** web security properties remain unchanged; UI does not
invent events absent from the normalized log; real-browser checks cover switching
and disconnected approval behavior.

**Dependencies:** P1.5, P0.14, P0.17–P0.19.

### P1.8 SDK and JSON-RPC runtime APIs

**Scope:** expose runtime selection, session mapping, event streaming,
permissions, and switch/recovery operations to `SoftwareAgent`, `Conversation`,
and server jobs.

**Acceptance criteria:** no process-global registry leaks between jobs; API
schemas are versioned; old SDK code continues to create a native runtime.

**Dependencies:** P0.4, P0.6, P0.14.

### P1.9 Cross-runtime trace reader

**Scope:** extend the trajectory reader and export format with runtime identity,
handoff lanes, native session references, authority map, and recovery state.

**Acceptance criteria:** historical logs remain readable; a trace distinguishes
native Garuda events from normalized external agent events; raw transcript and
secrets remain excluded.

**Dependencies:** P0.14, P0.18, P1.8.

## P1 Epic: quality, evaluation, and observability

**Outcome:** adapters are measurable, reproducible, and safe to evolve.

### P1.10 Runtime and handoff contract matrix

**Scope:** run all runtimes against common lifecycle, permission, diff,
cancellation, resume, handoff, and recovery scenarios.

**Acceptance criteria:** test matrix is generated from declared capabilities;
an adapter cannot be marked supported without a passing contract report.

**Dependencies:** P0.15, P0.19, P1.2–P1.4.

### P1.11 Opt-in real-harness integration tests

**Scope:** add locally gated smoke tests for installed authenticated agents with
strict cost/turn/time caps and no default subscription consumption.

**Acceptance criteria:** CI never needs user subscriptions; local runner reports
exact harness/version; tests can use a fixture workspace and clean worktree.

**Dependencies:** P1.2–P1.4, P1.10.

### P1.12 Cross-harness evaluation matrix

**Scope:** extend ablation/eval metadata to compare native and external
harness-runtime pairs on capability, completion, cost/unknown cost, latency,
approval burden, and handoff success.

**Acceptance criteria:** reports do not compare unavailable cost data as zero;
model and harness are separate dimensions; tasks are reproducible and no vendor
claim is inferred from a one-off run.

**Dependencies:** P1.9–P1.11.

### P1.13 Runtime observability and support bundle

**Scope:** add metrics/traces for startup, negotiation, turn duration, approval,
handoff, errors, adapter version, and cleanup; produce redacted support bundles.

**Acceptance criteria:** metrics preserve current native accounting; error triage
can distinguish protocol, harness, permission, workspace, and verifier failures;
support bundles pass redaction tests.

**Dependencies:** P0.14, P0.19, P1.9.

## P2 Epic: platform expansion

### P2.1 Expose Garuda as an ACP server

**Scope:** let ACP-native editors select Garuda itself as an agent server while
reusing the runtime/session/orchestration layer.

**Acceptance criteria:** inbound ACP clients receive stable sessions, diffs,
approvals, and cancellation; outbound and inbound ACP roles are isolated; no
network listener is exposed without explicit secure configuration.

**Dependencies:** all P0/P1 runtime work.

### P2.2 Supported direct model transports

**Scope:** add direct transports only after a vendor publishes supported public
authentication and request/session semantics needed by Garuda.

**Acceptance criteria:** each transport has a vendor-support citation, capability
declaration, test double, opt-in integration test, cost semantics, and migration
notes; no private endpoint or copied subscription token is used.

**Dependencies:** P0.4, P1.10.

### P2.3 Opt-in policy router

**Scope:** recommend or select runtimes by capability, user preference, budget,
availability, quality history, and workspace policy.

**Acceptance criteria:** routing rationale is logged; the user can pin a runtime;
unknown quota/cost never masquerades as free; automatic switch remains boundary
only and opt-in.

**Dependencies:** P1.5, P1.12, P1.13, P2.2.
