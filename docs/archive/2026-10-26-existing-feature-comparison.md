# Existing Garuda features compared with oh-my-pi and mini-swe-agent

Assessment date: **26 October 2026**.

**Yes, Garuda can improve several features it already has.** The strongest immediate opportunities are reliable tool-output capture, structured child-result delivery, accurate streaming metadata, clearer external-run diagnostics, and budgets covering the entire run.

Garuda already has valuable mechanisms worth preserving: attributed acceptance checks, inherited permission ceilings, scoped reviewed memory, explicit workspace ownership, and refusal to replay uncertain work. OMP offers richer coding interaction and operational surfaces. Mini provides a simpler baseline and a more direct relationship between conversation history and inspectable trajectory.

These are judgments about particular mechanisms. They do not establish that one agent is generally more accurate, faster, cheaper, or more secure.

This report complements the [feature opportunity study](2026-10-26-agent-feature-study.md). It evaluates the quality and limits of existing implementations rather than treating every difference as a request for a new feature.

## Evidence and assessment method

| Project | Reviewed source | Notes |
| --- | --- | --- |
| Garuda | 8f6c488335f1220a0aff5f97f76fdbbd7c05d0af | Current local main; documentation-only research changes left uncommitted |
| oh-my-pi | dcafcef6422c6a260649b879d75a5aabb1ea778e | Refreshed from upstream main for this follow-up; previous report used 553f0d1 |
| mini-swe-agent | 04d809ceab9df28f9adaed044884180159172930 | Upstream main unchanged since the previous study |

Source links are pinned to these versions. No authenticated harness, live model prompt, vendor credential file, or paid benchmark was used.

The review assessed five dimensions:

1. **Correctness:** what happens with malformed, stale, interrupted, or partially completed work.
2. **Coverage:** how much of a feature is supported and through which entry points.
3. **Usability:** whether the model and operator receive enough information to act correctly.
4. **Operability:** persistence, cancellation, cleanup, failures, and recoverability.
5. **Evidence:** which claims are supported by source, executable local checks, or only a hypothesis.

**Local validation:** 333 existing focused tests passed in two runs across 21 files. Four additional offline probes exercised actual Garuda implementations with synthetic provider responses, temporary workspaces, and a simulated disk failure. They revealed two correctness weaknesses, one handoff gap, and one expected limitation described below.

Upstream tests were inspected, not executed. The local test result is not a comparative benchmark, and passing tests did not cover the newly demonstrated weaknesses. Absence of a mechanism in the inspected paths is not proof that a project has no extension or alternative configuration implementing it.

No overall numeric score is assigned: a larger feature set can have weaker guarantees, and stricter refusal can make an agent less convenient while protecting a useful invariant.

## Comparison at a glance

“Stronger contract” means the inspected mechanism provides a more explicit guarantee in the named area. “Richer” means broader functionality. Neither means a measured end-to-end win. Detailed evidence and qualifications follow.

| Existing feature | Garuda compared with OMP | Garuda compared with mini | Decision |
| --- | --- | --- | --- |
| 1. Text editing | Useful recovery matcher; less version-aware | Dedicated edit contract instead of arbitrary shell edits | Improve stale-target handling |
| 2. File/document reading | Narrower selectors and structural views | More built-in readers and bounded observations | Improve large-file access |
| 3. Search | Less granular paging/caps and resource coverage | Easier structured discovery than shell-only operations | Improve result/work limits |
| 4. Post-edit diagnostics | Useful but less semantic and project-aware | More automatic feedback in the harness | Retain; improve freshness and coverage |
| 5. Large-output retention | Weaker failed-capture publication and metadata | Better retrievable buffer interface | Repair capture integrity first |
| 6. Context management | Good explicit state preservation; fewer context operations | Better long-session controls; more transformation complexity | Retain; measure before adding strategies |
| 7. Session persistence/inspection | Mixed: strong workflow evidence, poorer ACP payload visibility | More ownership records; less direct ACP transcript inspection | Improve evidence coverage |
| 8. Completion/verification | More explicit attributed acceptance in reviewed paths | Stronger distinction between submission and checked acceptance | Preserve; improve visibility |
| 9. Review flows | Strong identity/receipt discipline; less expressive findings/UI | More built-in review orchestration | Improve reviewer inputs |
| 10. Permissions/approvals | Conservative ceilings; less approval UX flexibility | More fine-grained tool/path policy | Preserve guarantees; reduce avoidable friction |
| 11. Workspaces/process ownership | More conservative uncertain-ownership policy; different isolation goals | More orchestration, more complexity | Preserve; improve operator reconciliation |
| 12. Delegation/structured output | Strict validation but weaker child delivery and concurrency UX | More built-in delegation | Forward typed outputs before adding workers |
| 13. Memory | More explicit human acceptance; weaker retrieval/automation | More built-in reviewed persistence | Improve retrieval, keep review |
| 14. Agent definitions/instructions/skills | Strong provenance/compile checks; less foreign-format compatibility | More controlled resolution and inspection | Improve explanation and compatibility |
| 15. Hooks/customization | Smaller surface; both can block failed pre-tool handling | More supported extension points than subclassing alone | Keep focused; expand only for concrete needs |
| 16. Provider/retry/streaming | Simpler compatibility layer; streaming not fully wired | Broader native runtime machinery, but a demonstrated metadata gap | Fix metadata before enabling loop streaming |
| 17. Cost and spend budgets | Good provenance; lacks equivalent general session-tree dollar gate | Better unknown-cost reporting; narrower main-run dollar-stop support | Extend existing budget ownership |
| 18. MCP | Explicit project trust; narrower lifecycle/resource surface | Built-in MCP support absent from inspected mini core | Improve readiness/lifecycle first |
| 19. Web research | More explicit public-network fetch restriction; less rich extraction/search | Structured safe fetch instead of unconstrained shell tooling | Preserve network policy; improve content fidelity |
| 20. Flow/quota continuation | Conservative, narrow durable workflow contract; fewer broad reset options | Much richer workflow continuation | Improve health/diagnostics, keep no-replay rules |
| 21. CLI/SDK/dashboard | More control-plane surfaces; weaker native live-token/worker experience | Broader interfaces, higher maintenance cost | Improve truthfulness and shared wiring |
| 22. Evaluation | Richer cross-runtime evidence; more setup | Mini is easier to understand as a baseline | Consolidate comparisons; avoid feature-count scoring |

