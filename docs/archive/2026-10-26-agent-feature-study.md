# What Garuda can learn from oh-my-pi and mini-swe-agent

Research date: **26 October 2026**. This is a source-based comparative study and proposed investment order, not an approved implementation specification or a benchmark result.

**Recommendation:** take oh-my-pi's investment in precise coding tools and inspectable interaction, and mini-swe-agent's discipline around a small execution loop, explicit failures, and reproducible experiments. Build these around Garuda's existing permission, workspace, evidence, and session owners.

The first investment should be **diagnosable failures and a deterministic regression corpus**. The most promising subsequent improvements are **snapshot-bound edits, structural file reads, language-server navigation, and better review inputs**. Continuous advisors and autonomous memory need stronger evidence before becoming defaults.

## Scope, source versions, and confidence

The review used upstream documentation, implementation files, and selected regression tests, alongside Garuda's current source, architecture, feature reference, and [recent workflow audit](2026-10-26-workflow-review.md). Public repository metadata and README pages were checked online; shallow local source snapshots enabled searches across implementation and tests.

| Repository | Reviewed commit | Version declared in source |
| --- | --- | --- |
| [oh-my-pi](https://github.com/can1357/oh-my-pi/tree/553f0d10e813a5e1fb65e545efe7c814bc03a879) | 553f0d10e813a5e1fb65e545efe7c814bc03a879 | coding-agent package 18.13.0 |
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent/tree/04d809ceab9df28f9adaed044884180159172930) | 04d809ceab9df28f9adaed044884180159172930 | Python package 2.4.6 |
| [Garuda](https://github.com/Darshan2104/Garuda-openagent/tree/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af) | 8f6c488335f1220a0aff5f97f76fdbbd7c05d0af | 1.2.0 |

All upstream source links below are pinned to those commits. Findings describe those snapshots; subsequent upstream changes may differ.

**Evidence levels used in this report:**

- **Observed:** a mechanism is present in the inspected source or documented contract.
- **Expected benefit:** an engineering hypothesis explaining why that mechanism might help Garuda.
- **Proposed validation:** what would establish correctness or usefulness before adoption.

Selected upstream tests were inspected, not executed. No upstream dependencies were installed, no authenticated agent sessions were launched, and no paid benchmark runs were performed. Thus this report does not independently validate either project's published performance claims or predict a particular success-rate increase for Garuda. An implementation's existence also does not prove compatibility with our adapters or confinement requirements.

The historical workflow audit is a convenience sample, not a benchmark: it found 124 session records, including 16 self-improvement/CI flow parents, with 11 completed and 5 failed. All 124 recorded verification as unavailable. Several failures had useful lifecycle evidence but insufficient persisted ACP payloads to explain their cause. Those observations motivate diagnostics and verification work; they cannot estimate a population success rate.

## What each project does well

### oh-my-pi: invest in the interface between the model and real development work

Its most useful contribution is a coherent set of capabilities around reading, editing, navigating, debugging, and inspecting work:

- Several edit contracts, current-content snapshots, and controls over edits to undisplayed lines.
- Structural source summaries that expose declarations and tell the agent which omitted ranges to read.
- Language-server navigation and refactoring, plus debugger access through DAP.
- Structured subagent results, isolated workspaces, retained outputs, and human controls over workers.
- Optional advisors and conditionally activated guidance.
- Artifact-backed output, retrievable resource references, and explicit session-context operations.

These are promising because models often lose time at interface boundaries: targeting an edit, finding a real reference, interpreting a truncated log, or reconstructing a worker's result. The current source also shows the maintenance cost: extensive lifecycle, retry, freshness, isolation, and compatibility machinery. We should adopt bounded mechanisms rather than reproduce the whole application. See its [tool documentation](https://github.com/can1357/oh-my-pi/tree/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools) and [session implementation](https://github.com/can1357/oh-my-pi/tree/553f0d10e813a5e1fb65e545efe7c814bc03a879/packages/coding-agent/src/session).

### mini-swe-agent: make execution and experiments easy to understand

Its central loop separates model querying from environment execution, appends observations to a linear history, and saves trajectories even when a step raises. Its environment normally starts a fresh subprocess for each action. Configuration, model/environment identities, cost, and terminal outcome travel with the saved result. The batch runner provides task selection, worker limits, per-instance artifacts, and prediction aggregation. See the [agent](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py), [environment](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/environments/local.py), and [batch runner](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py).

This makes it useful as a reference for experiments and failure inspection. It does not imply that Garuda should discard verification, permissions, workspaces, or recovery ownership to achieve a similarly short loop.

A version correction matters: mini v2 uses native tool calls by default and also supports text-parsed actions. Its current default agent file is 190 physical lines including comments and whitespace. The familiar “100 lines” description expresses the minimal design; it is not a count of the complete product or a requirement to avoid tool calling. See the [v2 migration guide](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/docs/advanced/v2_migration.md).

## Garuda already has much of the foundation

The comparison should not turn existing capabilities into a new feature backlog.

| Area | Current Garuda evidence | Useful remaining difference |
| --- | --- | --- |
| Editing | Layered exact/prefix/EOL/indentation matching; multi-edit commits one file only after all matches succeed | Bind edits to observed content and evaluate alternate contracts |
| Immediate feedback | Syntax checks for several formats and focused Python Ruff checks after writes | Semantic navigation and versioned project diagnostics |
| Large output | Session-local full-output buffers, grep/slice/query tools, bounded fallback shaping | Uniform capture-completeness and retention evidence across result types |
| Context | Compaction, pinned deterministic working state, context packs, bounded session briefs | Explicit investigation-context boundaries and better traceability of what was retained |
| Memory | Scoped, digest-bound proposals accepted by a user; labelled prompt sections | Evidence-linked retrieval, freshness, and deduplication |
| Structured output | Bounded JSON Schema validation at agent completion | Preserve structured child payloads through delegation and flow consumption |
| Delegation | Bounded children, inherited ceilings, child traces; read-only parallel flow groups | Typed child handoff and eventually independently owned editing workers |
| Review | Strict verdict/severity parser, bounded rounds, runtime/model independence | Better review packets and machine-readable finding provenance |
| Providers | LiteLLM, streaming, caching, retry, cost ledger, roles and routing | Tested model-specific interaction contracts and failure accounting coverage |
| Inspection | Event trajectories, dashboard, child logs, session read models | One coherent failure bundle and clearer coverage gaps |
| Continuation | Receipts, resume guards, hourly eligible Codex flow recovery | Better stop classification and scheduler/tick health; safely expanding eligibility is separate work |
| Evaluation | Harbor/ATIF, ablation runner, harness matrix, paired dual-model reports | A frozen minimal baseline and a corpus derived from real workflow failures |

Relevant source owners include [edit.py](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/edit.py), [multi_edit.py](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/multi_edit.py), [diagnostics.py](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/diagnostics.py), [buffer.py](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/buffer.py), [notes.py](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/context/notes.py), [subagent.py](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/subagent.py), [output_schema.py](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/agents/output_schema.py), [review.py](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/flows/review.py), and [evaluation guidance](../evaluation/index.md).

## Ranked opportunities

Priority means recommended sequencing, not a release-blocking defect classification. **A** means start with this batch; **B** means a bounded next experiment; **C** means later or workload-dependent.

Effort is a rough estimate for one engineer familiar with Garuda, including focused boundary tests and documentation. S = roughly 2–4 engineering days; M = 1–2 weeks; L = 3–6 weeks; XL = more than 6 weeks. These are planning estimates, not commitments; live compatibility and cross-platform lifecycle work can increase them.

| ID | Opportunity | Inspiration | Garuda change | Priority | Effort | Benefit confidence |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Failure bundles and causal trace inspection | mini + OMP | Extend existing observability | A | M | High for diagnosis; quality gain unmeasured |
| 2 | Frozen minimal baseline and incident corpus | mini | Extend existing evaluation | A | M | High for regression detection |
| 3 | Snapshot-bound, model-appropriate edits | OMP | Extend native tools | B | M–L | Medium; must compare to our current matcher |
| 4 | Structural file reads | OMP | Extend native read tool | B | M | Medium |
| 5 | LSP navigation and semantic refactoring | OMP | New native service | B | L | High for capability; task benefit unmeasured |
| 6 | Previewed structural rewrites | OMP | New edit-planning capability | B | L | Medium; strongest on repetitive changes |
| 7 | Sufficient review packets and structured findings | OMP + mini | Extend existing review | A | M | High for known review friction |
| 8 | Typed child results, then isolated editing workers | OMP | Extend existing delegation | B | M, then XL | High for handoff correctness; parallel gain uncertain |
| 9 | Evidence-linked, reviewed memory retrieval | OMP | Extend existing memory | C | M–L | Medium |
| 10 | Bounded asynchronous advisor | OMP | New optional review mode | C | L | Low–medium until cost/noise measured |
| 11 | Conditional guidance at safe boundaries | OMP | Extend hooks/steering | C | M | Medium for reminders; no security guarantee |
| 12 | Precise stop reasons, budgets, retry decisions | mini + OMP | Extend shared diagnostics | A | M | High for operational clarity |
| 13 | Uniform artifact completeness and retrieval | OMP + mini | Extend buffers/artifacts | A | M | High for evidence fidelity |
| 14 | Programmatic tool orchestration | OMP | New optional execution service | C | XL | Workload-dependent |
| 15 | Debugger protocol tools | OMP | New optional native service | C | L–XL | High for specific debugging tasks |
| 16 | Investigation checkpoints and session forks | OMP | Extend context/session model | C | L | Medium |
| 17 | Live worker inspection and steering | OMP | Extend existing dashboard | C | L | High for usability; runtime benefit unmeasured |
| 18 | Instruction compatibility and tested model contracts | OMP + mini | Extend configuration inspection | B/C | M–L | Medium |
| 19 | OMP as a distinct external ACP runtime | OMP | New runtime integration | B feasibility | S spike; M–L production | Compatibility unproved |

## 1. Failure bundles and causal trace inspection

**Observed upstream.** Mini saves a trajectory in the run loop's finally block, including terminal exceptions and run configuration. Its parser regression tests explicitly preserve response and cost metadata when action parsing fails. OMP can export/dump sessions and inspect child histories, although its documentation warns that some exports contain raw sensitive content. Sources: [mini parser regressions](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/tests/models/test_format_error_response_persistence.py), [mini output format](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/docs/usage/output_files.md), [OMP export operations](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/session-operations-export-share-fork-resume.md).

**Why this helps us.** Our workflow review could establish that an ACP run stopped but often could not explain a generic internal error. Currently, ordinary persisted ACP events retain identity/kind/sequence; the outbound prompt event additionally retains source-free digest/length information. Native trajectories are richer. A single inspection surface would make that asymmetry explicit and reduce repeated attempts to diagnose unavailable data.

**Recommended implementation.** Add a versioned, bounded diagnostic bundle assembled by the session read layer: resolved runtime/adapter/model identities, effective configuration digest, workspace checkpoint, causal event references, last operation and tool family, original stop cause, later recovery refusal, review state, and verification receipts. Report each field as present, unavailable, clipped, or withheld.

An optional session-local diagnostic capture policy could preserve selected redacted ACP error/tool summaries where the protocol exposes them. Do not record reasoning or blindly archive entire provider responses. Never read vendor credential files. Export should be an explicit local operation with a preview of included data.

**Validation and effectiveness.** Inject malformed frames, quota stops, permission denial, cleanup uncertainty, and disk-full capture failures. Each must yield a distinct bounded explanation while preserving the original outcome. Track the fraction of failed runs with an actionable cause and the time an operator needs to find the failed boundary. A bundle makes diagnosis easier; it cannot reconstruct old payloads that were never saved.

**Owners:** ACP adapter/normalizer, runtime events, session read models, observability trajectory, CLI/dashboard transports.

## 2. A frozen minimal baseline and incident regression corpus

**Observed upstream.** Mini's small coordinator, replaceable model/environment protocols, and explicit per-instance benchmark outputs make experiments relatively easy to interpret. Its batch selection supports filters, slices, deterministic shuffle, and bounded workers. Sources: [protocols](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/__init__.py), [batch runner](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py).

**Why this helps us.** Garuda already has ablations and paired reports. The missing discipline is to make every proposed tool or prompt improvement compete against a frozen, easy-to-explain baseline. Otherwise additional features can consume more turns and tokens while appearing successful on a few memorable tasks.

**Recommended implementation.** Add an evaluation-only minimal configuration using the existing loop, stateless environment, a small tool surface, and explicit task/config/model revisions. Keep workspace, permission, and acceptance policies fixed between baseline and candidate. An unrestricted external mini run is a separate reference system, not a like-for-like test of one Garuda feature.

Build synthetic incident cases from our own failures: ignored configuration keys, reviewer commands refused by policy, metadata written during no-edits steps, truncated tool arguments, stale edits, unmatched execution intents, quota races, and wrong cron installation. Sanitized fixtures should reproduce mechanisms without copying private sessions.

**Validation and effectiveness.** Begin with scripted models and fake adapters; these need no provider budget. Assert state transitions, actual tree changes, receipt attribution, and explicit missing evidence. Optional funded model pilots should compare the same tasks and conditions, reporting unknowns and inconclusive results. Do not treat one stochastic run as a measured uplift.

**Owners:** existing ablation, contract/harness matrices, dual-model report machinery. Avoid copying the ablation runner's permissive local setup into production admission.

## 3. Snapshot-bound edits and model-appropriate edit contracts

**Observed upstream.** Current OMP hashline editing uses a tagged file snapshot plus line/block operations. It records visible ranges, validates anchors, and can recover some stale edits. Other supported contracts include replacement and patch formats. This is more elaborate than the original per-line-hash explanation. Sources: [edit contract](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools/edit.md), [snapshot store](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/crates/pi-edit/src/store.rs), [seen-line regression](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/packages/coding-agent/test/read-edit-seen-lines.test.ts).

**Why this helps us.** Garuda's matcher already handles several common formatting mistakes. Its meaningful additional protection would be knowing which content the agent observed and refusing an edit against a different version. This matters when a human, formatter, or another worker changes the same file.

**Recommended implementation.** Start with an optional expected-content digest on the existing edit path. Associate it with the workspace/session/path and the bytes actually read. Initially refuse stale content rather than add heuristic recovery. Then evaluate an anchored format as a separate opt-in contract, with matching read rendering and model prompt. Keep the existing matcher available.

Use an internal full digest or equivalent reliable version identity; a short display tag is not authorization or a security primitive. Reads and writes must use the selected Environment, with an explicit backend contract for atomic compare-and-write. A Python read followed by write cannot eliminate races with external writers merely by checking a hash first.

**Validation and effectiveness.** Test stale content, wrong-file tokens, repeated lines, hidden ranges, Unicode, EOL handling, and concurrent updates. Measure correct edits per attempt, rereads, retries, total tokens, and independently checked task results. Reject silent mis-targeting even if it saves tokens. OMP itself documents partial multi-file writes after OS failures; do not describe its edits as universally atomic.

**Owners:** file/read tools, edit/multi-edit, Environment protocol, shared setup and tool runner.

## 4. Structural source reads with explicit omissions

**Observed upstream.** OMP can replace eligible source bodies with structural summaries, expose declarations, and give concrete ranges for omitted content. Its summary cache keys include freshly read content and fold settings. Small files, explicit ranges, and unsupported parses can use ordinary reads. Sources: [read behavior](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools/read.md), [summary implementation](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/packages/coding-agent/src/tools/read-summary.ts).

**Why this helps us.** Garuda's ordinary reader defaults to 2,000 lines before downstream shaping. Large modules can consume context before the agent knows which function matters. A declaration outline helps exploration while preserving targeted follow-up reads.

**Recommended implementation.** Add an explicit outline mode first, using Python's standard AST for Python files. Return qualified definitions, signatures, import sections, original line ranges, and an unambiguous omission notice. Expand to other languages only when parser packaging is justified. Keep prose and explicit line reads unchanged by default.

Tie the outline to a content digest. Displayed signatures do not mean a hidden function body was inspected, and an outline must never be accepted as evidence that a test body or security check does what its name suggests.

**Validation and effectiveness.** Cover decorators, nested definitions, multiline strings, syntax errors, generated files, and source changes. Measure input tokens and total exploration turns together with successful localization and evidence coverage. Falling tokens with worse investigation is a regression.

**Owners:** ReadFileTool, optional parser service, context output shaping. This is native-tool work; external harnesses retain their own readers.

## 5. Language-server navigation, followed by semantic refactoring

**Observed upstream.** OMP offers definitions, references, symbols, hover, diagnostics, rename, file rename, code actions, and capability inspection. Its implementation distinguishes read operations from writes and manages diagnostic freshness. Sources: [LSP tool](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools/lsp.md), [LSP configuration](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/lsp-config.md), [freshness regression coverage](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/packages/coding-agent/test/tools/lsp-diagnostics-freshness.test.ts).

**Why this helps us.** Text search finds spellings; a language server can identify the symbol those spellings refer to. This is useful for inheritance, aliases, re-exports, overloaded names, and refactors across files. Garuda's immediate syntax/Ruff feedback is valuable but does not provide these operations.

**Recommended implementation.** Stage one: definitions, references, symbols, hover, and diagnostics for a small supported server set. Stage two: previewed, version-bound WorkspaceEdit application for renames and selected code actions.

A language server is executable project-aware software. Trust its command/configuration through existing owners, launch it inside the selected workspace boundary, cap resources and output, and prove cleanup. Calling an LSP request “read-only” does not prove the server writes no caches. Raw requests and execute-command code actions can run arbitrary behavior and require the corresponding authority.

**Validation and effectiveness.** Test stale diagnostics, unsupported methods, URI/path escapes, UTF-16 positions, server crashes, cancellation, outside-workspace edits, and changed files between preview/apply. Judge task benefit through navigation accuracy and successful checked refactors, including startup latency. LSP diagnostics remain advisory unless an authorized acceptance check establishes verification.

**Owners:** dedicated shared language service, tools, workspace/process lifecycle, shared setup; never independent implementations in CLI and dashboard.

## 6. Previewed structural rewrites

**Observed upstream.** OMP's AST edit tool plans rewrites, presents a preview, and queues an explicit apply/discard operation. Its documentation identifies both stale-plan checks and limits: write failures can leave earlier files changed, and separate target passes do not form one universal transaction. Source: [AST edit contract](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools/ast-edit.md).

**Why this helps us.** Repetitive API migrations are awkward with independent text replacements. A structural rule can distinguish a function call from the same text in a comment or string. A preview gives the agent and user a chance to catch an overbroad rule before applying it.

**Recommended implementation.** Treat this as a dedicated change-plan capability: allowed file set, before digests, bounded replacement count, proposed diffs, and a plan identity. Apply only that plan after ordinary permission checks and revalidation. Do not inherit an effectively unlimited rewrite count.

Start with dry-run planning and narrow language support. Define failure behavior honestly: per-file atomicity is different from a cross-file transaction. If rollback cannot be proved, retain an explicit partial-application manifest and require reconciliation.

**Validation and effectiveness.** Include overbroad patterns, malformed syntax, overlaps, changed targets, cancelled writes, and partial filesystem failure. Compare correctly transformed sites, unrelated churn, and post-change checks against current multi-edit/shell approaches.

**Owners:** edit-planning service and Environment writes. AST matching cannot replace a language server's semantic understanding.

## 7. Review packets that reviewers can actually use

**Observed upstream.** OMP exposes prioritized review workflows and review annotation; mini's trajectories keep actions and observations together. Garuda already has strict review verdicts and severity, so another ranking vocabulary alone adds little. Sources: [OMP review surface](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/README.md), [annotation internals](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/slash-command-internals.md).

**Why this helps us.** A recent reviewer stopped because a needed command was denied. Another review approved a configuration key that was not consumed. A sufficient packet can reduce dependence on denied Git commands and show evidence of behavior instead of only a plausible patch.

**Recommended implementation.** Before launching review, freeze a bounded packet containing task/acceptance criteria, base and candidate identities, changed-file manifest, diff, relevant source/config excerpts, declared checks with actual receipt status, unresolved risks, and effective reviewer tool permissions. Existing snapshot/evidence owners should produce it.

Extend findings through a versioned schema: severity, path/range, explanation, evidence reference, suggested verification, and optional confidence. Confidence is the model's assertion, not a measured probability. Preserve the strict legacy parser until explicit schema migration; do not guess at malformed output.

**Validation and effectiveness.** A no-edits reviewer should complete a meaningful fixture review without mutating Git metadata. Known ignored-config and misleading-test fixtures must remain catchable. Measure invalid review outputs, permission-blocked reviews, actual findings confirmed by reproduction, and repair rounds. Show inherited draft provenance separately from launched runtime/model independence.

**Owners:** flow artifacts, review admission/parser, workspace snapshots, acceptance receipt readers.

## 8. Typed child results before more parallel editing

**Observed upstream.** OMP supports per-invocation output schemas, structured result metadata, retrievable JSON sidecars, and optional isolated child workspaces. Strictness matters: its permissive schema mode can return an invalid result with a warning after retries. Sources: [task interface](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools/task.md), [shared child policy](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/packages/coding-agent/src/task/structured-subagent.ts).

**Why this helps us.** Garuda can validate an agent's final JSON, but the ordinary delegation wrapper renders a textual summary rather than forwarding AgentResult.output as a typed child result. Its changed-files summary also infers only selected write-tool calls. A structured handoff would reduce reparsing and make coverage gaps visible.

**Recommended implementation.** First return validated data, schema identity, producer session, relevant workspace version, artifact references, status, and coverage alongside human-readable text. Derive changed files from workspace evidence rather than tool-name guesses.

Only then consider concurrent editing children, each with its own workspace lease and integration/check receipts. Existing parallel flow groups are read-only snapshot workers; they are not an editing-worker scheduler. Concurrent work needs real ownership and merge lifecycle design.

**Validation and effectiveness.** Reject invalid payloads as automation inputs; cover failed children with partial output, missing artifacts, stale results, and schema mismatch. Later test conflicting patches, parent cancellation, and uncertain cleanup. Measure parent re-reading and interpretation errors before pursuing parallel speed claims.

**Owners:** subagent runner/tool, existing output-schema validator, typed artifacts, workspace/capacity/integration services.

## 9. Evidence-linked memory with review and freshness

**Observed upstream.** OMP's optional local memory pipeline extracts session lessons, consolidates them, and injects a bounded project summary. Its guidance explicitly treats memory as heuristic and asks for current-repository evidence before acting. Source: [memory behavior](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/memory.md).

**Why this helps us.** Useful lessons from the past week should be easy to retrieve: a given profile field was ignored, one adapter writes startup metadata, or a particular review needs a richer packet. Re-reading every session is expensive, but copying an old conclusion into permanent instructions can preserve a mistake.

**Recommended implementation.** Extend Garuda's reviewed-note model with source session/receipt references, affected runtime/config versions, repository identity, last-confirmed evidence, and supersession/expiry. Search accepted notes on demand with a small injection budget. Begin with explicit proposals and deterministic deduplication; automated extraction can later generate proposals for review.

Retain Garuda's acceptance boundary. No background process should silently rewrite durable .context files, skills, or standing instructions. Memory does not authorize commands, prove quota availability, or override current source.

**Validation and effectiveness.** Test source deletion, stale versions, contradictory notes, secret-shaped content, cross-project access, and user rejection. Measure whether relevant accepted notes are retrieved and reduce repeated investigation without stale advice causing wrong actions.

**Owners:** context notes, proposal review, prompt plan, project identity. A remote vector store is not required for the first useful version.

## 10. A bounded asynchronous advisor

**Observed upstream.** OMP can run a separate advisor on transcript deltas and inject severity-labelled advice. Its source includes an emission guard against duplicate, empty, noisy, and excessive notes; the code documents a real advice-flood incident. Sources: [advisor behavior](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/advisor-watchdog.md), [emission guard](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/packages/coding-agent/src/advisor/emission-guard.ts).

**Why this helps us.** A reviewer that notices an ignored config option or missing ownership invariant before a long build finishes could avoid wasted work. Reviewing every turn, however, creates extra cost, quota exposure, and potentially distracting advice.

**Recommended implementation.** An opt-in, read-only advisor at meaningful boundaries: plan completion, candidate ready, repeated tool failures, or unusually broad diff. Reuse existing role identity, consult, budget, and steering mechanisms. Require concrete evidence-bearing notes, deduplicate them, cap notes and total spend, and queue delivery between turns.

Advisor output cannot grant permissions, approve verification, bypass independent final review, or resume a user-cancelled run. OMP permits richer advisor tool grants; Garuda's first version should keep the investigative ceiling narrow.

**Validation and effectiveness.** Seed defects into representative changes and measure actionable early catches, false alarms, advice ignored/accepted, repair savings, added latency, and total cost. Include stopped sessions, late results, compaction, quota exhaustion, and advisor crashes. Retain this as optional unless its net benefit is demonstrated.

**Owners:** review/consult service, role admission, native steering, usage accounting. ACP turn-level observation may be incomplete.

## 11. Conditional guidance at safe boundaries

**Observed upstream.** OMP's stream-rule mechanism can watch configured text/tool streams, interrupt generation, and inject guidance. Current rules can also use AST conditions over reconstructed source-bearing arguments; those snapshots do not necessarily contain the whole resulting file. Source: [TTSR lifecycle](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/ttsr-injection-lifecycle.md).

**Why this helps us.** Short task-specific reminders need not all occupy every prompt. A reminder such as “this config field must appear in effective settings” becomes useful when the agent changes configuration.

**Recommended implementation.** Start with deterministic triggers on complete proposed tool arguments or observed events using existing before-tool hooks and queued steering. Match a small trusted rule set, identify the rule/source, inject at most a bounded number of times, and record the trigger.

Treat this as guidance. Garuda's permission and verification enforcers must still act even if no trigger fires. Avoid mid-stream cancellation initially: partially generated calls, repeated injections, accounting, and provider replay make it much harder to prove safe behavior.

**Validation and effectiveness.** Test false positives, repeated matches, untrusted rules, compaction, and a deliberately missed trigger. An unsafe action must still be denied by its ordinary owner. Measure avoided repeated mistakes and added turns; a reminder that fires constantly belongs in simpler standing guidance or a real validator.

**Owners:** hooks, steering, configuration trust. Arbitrary regex/AST rules are executable work and require bounded evaluation.

## 12. Precise stop reasons, failure accounting, and retry decisions

**Observed upstream.** Mini distinguishes format errors, limits, time exhaustion, submission, and interruptions. It threads output-limit finish reasons into parser feedback and charges reported cost on a failed parse. OMP separates overflow maintenance from ordinary retry and requires authoritative timing for certain long usage waits. Sources: [mini exceptions](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/exceptions.py), [truncation regressions](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/tests/models/test_truncation_finish_reason.py), [OMP retry policy](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/non-compaction-retry-policy.md).

**Why this helps us.** Operationally different failures need different next actions. A weekly quota stop, transport 429, invalid review, user denial, incomplete output, and uncertain process cleanup must not all look like retryable model failure.

**Recommended implementation.** Keep existing recovery guards and add a common projection of original cause, phase, attempted action, retry eligibility, evidence source, next check, and safe operator action. Audit that parsing failures, auxiliary calls, interrupted streams, fallback attempts, and children retain known reported usage once. Unknown usage/cost must remain unknown.

Improve continuation visibility with persisted last tick attempt/result and health distinct from installed/enabled status. Preserve local failure details alongside later recovery refusals rather than overwriting them.

**Validation and effectiveness.** Use scripted failures and fake clocks to prove bounded retries, no replay after uncertain side effects, preserved review budgets, cancellation, and cost deduplication. Track unclassified stops and repeated futile attempts.

This does **not** add Claude quota recovery, broaden version proofs, resume external chats, or make protected flows eligible. Those require separate source and lifetime evidence. OMP's retry and credential-handling strategies are not authority to read or rotate vendor OAuth tokens in Garuda.

**Owners:** model transport, accounting, normalized runtime outcomes, flow/quota recovery, scheduler diagnostics.

## 13. Uniform artifact completeness and retrieval

**Observed upstream.** OMP separates binary content-addressed blobs from session-local text artifacts. Its output sink can distinguish successful capture, sampled capture, and failed capture; integrity tests reject short writes before publishing a usable artifact. Sources: [storage architecture](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/blob-artifact-architecture.md), [integrity tests](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/packages/coding-agent/test/artifacts-integrity.test.ts).

**Why this helps us.** Garuda already stores large outputs and has digest-checked flow artifacts. The useful extension is a shared way to tell whether a referenced log is complete and still available. A clipped display must not imply that the full source was persisted successfully.

**Recommended implementation.** Define a common read projection for artifact identity, producer, digest/size, media type, redaction, capture completeness, omitted byte count, retention, and availability. Make it usable from failure bundles, child results, flow packets, and the dashboard without replacing their distinct storage owners.

Start with read-only references. A future artifact/session URI can reduce tool-learning overhead, but resolving it must authorize the selected session/project and preserve source classification. A generic filesystem-looking interface must not grant broad writes.

**Validation and effectiveness.** Test short writes, missing/pruned buffers, oversized samples, cross-session references, corrupt digests, symlinks, and concurrent allocation. Do not advertise an incomplete artifact as full output. Measure how often investigations can retrieve the exact necessary output and how often capture loss is explicit.

**Owners:** buffers, flow artifacts, session storage/read models, context retrieval. Global binary deduplication is optional until storage data justifies it.

## 14. Programmatic orchestration over existing tools

**Observed upstream.** OMP's eval surface supports retained Python/JavaScript execution, tool callbacks, structured data, background jobs, and artifact-backed output. Its documentation makes clear that a subprocess/worker runtime can still have filesystem and network effects. Source: [eval contract](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools/eval.md).

**Why this helps us.** For data-heavy analysis, the agent could search many bounded results, parse them, aggregate findings, and return only relevant facts. This can avoid sending large intermediate tables repeatedly through the model.

**Recommended implementation.** First evaluate a short-lived, confined execution cell with structured inputs/results. Persistence should be optional and scoped to an explicit owner. Every callback must resolve through Garuda's ordinary ToolRunner, retaining target permissions, hooks, output bounds, events, and accounting.

Direct language file/network APIs still need workspace confinement; routing tool callbacks alone cannot constrain arbitrary Python or JavaScript. A local worker is not a sandbox. Avoid implicit package installation and do not replay a cell after kernel loss when side effects are uncertain.

**Validation and effectiveness.** Test permission bypass attempts through callbacks and direct APIs, cancellation, kernel death, output caps, late callbacks, and shared budget exhaustion. Benchmark only workloads with substantial local data processing; a simple one-file edit is unlikely to repay kernel startup and lifecycle complexity.

**Owners:** a new execution service over Environment and ToolRunner. Existing shell/Python commands already provide a baseline; this feature must justify the extra bridge.

## 15. Debugger access through DAP

**Observed upstream.** OMP can launch/attach debug sessions, set breakpoints, inspect stacks/scopes/variables, step, evaluate expressions, and terminate session trees. Its approval treatment differs by action. Source: [debug tool](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools/debug.md).

**Why this helps us.** Some defects are much easier to explain from runtime state: a deadlock stack, a null pointer, or a value changed by a callback. Repeated print instrumentation can alter timing and leave cleanup work.

**Recommended implementation.** An optional DAP adapter service limited initially to agent-launched debuggees inside the workspace boundary. Offer breakpoints, stack/variable inspection, stepping, and bounded output first. Process attachment, arbitrary evaluate, raw adapter requests, and memory writes require separate explicit authority.

A variable getter or debugger expression can execute code, and “inspect” is not always side-effect-free. Debugger support also cannot guarantee reproduction of a timing-sensitive failure.

**Validation and effectiveness.** Use known deterministic failing programs, adapter disconnects, child processes, cancellation, stale frame IDs, and attempted attachment to unrelated processes. Measure time to a correct diagnosis, transient debug edits, and checked fixes. Keep this workload-specific until Garuda's task mix shows enough native-code or stateful-service debugging.

**Owners:** dedicated debugger/process owner and native tools, not the inference Model protocol.

## 16. Investigation checkpoints and session forks

**Observed upstream.** OMP supports context checkpoints/rewind, branching, and forks. Its checkpoint/rewind documentation explicitly says these operations change conversation context and do not restore the working tree or processes. Sources: [checkpoint](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools/checkpoint.md), [rewind](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/tools/rewind.md).

**Why this helps us.** Long investigations accumulate discarded hypotheses. An explicit investigation boundary can preserve a concise evidence report while keeping the detailed trail accessible, making later implementation context easier to use.

**Recommended implementation.** Start with a read-only investigation scope that yields a report containing source references, failed approaches, conclusions, and coverage. Keep the authoritative event trail append-only; change only what context the next model call sees. Retain the deterministic working-state card, checks, side effects, and current instructions.

A fork must get a new session identity and acquire its own workspace authority before running. Old verification becomes historical evidence tied to its candidate. Never imply that switching context restores files or gives permission to rerun completed operations.

**Validation and effectiveness.** Test compaction, unresolved tool calls, changed workspace, cancellation, and missing artifacts. Measure retained fact accuracy, repeated exploration, and token/turn cost. Prefer existing collection briefs if they solve the same task with less machinery.

**Owners:** context manager/condenser, session linkage, briefs, workspace admission.

## 17. Live worker inspection and steering

**Observed upstream.** OMP's Agent Hub displays live and persisted workers, transcripts, usage, and lifecycle controls. It distinguishes advisors from steerable workers. Source: [Agent Hub](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/agent-hub.md).

**Why this helps us.** “What is going on?” is a real workflow need. Garuda already has sessions, flow steps, approvals, and child logs, but the operator should be able to see who owns work, what it is waiting for, and whether progress evidence is recent.

**Recommended implementation.** Extend the existing dashboard with parent/child lineage, current admitted role/runtime, latest known operation, duration, budget, waiting reason, trace coverage, and links to outputs/checks. Distinguish unknown state from dead or stuck state.

Add steering only through the existing runtime lifecycle owner. A dashboard revive action cannot bypass lease/capacity claims, quota identity checks, cancellation, or unfinished-intent reconciliation. Reading a worker must never resume it.

**Validation and effectiveness.** Exercise read-only viewers, reconnecting streams, missing children, stale heartbeats, simultaneous control requests, and cancelled sessions. Measure operator time to find an active failure or explain a wait, rather than claiming the UI makes the model smarter.

**Owners:** session read model, child lineage/events, existing dashboard and runtime control paths.

## 18. Instruction compatibility and tested model interaction contracts

**Observed upstream.** OMP discovers multiple instruction/rule conventions and supports per-model edit variants. Mini separates action parsing/observation formatting from its coordinator and offers both text and native tool-call models. Sources: [context discovery](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/docs/context-files.md), [edit settings](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/packages/coding-agent/src/edit/settings.ts), [mini action parsing](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/models/utils/actions_toolcall.py).

**Why this helps us.** Existing repository guidance reduces onboarding work, and models can differ in how reliably they produce a given edit/response format. Garuda already has agent definitions, profile migration, prompt provenance, and provider transports; those are the right extension points.

**Recommended implementation.** Begin with an inspection-only compatibility report: discovered files, scope, precedence, unsupported fields, duplicates, and proposed mapping to Garuda sections. Import instruction text separately from executable hooks, tools, MCP servers, or permissions. Existing foreign settings cannot silently become trusted launch authority.

For model interaction, pin a small tested contract: exact model identity, parser/prompt version, tool schema, edit format, structured-output support, and output-limit behavior. Show the resolved contract in agent inspection and trace metadata. Prefer exact selectors and explicit fallback over unbounded substring rules.

**Validation and effectiveness.** Test conflicting instruction sources, nested scopes, symlink escapes, unsupported keys, malformed arguments, and provider capability mismatches. Use the actual loader and dispatch path, addressing the kind of ignored-field failure our review found. Compare parse/edit reliability by model before introducing broad automatic switching.

**Owners:** agent resolution/compile/prompt inspection, model transports, trusted configuration. Keep external harnesses behind AgentRuntime.

## 19. Try OMP through ACP before porting its whole tool stack

**Observed upstream.** OMP has a stdio ACP entrypoint that isolates protocol stdout and waits for session teardown. Garuda already has a generic ACP runtime catalog. However, Garuda's existing pi entry launches pi-acp; that is not proof of compatibility with OMP. Sources: [OMP ACP implementation](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/packages/coding-agent/src/modes/acp/acp-mode.ts), [Garuda Pi manifest](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/acp/builtin/pi.json).

**Why this helps us.** A compatible OMP runtime could let Garuda users try its coding tools while Garuda keeps cross-runtime sessions and workflow orchestration. It is a smaller experiment than reimplementing its Rust/Bun toolchain in Python.

**Recommended implementation.** Conduct a version-pinned, no-prompt handshake/capability study first. Register a separate runtime identity if production integration proceeds. Prove option selection, permission behavior, cancellation, cleanup, no-edits/confinement behavior, usage reporting, and error normalization before advertising support.

Never assume OMP's editor integration exposes all its tools through Garuda's current ACP subset. Account for its runtime dependencies and settings; an authenticated official CLI owns its credentials. Do not import vendor OAuth stores.

**Validation and effectiveness.** A small separately authorized prompted fixture can establish actual tool lifecycle behavior after the unprompted conformance work. Compare checked outcomes and usable evidence on matched tasks, not feature counts.

**Owners:** AgentRuntime catalog/adapters and conformance tests. External completion remains distinct from Garuda's native completion gate. Garuda already supports explicit user-authorized post-run acceptance checks on standalone ACP runs; connectivity alone does not select checks or prove acceptance. Those checks do not confine the external harness or make check-protected flows eligible for automatic quota recovery.

**Follow-up clarification:** the [existing feature comparison](2026-10-26-existing-feature-comparison.md) exercised the standalone ACP check path and corrects the earlier wording that implied external acceptance verification required a new mechanism.

## Features to postpone or deliberately avoid copying

| Idea | Why it is interesting | Why it is not an immediate Garuda investment |
| --- | --- | --- |
| Native Rust shell/search utilities | Potential portability and lower process overhead | Model latency, tool time, and platform failures must show a real bottleneck first; rewriting working Python/RG boundaries is expensive |
| Browser and desktop automation | End-to-end visual interaction | Broad new authority and process/network lifecycle; use existing approved integrations before a bespoke subsystem |
| Live relay collaboration | Shared human sessions | Authentication, transport, access separation, and deployment form a separate product; improve local inspection first |
| Automatic commit splitting | Cleaner reviewable changes | Partial staging and dependency ordering can damage user-owned index/worktree state; retain explicit reviewable Git operations |
| Automatic memory-to-skill generation | Reuses workflows | Risks promoting an unverified conclusion into executable or standing instructions |
| Image-based or provider-native opaque compaction | May retain more historical information | Model-specific cost/fidelity/replay tradeoffs; not a substitute for portable evidence and bounded context |
| Text-parsed shell actions as the production default | Simple compatibility baseline | Native tool calls already work; another parser needs an demonstrated workload benefit |
| Shell-only permission simplification | Fewer tool schemas | Arbitrary shell execution still needs confinement and policy; fewer tools do not imply a smaller authority surface |
| Credential pools, OAuth import, automatic token rotation | Can reduce interruptions in some harnesses | Conflicts with Garuda's explicit credential boundary |
| Permanent every-turn advisors | Early catches | Added spend and quota use, noisy feedback, and surprise continuations can outweigh avoided repairs |

Mini's v2 migration also documents removal of a more complex full-screen interface after insufficient adoption. That is a useful product lesson: deletion and simplification can be improvements. We should assess usage before adding a parallel UI. Source: [migration removals](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/docs/advanced/v2_migration.md).

The published OMP editing gains and mini SWE-bench scores are worth investigating, but they are not transferable estimates. Model, task set, edit version, budgets, environment, and grading all differ from Garuda. In particular, do not apply old hashline results directly to today's expanded OMP contract or our existing recovery-aware matcher.

## Which runs would benefit

This distinction matters for the subscription-backed flows in our recent audit. Improving a Garuda-native tool does not change the tools inside Claude Code or Codex. Their harnesses own those operations.

| Investment | Native Garuda runs | External ACP runs |
| --- | --- | --- |
| Failure bundles, stop projection, artifact coverage | Direct benefit | Direct benefit where evidence is exposed; missing payloads stay missing |
| Review packets and incident fixtures | Direct benefit | Useful to flow reviewers and adapter conformance; no new native verification guarantee |
| Native edit/read/LSP/codemod tools | Direct benefit | No automatic effect on the external harness's tools |
| Typed children and native advisor | Direct benefit | Requires a separately proved external lifecycle/observation path |
| Reviewed memory retrieval | Direct prompt integration | Can contribute to an explicitly selected bounded handoff; no access to hidden vendor memory |
| Distinct OMP ACP runtime | Alternative execution engine | Potential way to access upstream capabilities after conformance proof |
| Recovery/tick health | Better visibility into supported flows | Same current Codex eligibility and version/account guards |

For improving our current ACP-heavy self-improvement workflow, prioritize the shared evidence/review/stop work in Batch A. Treat native coding tools as an investment in Garuda's own agent, and OMP integration as a separate way to try another harness.

## Three concrete examples of expected value

**An ignored configuration option.** Today a plausible patch and an approving model review can still miss that a loader ignores a field. With the proposed incident corpus and review packet, the coder must show the effective configuration produced through the real loader; the reviewer receives that evidence and its candidate identity. This directly addresses an observed failure mechanism without needing a more expensive reviewer model.

**A rename across aliases and re-exports.** Today the native agent searches spellings, edits several files, and discovers missing references through tests. With LSP navigation it can locate semantic references, preview a bounded WorkspaceEdit, revalidate the affected versions, apply it through the normal workspace owner, and run authorized checks. The expected gain is fewer missed references and repair turns. The checked result, startup overhead, and unrelated churn decide whether it actually helped.

**A quota stop during review.** A coder's output is already receipted, while its reviewer is waiting. Better stop evidence tells the operator which stage is pending, why it is eligible to wait, what quota source was observed, and when the scheduler last checked. A complete review packet lets the eventual reviewer continue without repeating the coder. Garuda's current eligible-flow recovery already preserves completed steps; the proposed improvement is diagnosis and evidence availability, not a new claim that every stopped run can be resumed.

## How to establish whether a feature is effective

Separate three questions:

1. **Does the mechanism work?** Deterministic boundary cases can prove that a stale edit refuses, an invalid child result is withheld, or a failure bundle retains the original cause.
2. **Is it wired into real entry points?** Drive the installed CLI/SDK/service path, effective configuration loader, selected Environment, and shared owners. A helper-only test cannot establish this.
3. **Does it improve task outcomes economically?** Only representative comparable trials can establish this, accounting for the whole trajectory and uncertainty.

### Measurement plan that respects the current budget

Garuda's backlog explicitly records the absence of a repeated paid benchmark budget and the instability of single-trial scores. Therefore the first two stages below can proceed without provider calls. Stage three is a future experiment requiring a separately chosen budget, not an assumption in this report.

| Stage | Work | What it can establish |
| --- | --- | --- |
| Offline contract cases | Synthetic incidents, scripted model responses, fake adapters, controlled filesystem/process failures | Specific mechanism correctness and regressions |
| Local integration fixtures | Actual parser, installed command paths, bounded local tools; optional live environment integrations | Wiring, dependencies, cleanup, and platform behavior |
| Optional bounded model pilot | Matched task/model/config conditions with explicit caps | Preliminary quality/cost/latency evidence; small samples may remain inconclusive |
| Expanded rollout evidence | Representative usage and repeated trials if funded | Whether defaults or broader availability are justified |

Use existing paired-report provenance where applicable: task version, source revision, exact model/runtime versions, prompt/tool contract revision, environment/image identity, price source, selected checks, and configuration digest. Native and external comparisons must disclose differences in authority, hidden system prompts, and available telemetry.

### Measures that matter

| Measure | Why | Common misleading substitute |
| --- | --- | --- |
| Independent acceptance outcome | Establishes task result under authorized checks | Agent says completed or reviewer approves |
| Total known cost, unknown-cost coverage, tokens | Includes planner, tools, summaries, advisor, children, retries | Main model tokens alone |
| End-to-end time including setup/waits | Represents user experience | Sum of overlapping tool durations |
| Correct edit per attempt and stale refusal | Detects interface friction and unsafe targeting | Number of edits accepted |
| Evidence completeness and artifact availability | Establishes what a later review can actually inspect | Event count or artifact path exists |
| Review findings confirmed by evidence | Separates useful catches from noise | Volume of critique |
| Operator intervention and diagnosis time | Measures operational robustness | Automatically retried every failure |
| Unrelated file churn and uncertain cleanup | Detects hidden damage and unfinished authority | Tests passed once |

For a funded tool pilot, a possible predeclared target is a noticeable reduction in total tokens or wall time, such as 15%, with preserved independently checked outcomes and evidence coverage. This is an illustrative decision threshold, not a predicted result or an adopted Garuda release gate. Existing dual-model release thresholds remain specific to that feature.

Any wrong-file mutation, permission widening, false verification, replay of uncertain work, or unproved ownership is a correctness failure. A performance win cannot offset it. Small samples cannot establish a general “no quality regression” claim.

## Recommended delivery sequence

### Batch A: make current workflows explainable

Deliver IDs 1, 2, 7, 12, and 13 as focused changes: causal failure projection, a small incident corpus, sufficient review packets, explicit stop/budget evidence, and artifact completeness.

A useful first slice is a read-only failure view over existing evidence plus deterministic fixtures for generic ACP error, ignored config, denied reviewer command, and capture loss. Missing historical information must remain visibly missing. Expand diagnostic capture only through an explicit bounded policy.

This addresses the failures already seen in Garuda with comparatively little new execution authority. Successful completion means that an operator can identify the original failed boundary, distinguish review from verification, and retrieve the evidence that actually exists.

### Batch B: improve the native coding interface

Deliver ID 3's expected-content check and ID 4's Python outline as separate opt-in experiments. Follow with ID 5's read-oriented LSP service. Treat semantic renames and ID 6's codemods as later mutation features after lifecycle/version checks work.

Add ID 8's typed child handoff before parallel editing. In parallel at the project-planning level, ID 19 can receive a small ACP feasibility study; no OMP support claim is warranted yet.

This sequence lets us observe whether better local tools reduce retries and exploration cost without making recovery or ownership more complicated first.

### Batch C: long-session and specialist capabilities

Choose among memory retrieval, advisor checkpoints, conditional guidance, investigation scopes, and worker controls based on actual recurring friction. Add a kernel or debugger only when the relevant task mix justifies its lifecycle cost.

Do not bundle all these into one “robustness” PR. Each introduces a different boundary and needs its own scoped design, acceptance criteria, and independent rollback/disable path.

## Adoption routes and architectural constraints

There are three viable approaches:

| Approach | Benefit | Cost/tradeoff | Recommendation |
| --- | --- | --- | --- |
| Selective native improvements | Works through Garuda's own tools and evidence model | We own implementation and maintenance | Primary route for diagnostics, edits, reads, review packets |
| Distinct external OMP runtime | Faster access to upstream tool investment | External loop/authority; compatibility and telemetry limits | Run a bounded feasibility study |
| Whole-stack port or rewrite | Maximum apparent feature parity | Duplicates mature systems and imports substantial lifecycle debt | Do not pursue |

Every native capability should pass through shared setup and ToolRunner with its existing permissions, hooks, effect classification, output handling, and events. New process-backed services require workspace-aware ownership, cancellation, and cleanup. An external coding harness remains behind AgentRuntime, not Model.

Preserve these distinctions throughout implementation:

- Model inference transports versus external agent runtimes.
- Advisory diagnostics/reviews versus authorized verification.
- Saved context versus execution replay.
- Workspace guardrails versus actual confinement.
- Durable reviewed knowledge versus session-local generated state.
- Known measured usage versus estimates or unavailable usage.
- Scheduler installation versus demonstrated tick health and eligible pending work.

The existing hourly Codex recovery can continue to serve eligible future flows. This study did not change its schedule, enroll work, or launch a self-improvement campaign. More capable tools do not make a stopped or uncertain operation automatically safe to resume.

Both reviewed projects declare MIT licensing ([OMP](https://github.com/can1357/oh-my-pi/blob/553f0d10e813a5e1fb65e545efe7c814bc03a879/LICENSE), [mini](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/LICENSE.md)). No source was copied into Garuda for this study. If implementation later reuses code, preserve applicable notices and check the specific package/dependency licenses.

## Source inspection trail

The most important cross-checks beyond the README claims were:

| Question | Files inspected | Consequence for the recommendation |
| --- | --- | --- |
| Is current hashline just per-line hashes? | OMP edit docs, edit settings, Rust snapshot store and mismatch handling | Recommend content/version binding first; do not copy an outdated wire contract |
| Does summarization really constrain unseen edits? | OMP structural-read implementation and read/edit seen-lines regression | Couple outline adoption with truthful visibility evidence |
| Is AST preview universally atomic? | OMP AST edit execution/failure documentation | Specify partial-application behavior instead of claiming cross-file transactions |
| Does an advisor always help? | OMP advisor runtime guidance and emission guard | Budget and deduplicate advice; test cancellation and late delivery |
| Does rewind undo code? | OMP checkpoint and rewind implementation documentation | Separate model-context changes from workspace restoration |
| Are artifacts always full logs? | OMP output-sink architecture and short-write regressions | Track sampling, capture failure, retention, and missing data |
| Is mini still text-only? | mini v2 migration, toolcall parser, current CLI configuration | Evaluate native/tool-text contracts explicitly; avoid old assumptions |
| Are failures saved and charged? | mini DefaultAgent, parser-persistence tests, truncation tests | Keep diagnostic and usage evidence across failed parses |
| Are shell steps stateful? | mini LocalEnvironment; Garuda LocalEnvironment and optional shell | Use existing stateless execution as the baseline; do not build a duplicate |
| Does Garuda lack structured results? | Garuda output_schema, subagent wrapper/formatter, flow artifacts | Improve typed handoff propagation rather than add another validator |
| Does Garuda lack memory or context compaction? | Garuda notes, module map, working-state/context architecture | Extend reviewed retrieval and context traceability |
| Can existing pi support stand in for OMP? | Garuda pi manifest; OMP ACP entrypoint | Require a distinct version-proved integration |
| Can gains be assumed from the last week's runs? | Garuda workflow audit, backlog, paired evaluation guide | Use deterministic incident proofs first; keep performance claims unmeasured |

This report recommends capability investments and validation paths. It does not add those items to the open backlog as accepted work, change runtime behavior, or claim that implementing all of them would improve Garuda.