## Findings demonstrated during this assessment

### F1. A failed buffer write can reappear as an ordinary stored buffer

**Observed:** an offline probe made the actual buffer writer write three bytes and then raise an OSError. The immediate store call failed, but a new ToolOutputBuffer instance scanning that directory discovered the three-byte file as an ordinary BufferRef. The record had no capture-failure field.

This follows from direct writes to the final .txt path and resume discovery that reconstructs metadata from whatever matching files exist. The current tool runner catches storage failure and falls back to bounded inline shaping, which protects the immediate tool observation. That does not prevent a later buffer listing from presenting the leftover partial file.

**Comparator:** OMP's artifact writer stages publication and its output sink records incomplete capture rather than advertising complete output.

**Recommended priority: high.** Publish captured buffers only after successful write validation; preserve a truthful incomplete/missing result where appropriate. Retain fallback output. Add one boundary regression covering failed write followed by fresh-instance discovery, rather than only testing the store helper.

**Limit:** this was a synthetic storage failure, not evidence that a particular user's existing buffers are corrupt. Garuda's digest-checked flow artifacts have a different owner and must not be described as sharing this buffer defect.

Evidence: [Garuda buffer](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/buffer.py), [runner fallback](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/tool_runner.py), [OMP artifact integrity tests](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/packages/coding-agent/test/artifacts-integrity.test.ts), [OMP capture model](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/blob-artifact-architecture.md).

### F2. Streaming loses a provider's terminal output-limit reason

**Observed:** the same synthetic provider returned a terminal reason of length through both transport shapes. Garuda's ordinary complete() returned raw.finish_reason equal to length. Its complete_streaming() returned the same text but no finish_reason. StreamDelta has no terminal-reason field, and stream accumulation does not preserve it.

**Comparator:** mini's parser feedback distinguishes output truncation from malformed actions; OMP's loop consumes explicit stop reasons for recovery and incomplete-output handling.

**Recommended priority: medium now, prerequisite before loop streaming.** Preserve terminal metadata consistently and test the provider-to-public-response boundary. Also audit partial-stream usage and fallback accounting separately; the probe did not prove those paths incorrect.

**Important scope:** Garuda's native loop currently calls complete(), not complete_streaming(). This proves a streaming API gap, not that normal native completion currently misclassifies every truncated response. The TUI's own module documentation acknowledges that native token streaming is not wired through the loop.

Evidence: [transport](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/model/litellm_model.py), [stream protocol](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/model/protocol.py), [actual loop](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/loop.py), [TUI scope](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/interfaces/tui.py), [mini truncation regression](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/tests/models/test_truncation_finish_reason.py).

### F3. Validated structured child data is dropped by the ordinary handoff formatter

**Observed:** an AgentResult with a structured output object and a short final message retained the object internally. The actual format_subagent_summary function emitted the final message but omitted the structured object. InvokeSubagentTool returns that formatted text.

The probe supplied the structured output field directly; the existing schema suite separately exercises validation. It isolates delivery rather than proving a live delegated run. This is a delivery gap rather than a missing schema validator. A model might duplicate its JSON in final prose, but the parent should not depend on that duplication.

**Comparator:** OMP returns structured validation status/data and stores separately retrievable child JSON.

**Recommended priority: high for automated delegation.** Preserve validated output, schema identity, producer and workspace provenance in a typed result/artifact alongside display text. Do not weaken Garuda's strict completion validation to match a permissive upstream mode.

Evidence: [formatter](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/subagent.py), [tool wrapper](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/subagent.py), [schema owner](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/agents/output_schema.py), [OMP task results](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/tools/task.md).

### F4. Existing text matching accepts a file changed since an earlier read

**Observed:** after reading a temporary file, the probe changed an unrelated line, then used EditTool to replace a still-matching string. The edit succeeded and preserved the unrelated change.

This is correct under Garuda's current string-match contract. It demonstrates the absence of a read-version precondition, not data loss or a broken existing promise.

**Comparator:** OMP's snapshot-based contract can reject or explicitly recover changed targets.

**Recommended priority: medium, opt-in improvement.** Add content/version binding where stale targeting is costly. Establish atomic backend behavior before claiming race-free writes. Preserve the convenient current matcher for workflows that do not require whole-file version binding.

Evidence: [Garuda edit contract](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/edit.py), [Environment protocol](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/workspace/protocol.py), [OMP edit contract](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/tools/edit.md).

### F5. Existing budget support is narrower than one shared dollar cap

**Observed in source:** Garuda has turn/deadline/context limits, a collection budget with token and known-cost reservations, and a routing budget used for initial selection. These do not establish a general cumulative dollar gate across the main reasoning loop, summaries, verifier/critic, and ordinary children.

OMP has a shared session-tree cost gate. Mini checks its current agent spend before the next model query. Both charge completed responses, so neither should be called an exact prepaid maximum that cannot overshoot.

**Recommended priority: medium.** Extend the existing budget vocabulary and shared accounting owner rather than add disconnected per-interface caps. Include concurrent reservations, unknown prices, children, auxiliary calls, and resumed runs. Keep subscription usage unknown unless a proved source supports the desired unit.

Evidence: [Garuda collection ledger](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/collection.py), [model budget configuration](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/model/config.py), [routing budget](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/runtime/router.py), [OMP cost gate](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/packages/coding-agent/src/session/cost-gate.ts), [mini limit checks](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py).

### F6. Standalone ACP acceptance checks already work; some prose underclaims them

**Observed:** the existing executable CLI test launches a fake ACP runtime with a user-requested check and observes verification passed with user-request authority. That test passed in this assessment.

Garuda's guide says broadly that ACP results are not verified. The precise distinction is that **Garuda does not apply its native completion gate inside the external harness, but explicit post-run acceptance checks can independently verify selected criteria on a standalone ACP run**.

**Recommended priority: high for documentation accuracy.** Explain that distinction consistently and show the effective check selection before launch. Do not design an already existing standalone check path as a new feature.

This does not imply that all ACP runs are verified, that native tool authority confines the harness, that checks are exhaustive, or that check-protected flows are eligible for automatic quota continuation.

Evidence: [CLI acceptance wiring](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/interfaces/main.py), [receipt policy](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/acceptance.py), [executable ACP check test](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/tests/test_acceptance.py), [guide wording](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/docs/guides/external-harnesses.md).

## Detailed assessment of existing features

### 1. Text editing and multi-edit

**Garuda:** the existing matcher handles exact text, pasted line prefixes, EOL differences, and unique whitespace/indentation matches. Ambiguity refuses unless replace-all is explicit. MultiEdit resolves every edit against an in-memory candidate before making one write; later matches see earlier edits.

**Comparison:** this is substantially more helpful than asking a shell-only agent to assemble edits itself. OMP offers several model-specific edit formats, snapshot/visible-range checks, and richer diagnostics. Its multi-section OS writes can still partially apply; it is not universally transactional.

**Judgment:** Garuda is competent for ordinary text replacements but weaker at stale-target detection and model-specific interaction. Its multi-edit guarantee is validation atomicity, not proof of crash-atomic storage or cross-file rollback.

**Best improvement:** F4, plus typed error categories and correct bytes/version handling at Environment. Local newline translation is already a recorded accepted limitation; do not silently claim byte preservation or reopen that decision without a concrete requirement.

**Effectiveness test:** correct target/content under formatting variations and concurrent change, retry turns, and checked result—not how many patches were accepted.

Evidence: [edit](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/edit.py), [multi-edit](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/multi_edit.py), [local backend](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/workspace/local.py), [OMP edits](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/tools/edit.md), [mini environment](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/environments/local.py).

### 2. File reading and document tools

**Garuda:** numbered text reads, offset/limit selection, long-line clipping, and dedicated document/image readers. The text reader obtains the full file before selecting lines, so a small display limit does not imply bounded underlying read memory.

**Comparison:** OMP has structural summaries, multiple ranges, streaming of larger text sources, archive/database/notebook views, and a shared path interface. Mini can invoke available shell programs, but that is not an equivalent built-in reader contract.

**Judgment:** Garuda's text reader is straightforward and predictable; OMP is richer for large repositories and mixed documents. More formats do not establish better extraction accuracy.

**Best improvement:** bounded range reads at Environment, an explicit Python outline, honest omission metadata, and retrievable source ranges. Keep raw/ranged reads available.

**Effectiveness test:** bytes allocated/read, useful source localization, extraction fidelity on representative documents, and total exploration cost. Do not turn every initial read into a lossy summary by default.

Evidence: [Garuda reader](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/files.py), [documents](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/documents.py), [OMP read contract](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/tools/read.md).

### 3. Search, glob, and result shaping

**Garuda:** structured search modes, context lines, explicit ignore behavior, ripgrep with a grep fallback, and glob semantics corrected after find's broader matching. Grep's result-line cap is applied after env.execute has returned; Glob uses a find traversal followed by sorting.

**Comparison:** OMP's native search has per-file/global result caps, paging, match-centred long-line windows, and file/resource integration. Mini can run a carefully bounded rg command but must choose that command itself.

**Judgment:** Garuda is adequate on small/medium workspaces; OMP exposes better controls for large result sets. There is no timing result here proving native code is faster for our tasks.

**Best improvement:** bound capture/production upstream, distribute results across files, distinguish capped match count from exact total, and provide stable pagination tied to source state. Keep ignore semantics explicit.

**Effectiveness test:** a common symbol across a large fixture, one huge line, ignored/generated trees, invalid patterns, cancellation, and fallback differences. Measure full execution/capture, not only display length.

Evidence: [Garuda search](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/search.py), [OMP grep](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/tools/grep.md), [OMP native text pipeline](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/natives-text-search-pipeline.md).

### 4. Post-edit syntax and lint feedback

**Garuda:** fast syntax checks for supported formats and focused Python Ruff diagnostics after writes. Unavailable checkers and internal checker errors can produce no opinion. Syntax errors are reported after the edit lands rather than causing rollback.

**Comparison:** OMP integrates language-server diagnostics, formatting, and semantic operations. Mini relies on commands the model chooses to run.

**Judgment:** Garuda's current feedback is useful and inexpensive in architecture, but less project-aware. Silence must not be interpreted as “all checks passed.”

**Best improvement:** report checker availability/freshness without noisy success spam; distinguish introduced diagnostics from preexisting problems. Add project-aware checks only through trusted command/process owners. LSP is a broader extension, not a repair required for the current syntax contract.

**Effectiveness test:** invalid syntax, preexisting undefined names, absent checker, stale diagnostic, and latency. Only authorized acceptance checks establish verification.

Evidence: [Garuda diagnostics](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/diagnostics.py), [OMP diagnostics lifecycle](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/tools/lsp.md), [OMP freshness tests](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/packages/coding-agent/test/tools/lsp-diagnostics-freshness.test.ts).

### 5. Large-output buffering

**Garuda:** full body on disk with a compact preview, grep/slice/list/query retrieval, adaptive thresholds, and fallback shaping. Old buffers can be pruned after the configured retention interval; references are not permanent evidence.

**Comparison:** better retrieval ergonomics than mini's ordinary clipped observations, but weaker than OMP's explicit capture states and staged artifact publication. Garuda's flow-artifact integrity should not be confused with buffer storage.

**Judgment:** good design with a demonstrated storage weakness, F1.

**Best improvement:** successful publication before discovery, expected size/digest, complete/sample/failed/missing states, and truthful retention. Keep immediate tool execution separate from capture outcome.

**Effectiveness test:** successful retrieval after resume, full-disk/short-write/cancel, missing/pruned captures, and no advertisement of a partial file as complete. This can be proved without model calls.

Evidence: [buffer](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/buffer.py), [output shaping](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/context/shaper.py), [OMP artifacts](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/blob-artifact-architecture.md), [mini observations](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/config/default.yaml).

### 6. Context windows and compaction

**Garuda:** request preflight, output reserve/margin, adaptive observations, mechanical pruning before model summaries, retrievable buffers, and a deterministic working-state card pinned across compaction. This is more disciplined than relying entirely on an LLM summary.

**Comparison:** OMP offers richer branch/context maintenance and provider-specific methods. Mini's linear history is easy to inspect but does not provide equivalent built-in compaction in its DefaultAgent loop.

**Judgment:** retain Garuda's state-preservation approach. Richer compaction choices do not establish better retained accuracy, token economics, or cache behavior.

**Best improvement:** visible retained/omitted evidence, phase-aware context, and failure/recovery fidelity around existing strategies. Do not replace deterministic state with an opaque provider summary.

**Effectiveness test:** preserve task, constraints, file effects, check identities, and unanswered tool pairing across compaction/resume. Compare whole-run turns/cost and fact retention if a funded model experiment is available.

Evidence: [condenser](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/context/condenser.py), [state card](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/context/state_card.py), [OMP compaction](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/compaction.md), [mini loop](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py).

### 7. Session persistence and inspection

**Garuda:** append-only event records, atomic metadata/message replacement, linked runtime segments, workspace baselines, and protected publication for checked flows. Ordinary metadata uses best-effort locking and lacks the full strict publication policy; guarantees differ by record.

**Comparison:** OMP has versioned session trees, retained artifacts, forks, and rich export. Mini serializes direct messages/configuration into trajectories but writes the trajectory file directly. Simpler inspection is not the same as crash-safe dispatch.

**Judgment:** Garuda's workflow authority/evidence is strong; its ordinary ACP trail is much less explanatory than native trajectories or a full external transcript.

**Best improvement:** a versioned coverage-aware diagnostic projection, bounded selected payload capture where authorized, and clearer original-versus-recovery causes. Never invent unavailable old messages.

**Effectiveness test:** torn records, unavailable payloads, interrupted operations, and fresh-process inspection. Benchmark checkpoint latency before revisiting the backlog's accepted full-transcript rewrite cost.

Evidence: [Garuda sessions](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/sessions.py), [protected publication](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/session_publication.py), [ACP persistence](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/acp/adapter.py), [OMP sessions](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/session.md), [mini save](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py).

### 8. Completion and attributed verification

**Garuda:** native task completion and authorized acceptance are separate. Receipts bind command, authority, workspace fingerprint, exit code, and bounded output. A check that changes the tree or depends on test infrastructure changed by the run cannot casually become passing verification.

**Comparison:** mini's completion sentinel is submission, not an independent local verdict; its benchmark evaluator supplies separate grading. No equivalent general authority/candidate-bound acceptance owner was established in the inspected OMP paths.

**Judgment:** this is a Garuda strength in explicit verification semantics. It does not mean the checks prove every task requirement, or that every default run uses the expensive eval/rigorous gates.

**Best improvement:** show effective checks before launch and reasons for unavailable/invalidated verification afterwards. Correct F6's native-versus-ACP distinction. Preserve independent receipts rather than replace them with another model's approval.

**Effectiveness test:** false success claim, changed test infrastructure, stale candidate, missing checks, and real CLI post-run acceptance. Existing acceptance tests passed.

Evidence: [receipt owner](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/acceptance.py), [modes](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/modes.py), [mini submission](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/environments/local.py), [mini benchmark execution](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py).

### 9. Reviewer workflows

**Garuda:** explicit verdict/severity, bounded repair rounds, runtime/model identity independence including relevant fallbacks/consults, and receipt-based resume. Unresolved reviews cannot be skipped as if approved.

**Comparison:** OMP has richer review interaction, priority/confidence presentation, annotation, and live advisors. Mini's core does not provide an equivalent built-in reviewed-flow engine.

**Judgment:** Garuda is strong on workflow integrity and weaker on reviewer usability. Identity independence is not proof that all inherited drafts were independently authored.

**Best improvement:** a sufficient candidate-bound review packet plus a versioned structured finding schema. Give reviewers effective tool permissions before spending a turn. Confidence remains an assertion until calibrated.

**Effectiveness test:** reviewer can catch an ignored-config defect without denied Git commands; invalid output remains an explicit stop; completed coder work is not rerun when only review is pending.

Evidence: [review owner](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/flows/review.py), [flow artifacts](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/flows/artifacts.py), [OMP review/annotation surface](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/slash-command-internals.md), [OMP advisors](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/advisor-watchdog.md).

### 10. Permissions and approvals

**Garuda:** tool/path/command screening, effective ceilings, reauthorization of rewritten calls, conservative unknown effects, and inherited restrictions on children. Runtime brokers preserve approval outcomes where wired. These controls do not confine broad shell results.

**Comparison:** OMP has tiered permissions, remembered target grants, restricted-folder prompts, and rich dialogs. Its current default approval mode is yolo, while its tool/user denies and explicit prompt rules still matter. Mini's InteractiveAgent offers confirm/human/yolo and command regex whitelists.

**Judgment:** Garuda provides useful conservative mechanisms; OMP provides a richer approval experience. Neither a regex policy nor an approval dialog is a sandbox.

**Best improvement:** unify outcome visibility across interfaces and give an exact reason/source for restrictions. Use prepared review evidence instead of widening a shared side-effect-free allowlist simply to allow Git in reviewers.

**Effectiveness test:** child escalation, hook rewrites, path aliases, headless refusal, stale approval answers, and concurrent runs with separate roots. Existing ceiling and guard tests passed.

Evidence: [Garuda permissions](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/permissions.py), [broker](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/acp/broker.py), [OMP approvals](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/approval-mode.md), [mini interactive permissions](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/interactive.py).

### 11. Workspaces, subprocess cleanup, and ownership

**Garuda:** local/Docker/remote/tmux backends, optional persistent shell, per-session worktrees, leases/capacity, supervised checked-flow execution, and conservative quarantine when descendants/authority are uncertain. Docker is a confinement boundary; worktree separation alone is not.

**Comparison:** OMP has copy-on-write task isolation, retained workspaces, native shell/process support, and owner markers. Its isolated-workspace cleanup treats missing/malformed markers differently from Garuda's retained uncertain-owner policy. Mini offers multiple environment backends with a simple stateless action path.

**Judgment:** Garuda's refusal model is deliberately more conservative in the examined authority paths. This protects non-replay but can require more operator reconciliation; it is not a claim that OMP has no ownership controls.

**Best improvement:** explain quarantined owners and required reconciliation evidence in one view. Preserve default stateless execution and existing cancellation settlement rather than rewrite the shell based on feature breadth.

**Effectiveness test:** parent death, reused PID, incomplete cleanup, cancelled launch, conflicting mutations, and destination change before integration. Live Docker/tmux/OS comparisons were not run here.

Evidence: [lease](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/workspace/lease.py), [settlement](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/workspace/settlement.py), [worktrees](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/workspace/worktrees.py), [OMP ownership](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/packages/coding-agent/src/task/isolation-ownership.ts), [mini environment](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/environments/local.py).

### 12. Delegation and structured output

**Garuda:** bounded schema compilation, unsupported-keyword refusal, a limited repair window, and no invalid result returned as valid output. Children inherit permissions, admitted tools, depth/launch/deadline limits, and separate traces. Ordinary SubagentRunner allows only one active child; read-only flow parallelism is a separate mechanism.

**Comparison:** OMP supports typed child results, schema sidecars, asynchronous workers, revival, and workspaces. Its permissive mode can accept invalid structured output after retries; strict mode can refuse. Mini's core does not include this delegation system.

**Judgment:** Garuda's strict output contract is a strength; end-to-end result delivery is weaker because of F3. More workers are not the first repair.

**Best improvement:** typed child handoff, evidence-derived changed files, visible schema/validation status, then separately designed concurrency. The summary's tool-name inference should not stand in for authoritative workspace deltas.

**Effectiveness test:** wrong shape, failed child with partial output, stale artifact, inherited ceiling, budget exhaustion, and parent cancellation. Existing schema/ceiling tests passed.

Evidence: [schema owner](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/agents/output_schema.py), [subagent runner](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/subagent.py), [OMP task contracts](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/tools/task.md).

### 13. Reviewed memory

**Garuda:** small scoped proposals, secret-pattern rejection, digest-bound human acceptance, idempotent acceptance after a crash, and labelled prompt loading. Headless work can propose but cannot silently accept its own future instructions.

**Comparison:** OMP has richer local/remote memory retrieval, session extraction, consolidation, and optional learning. Its local backend is disabled by default; behavior varies substantially by backend. Mini's DefaultAgent has no equivalent durable memory owner.

**Judgment:** Garuda is stronger on explicit acceptance control, weaker on finding and refreshing useful prior knowledge. More automatic storage does not demonstrate better memory accuracy.

**Best improvement:** evidence/source references, supersession, version/freshness metadata, and search of accepted notes. Preserve review and project boundaries. Avoid promoting generated lessons into executable skills by default.

**Effectiveness test:** relevant recall, stale/contradictory notes, cross-project access, secret rejection, and repeated acceptance. Existing note tests passed; recall quality was not measured.

Evidence: [Garuda notes](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/context/notes.py), [prompt assembly](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/agents/prompt.py), [OMP memory](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/memory.md), [OMP retain semantics](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/tools/retain.md).

### 14. Agent definitions, instruction loading, and skills

**Garuda:** a versioned resolver with source provenance, bounded inheritance, trusted locations, compile-time budget checks, mandatory prompt refusal, scoped skills, and inspection commands. Required gates cannot be silently disabled by a project definition.

**Comparison:** OMP discovers many foreign formats and supports a broad plugin/skill ecosystem. Mini's small YAML/model/environment configuration is easier to follow but does not provide the same provenance/authority model.

**Judgment:** Garuda has a strong controlled-configuration design; onboarding compatibility and unsupported-field explanation can improve. The historical ignored-field incident shows why the actual effective loader matters more than a declaration.

**Best improvement:** surface unsupported/ignored legacy keys, field ownership, prompt contributions, and conflicts consistently. Add inspection-only foreign-format translation before automatic activation.

**Effectiveness test:** real loader output, inheritance conflicts, mandatory prompt overflow, symlink/path escapes, and an inert legacy key. Do not import foreign hooks/MCP/permissions merely because instruction text is supported.

Evidence: [compiler](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/agents/compile.py), [prompt plan](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/agents/prompt.py), [skill sources](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/skills/sources.py), [OMP context discovery](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/context-files.md), [OMP skills](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/skills.md).

### 15. Hooks and custom tools

**Garuda:** before/after/session hooks and opt-in project Python tools. Guard hooks block on failure by default, have bounded deadlines, and settle process groups. A rewritten call is screened again; a hook cannot turn an approved read into an unchecked write.

**Comparison:** OMP's current runtime uses its extension runner/wrapper for richer events, commands, renderers, tools, and context transformations. Its current wrapper also blocks pre-tool handler errors. The legacy hooks documentation explicitly warns that it is not the default runtime path. Mini offers extension through subclassing and protocols.

**Judgment:** Garuda is focused and sufficient for policy/custom-tool use; OMP is richer for application customization. It would be incorrect to claim only Garuda fails closed.

**Best improvement:** consistent lifecycle documentation and diagnostics for blocked hooks and direct/discovered target calls. Add new events only when an actual integration needs them.

**Effectiveness test:** failed guard, timeout cleanup, argument rewrite, unknown target, lazy dispatch, and unchanged approved calls. Existing hook tests exercise these behaviors.

Evidence: [Garuda hook owner](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/plugins/hooks.py), [tool lifecycle](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/tool_runner.py), [OMP extensions](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/extensions.md), [OMP current wrapper](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/packages/coding-agent/src/extensibility/extensions/wrapper.ts).

### 16. Model providers, retries, caching, and streaming

**Garuda:** LiteLLM transport, reasoning/caching controls, bounded retry/backoff, request-size preflight, ordinary/streaming model APIs, and an optional per-provider in-process concurrency limit.

**Comparison:** OMP invests in provider-specific wire policies, stop classifications, streaming, and replay-sensitive recovery. Mini supports multiple model adapters and explicit format-error handling. Provider count is not an accuracy measurement.

**Judgment:** Garuda has a useful portable foundation but is less integrated for live streaming. F2 is demonstrated. Core-loop token streaming is absent, despite a tested streaming API and renderer hooks.

**Best improvement:** preserve terminal reason and known usage across transport shapes; distinguish user cancellation, malformed output, context overflow, and transient errors. Then wire live streaming through the existing owner with stable tool-call/result pairing. Do not use blanket replay of partly executed turns.

**Effectiveness test:** interrupted streams, malformed args, terminal reason parity, known billed failures, cache markers, and provider/request compatibility. Existing streaming tests passed but did not catch F2.

Evidence: [transport](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/model/litellm_model.py), [governor](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/model/governor.py), [OMP provider policy](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/provider-compat-reference.md), [OMP retry contract](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/non-compaction-retry-policy.md), [mini parsing failures](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/tests/models/test_format_error_response_persistence.py).

### 17. Cost accounting and spend control

**Garuda:** provider-reported cost first, versioned fallback prices, auxiliary call accounting, idempotent usage records, and distinct ACP snapshots. Unknown cost is not zero. Collection has reservation-aware token/cost budgets; routing budget is a selector constraint.

**Comparison:** OMP has an explicit cost gate shared across its session tree. Mini has a simple pre-query agent cost limit; its optional ignore-errors cost mode can substitute zero for unavailable calculation, which weakens budget meaning.

**Judgment:** Garuda is strong in attribution and unknown-value honesty, weaker in general whole-run dollar admission. Accounting and enforcement are different features.

**Best improvement:** F5, plus an audit of failed/partial-call usage without assuming missing data is free. Include auxiliary work and children; publish measured, estimated, and unknown separately.

**Effectiveness test:** one request crosses a cap, several concurrent requests reserve budget, a child or summarizer spends it, unknown pricing, resume, and deduplication. No exact prepaid maximum should be promised without reserving credible bounds.

Evidence: [Garuda accounting](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/model/accounting.py), [prices/cost resolution](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/eval/costs.py), [collection ledger](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/collection.py), [OMP gate](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/packages/coding-agent/src/session/cost-gate.ts), [mini cost handling](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/models/litellm_model.py).

### 18. MCP integration and lazy tools

**Garuda:** stdio/HTTP transports, per-server fault isolation, bounded calls, trusted project configuration, per-tool effect authorization, and lazy search/use dispatch. Discovered calls enter ordinary ToolRunner controls. The supported dependency range is MCP 1; a test filename containing v2 does not establish MCP protocol/package 2 support.

**Comparison:** OMP has deferred cached tool startup, live tool/resource/prompt updates, reconnect handling, subscriptions, and readiness controls. Mini's inspected core does not implement an equivalent MCP client.

**Judgment:** Garuda's trust/dispatch design is valuable; OMP provides a richer dynamic operational experience.

**Best improvement:** explicit required-versus-optional server readiness, visible per-server degraded state, bounded reconnection, then resource/prompt support when needed. Never reconnect an execution-bearing server call by blindly replaying it.

**Effectiveness test:** one bad server beside a real local echo server, cancellation/teardown, changed trusted config, late discovery, transport closure, and lazy target hook/permission equivalence.

Evidence: [Garuda client](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/mcp/client.py), [trust/config](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/mcp/config.py), [lazy tools](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/discovery.py), [OMP lifecycle](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/mcp-runtime-lifecycle.md).

### 19. Web search and fetching

**Garuda:** dedicated fetch/search, bounded bytes/time, HTML text extraction, domain controls, redirect revalidation, and public-address validation tied to the actual pinned-IP connection.

**Comparison:** OMP has far richer source-specific extraction, search backends, document rendering, and resource caching. Its approval documentation's private-URL classification is not DNS resolution; that approval mechanism is not equivalent to Garuda's pinned fetch policy. This assessment does not audit every OMP transport or prove a general SSRF vulnerability. Mini can use shell networking without the same tool-specific policy.

**Judgment:** retain Garuda's explicit network boundary and improve content/research usability. Do not remove network restrictions to gain access to richer readers.

**Best improvement:** source/title/date/section provenance, reusable bounded rendered content, and supported search-provider fallback with clear unknowns. Content from websites remains untrusted data, not execution instructions.

**Effectiveness test:** DNS rebinding, redirect to private address, oversized/slow body, malformed HTML, source attribution, and extraction fidelity. No live search-quality comparison was run.

Evidence: [Garuda web tools](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/tools/web.py), [OMP fetch/render](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/packages/coding-agent/src/tools/fetch.ts), [OMP search](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/tools/web_search.md), [OMP URL approval scope](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/approval-mode.md).

### 20. Flows, review continuation, and quota recovery

**Garuda:** immutable step artifacts/receipts, saved review state, checkpoint validation, exclusive claims, account/version-proved Codex quota evidence, and hourly continuation through the ordinary flow owner. Uncertain execution cannot authorize replay.

**Comparison:** OMP has richer in-session provider retry/reset/fallback behavior, with replay-sensitive decisions of its own. Mini's core has cost/step/time stops and saved output, not an equivalent durable multi-role continuation scheduler.

**Judgment:** Garuda's durable workflow contract is a strength with narrow eligibility. It is not universally better availability: Claude, protected/check-enabled flows, dashboard flows, parallel groups, and historical stops remain outside current automatic recovery.

**Best improvement:** tick health, original failure plus recovery reason, eligibility explanation, and review-packet availability. Extend support only when quota and process-lifetime evidence is proved.

**Effectiveness test:** pending review does not rerun coder, concurrent ticks dispatch once, changed workspace/account/version refuses, and unproved cleanup retains ownership. Do not treat scheduling installed as proof of a successful continuation.

Evidence: [flow recovery owner](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/flows/quota_recovery.py), [flow execution service](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/flows/service.py), [scheduler](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/runtime/recovery_schedule.py), [OMP retry/reset behavior](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/non-compaction-retry-policy.md).

### 21. CLI, SDK, dashboard, and human visibility

**Garuda:** several interfaces share agent/runtime setup and session read models, with backgrounds, approvals, flows, cost/limits, and trajectory views. Shared ownership is preferable to recreating admission logic in each UI.

**Comparison:** OMP has richer live terminal interaction and worker controls. Mini's simpler CLI/inspector has a lower conceptual surface. Neither breadth nor simplicity alone establishes a better experience.

**Judgment:** improve existing visibility and wiring before building a parallel UI. Garuda's native token streaming gap is a concrete implementation limitation; the current ACP diagnostics gap is a separate data-coverage limitation.

**Best improvement:** show actual current owner/operation, waiting reason, recent progress timestamp, evidence coverage, and safe next action. A status read must not implicitly resume work. Keep native versus external behavior explicit.

**Effectiveness test:** fresh-process reconnect, missing child traces, read-only access, waiting approval/quota/cleanup, and consistent effective config across CLI/SDK/web. Operator diagnosis time is the useful measure.

Evidence: [Garuda session read model](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/core/read_model.py), [TUI](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/interfaces/tui.py), [OMP Agent Hub](https://github.com/can1357/oh-my-pi/blob/dcafcef6422c6a260649b879d75a5aabb1ea778e/docs/agent-hub.md), [mini inspector](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/utilities/inspector.py).

### 22. Evaluation and experimentation

**Garuda:** Harbor/ATIF, deterministic contract matrices, ablations, harness trials, and paired cost/quality reporting with explicit provenance and unknown values. This is substantial infrastructure; it does not establish a measured advantage by itself.

**Comparison:** mini offers a simpler directly inspectable reference loop and batch benchmark execution. OMP has tool-specific experiments and performance claims, but those results cannot be transplanted to Garuda.

**Judgment:** Garuda should consolidate its existing evidence paths and use a frozen simple baseline. Building another evaluator would likely add fragmentation.

**Best improvement:** an incident corpus, task/config/environment identities, one varied mechanism per experiment, and reports distinguishing acceptance from model success. Respect the backlog's unavailable repeated paid-trial budget.

**Effectiveness test:** deterministic offline mechanisms first; optional capped matched-task trials later. Unknown cost, missing observations, and single-trial variability must remain visible. Do not compare unlike authority or hidden harness prompts as though only one feature changed.

Evidence: [ablation](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/eval/ablation.py), [harness matrix](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/garuda/eval/harness_matrix.py), [paired report contract](https://github.com/Darshan2104/Garuda-openagent/blob/8f6c488335f1220a0aff5f97f76fdbbd7c05d0af/docs/evaluation/dual-model-routing.md), [mini batch](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py).

## Recommended action order for existing capabilities

| Order | Change | Why this order | Acceptance evidence |
| --- | --- | --- | --- |
| 1 | Repair buffer publication/discovery | Demonstrated integrity weakness on a current retrieval path | Failed write never becomes a complete discoverable capture |
| 2 | Preserve typed child output | Existing validation loses value at the final handoff | Parent receives validated payload and producer/schema identity |
| 3 | Correct verification/streaming descriptions | Existing support is both underclaimed and overgeneralized | Documentation matches executable standalone ACP checks and actual native loop |
| 4 | Preserve streaming terminal metadata | Demonstrated API inconsistency; required before further streaming wiring | Same terminal reason survives ordinary and streaming transport |
| 5 | Improve ACP failure coverage and reviewer packets | Directly helps the external workflows we actually ran | Original cause and candidate-bound review evidence available without invented payloads |
| 6 | Extend shared spend budgets | Current limits do not cover all model work uniformly | Children/auxiliaries/resume/concurrency consume the same admitted budget |
| 7 | Improve read/search bounding and stale edits | Large-repository friction and targeting gaps | Correct source targeting with bounded I/O/output; no silent stale recovery |
| 8 | Improve memory/MCP/operator usability | Valuable existing foundations, less immediate integrity risk | Relevant recall and clear readiness/coverage without widening authority |

Orders 1–4 are bounded correctness/delivery/documentation work. Orders 5–8 need scoped design decisions; the previous feature study supplies broader alternatives. Nothing in this report authorizes deploying every proposal or relaxing existing guards.

For the ACP-heavy workflow, shared diagnostics, review inputs, and acceptance visibility have direct impact. Native file/edit improvements do not alter Codex's or Claude Code's internal tools.

## What the checks actually established

Existing focused suites:

| Run | Areas | Result |
| --- | --- | --- |
| A | Editing, multi-edit, buffers, reviewed memory, output schema, delegated ceilings, context budget/pack, hooks | 154 passed |
| B | Web tools/security, MCP client/transports/allowlist, streaming API/rendering, model governor, cost accounting, flow review, acceptance | 179 passed |
| Combined | 21 existing test files | 333 passed |

These include real local subprocess/CLI fixtures where the tests define them; model responses and provider calls are synthetic. They are not live external-harness, real-provider, Docker/tmux/Harbor/OS-sandbox, browser, or performance comparisons.

Additional offline probe observations:

| Probe | Input | Observed result | Interpretation |
| --- | --- | --- | --- |
| Finish-reason parity | Identical synthetic length stop through ordinary and streaming transport | Ordinary retained length; streaming returned no reason | API metadata weakness, F2 |
| Child result delivery | Structured output present only in AgentResult.output | Text formatter omitted the object | Delivery gap, F3 |
| Changed file after read | Unrelated line changed before a still-valid replacement | Edit accepted; unrelated change preserved | Current contract limitation, F4 |
| Failed capture and restart | Writer produced three bytes then raised synthetic disk-full error | Fresh discovery listed the partial file without capture-failure metadata | Storage/discovery weakness, F1 |

Raw synthetic observations were saved locally to /tmp/garuda-existing-feature-probes-20261026.json. This temporary file is not a durable evidence store; the table records the relevant observations.

No runtime fixes or new repository tests were made in this assessment. No full test suite was rerun for the documentation-only change. Comparative reliability beyond the demonstrated local contracts remains unmeasured.

## Corrections and limits that should stay visible

- Standalone ACP runs can receive explicit post-run acceptance checks. This is independent of applying the native completion gate inside an external harness.
- A streaming API and delta renderer do not mean native token streaming is wired into the actual loop.
- Multi-edit prevalidation does not promise crash-atomic filesystem writes.
- Snapshot tags do not confer authority or eliminate write races unless the backend enforces the comparison.
- Human-reviewed memory and autonomous retrieval optimize different goals; neither establishes truthful knowledge.
- OMP also has fail-closed pre-tool handling, replay safeguards, and ownership controls. Garuda does not uniquely possess those ideas.
- Broad default permission differences are not a full security audit; confinement, source trust, and entry-point wiring matter.
- Current quota recovery and checked-flow supervision have different eligibility boundaries. Improvements to diagnostics do not merge them.
- Existing acceptance/integration mechanisms are worth retaining, while the two demonstrated weaknesses should not be concealed by a passing suite.

The most useful conclusion is feature-specific: retain Garuda's explicit authority and evidence model, repair the places where data loses integrity or structure, and borrow richer interaction only where it solves an observed workflow problem.

