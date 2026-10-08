# Ready-to-use workflow scenarios for Garuda

**Status:** Final implementation plan (revision 5). Approved to start P0 and P1a; P1b, P2, and P3 each start after the preceding gate in §9. Features ship only after their phase acceptance gates; this document does not implement them.

**Date:** 2026-10-06.

**Final design review:** 2026-10-07.

**Reviewed baseline:** `origin/main` at `d47c16b` (2026-10-06). It has no `garuda/` code changes since `d51b294bbc15ea09b26ab4644876ca83222dbda1`, against which the code facts in §3 were checked. Recheck the then-current main before implementation and implement from a fresh main-based branch; preserve user changes in any older local checkout.

**Goal:** A user who has connected their agents (Claude Code, Codex, or a native model) can pick a goal-oriented starter and get a real result without authoring a workflow. Each starter checks its own requirements; connecting one harness does not guarantee an independent reviewer. Starters are native Garuda behavior inspired by OpenRig's tour, limited to what fits Garuda's current runtime boundaries.

## 0. Scope reductions and review corrections

| Change | Why |
|---|---|
| **Seven cards became five launchable starters over two launch kinds (`flow`, `run`)** | "Follow progress" is the existing session/flow detail view, not a workflow; it becomes a result-view feature for every starter. |
| **"Ask a specialist" now uses a read-only role run, not a consult facade** | A human question does not need an in-progress asker session. `garuda run --role R --no-edits` provides a bounded answer on an idle checkout; it does not provide consult snapshot isolation. This drops the synthetic parent and `ConsultScenarioService`. |
| **"Understand an earlier decision" folded into "Ask a role"** | It is the same read-only run with a source attached. |
| **Repository sources are named files** | Follow `interfaces/web/grounding.py`: repository documents remain files that the task names and the model reads. Do not add general document extraction/pasting. Validated plan artifacts and existing session briefs retain their separate, bounded handoff mechanisms. |
| **Build-and-review gets trusted checks** | Reuse `acceptance.accept()` and `checks_with_authority()`, but add full-flow baseline/delta evidence and finalize checks while the flow retains workspace ownership. Adding the flag alone does not establish trustworthy verification. |
| **Recipes are addressed** | `garuda recipe run` ("YAML workflow recipes", `config/recipes.py`, documented in `docs/use-cases/automate.md`) already exists and was not mentioned. Starters replace recipes for the plan/build/review journey only; recipes remain for custom prompt sequences. See §7. |
| **The golden path is explicit** | `garuda init` proposes the four packaged-flow roles. P1a must exit with *connect → `garuda init` → `garuda starter run plan-change`* working; reviewed starters separately validate reviewer independence, and single-harness users get a working build path (§4.1). |
| **Dashboard one-click Start split into its own phase** | The dashboard has no write route for runs or flows today (only `POST /api/chat`). Launch intents, operation IDs, plan tokens, and server-shutdown semantics exist only for that new write surface. The read-only library, preview, and copy-command ship first. |
| **The first release is smaller** | P1a ships five CLI starters with no new verification mechanism; flow checks (P1b) and the read-only dashboard (P2) follow independently, and one-click launch (P3) last. See §9. |

### Review corrections in revision 3

- Verify the exact final candidate before releasing the flow lease; record the parent's full-flow baseline/delta and preserve verification during final state publication.
- Check reviewer independence as part of starter readiness, not merely the existence of four role names.
- Correct the recipe inventory: recipes already hold a workspace lease across their steps.
- Define a validated, bounded plan-artifact transfer into the next flow's actual task input.
- Use **Starters** in the UI and the proposed `garuda starter` CLI; retain `scenarios` for internal modules and HTTP resources. Use the existing `garuda config trust` command.

### Review corrections in revision 4

- Describe the independence predicate as it actually is: an exact match of (runtime, model ID) pairs, across the reviewed role's configured, fallback, launched, and consulted identities. Readiness calls `flows/review.py::check_independent`; it does not reimplement or tighten the rule. A stricter rule would be a separate change.
- Give single-harness users a working build path: a **Build and check** fallback through `run-with-role`, and an explicit, user-authored `independent: false` override that is labelled "review not independent" everywhere. No packaged starter waives independence.
- Split P1. P1a ships all five starters with no new verification mechanism. P1b adds flow checks as a separately reviewed change, because it alters ownership and evidence.
- Deliver an earlier plan to `plan-feedback` through the §4.3 artifact handoff, not as a repository source.
- Call compile read-only (it reads receipts and artifacts but writes nothing), rather than pure.
- Record that recipes also lack a session baseline and delta evidence, and correct the existing user doc that says recipes take no workspace lease.

### Final clarification in revision 4.1

Readiness uses non-executing resolved role plans, including canonical runtime IDs behind aliases, with the existing independence predicate. It also reads the effective review policy: a user-authored waiver stays runnable if other requirements pass and is visibly labelled. Readiness tests use explicit expected behavior and actual flow enforcement rather than deriving their expected verdict from the same helper being tested.

### Final corrections in revision 5

- Scope the alias claim to what the predicate does today. `RolePlan.runtime_id` is canonical, so a role's primary runtime is compared by canonical ID. Fallback and consulted entries are still compared by their raw `harness` string in `flows/review.py::identities`, so an alias there is not caught. That gap is pre-existing engine behavior: record it in `docs/BACKLOG.md` and fix it in `review.py` as its own change, not in the starter layer.
- Move the §5.3 check-contract confirmation out of P0 into a P1b entry gate, so P1a does not wait on verification design.
- Add the `checks` parameter to `FlowExecutionService.run` only in P1b.
- Add an implementation start checklist (§9) covering branch, index, and the standalone docs fix.

**Re-review focus:** the ownership/evidence contract in §5.3, reviewer readiness and the single-harness path in §4.1, actual plan delivery in §4.3, the P1a/P1b split in §9, and their owner-boundary tests in §12. All changes remain proposed; no runtime code or durable decisions are changed by this document.

## 1. Recommendation

Build a small **scenario catalog and brief compiler** over Garuda's existing packaged flows, role runs, acceptance checks, read model, and dashboard. Ship five starters:

| ID | Starter | Launch kind | Existing execution |
|---|---|---|---|
| `plan-change` | Plan a scoped change | `flow` | `plan-only` |
| `plan-feedback` | Turn feedback into a plan | `flow` | `plan-only` (different form) |
| `build-review` | Build, review, and check a change | `flow` | `plan-build-review`, or `pair` with validated plan input; checks via new ownership-preserving finalization (P1b) |
| `run-with-role` | Run a task as a chosen role | `run` | `garuda run --role` with `--name`, `--isolation`, `--bg`, `--check` |
| `ask-role` | Ask a role a question (optionally about a decision doc) | `run` | `garuda run --role --no-edits` with named sources |

Two cross-cutting features apply to every starter: a **result and next-action summary** on the existing detail view, and **Implement this plan**, an explicit follow-up from any `plan` artifact.

Not in scope: persistent teams, multi-week coordination, cross-machine coordination, assignment or claim queues, mailboxes, a resident daemon, a new workflow engine, or a human-facing consult facade.

## 2. OpenRig source scenarios

OpenRig's [tour](https://openrig.dev/tour) presents staged product journeys over a persistent lead/specialist control plane. Garuda translates only the interactions that fit its native/ACP runtime, session ownership, context, and verification. No OpenRig package, CLI, service, assets, terminal driving, or runtime state is imported.

| Source scenario | Decision | Garuda starter | Explicit limit |
|---|---|---|---|
| [Coordinate a project over weeks](https://openrig.dev/tour/parallel) | Drop | `build-review` covers one bounded change | No persistent lead or milestone management |
| [Ask a specialist](https://openrig.dev/tour/send) | Adapt | `ask-role` | A fresh read-only run, not a message to a live agent |
| [Give work an owner](https://openrig.dev/tour/queue) | Adapt | `run-with-role` | A named session, not a durable assignment |
| [Coordinate across machines](https://openrig.dev/tour/across-machines) | Drop | none | Remote Docker is not a distributed team |
| [Scope work against a goal](https://openrig.dev/tour/scope) | Keep, bounded | `plan-change` | Models judge scope; Garuda does not enforce prose |
| [Use earlier decisions](https://openrig.dev/tour/context) | Adapt | `ask-role` with sources | Explicit files or session briefs only |
| [Know the next workflow step](https://openrig.dev/tour/workflow) | Adapt | Result and next action | Advice from evidence; no persisted human gate |
| [Give feedback to one lead](https://openrig.dev/tour/feedback) | Adapt | `plan-feedback`, then Implement this plan | No lead inbox or automatic routing |
| [Ask one lead for progress](https://openrig.dev/tour/progress) | Adapt | Result and next action | Covers only the selected records |

Starter titles must not promise capabilities the runtime lacks. Dropped scenarios do not appear as cards.

## 3. Current-main facts this plan depends on

| Primitive | Owner on main | Use |
|---|---|---|
| Role proposal | `interfaces/onboarding.py::init_proposal` (`garuda init`) | Proposes four roles; a single harness can bind coder and reviewer to the same identity, so independent-review readiness needs a separate check |
| Project checks | `onboarding.py::project_proposal` (`garuda init --project`), `garuda config trust` | Project init writes and trusts confirmed bytes; config trust reviews an existing or changed project configuration |
| Packaged flows | `flows/packaged.py` | `plan-only`, `pair`, `plan-build-review`; a same-name `garuda.yaml` flow replaces one |
| Flow command | `interfaces/main.py::run_flow` (about 65 lines) | Extracted into a shared service (§6.3) |
| Flow execution | `flows/engine.py`, `launch.py`, `artifacts.py`, `review.py` | Preserve step/review mechanics; extend the parent lifecycle for baseline/delta tracking and check finalization before lease release |
| Role runs | `garuda run` flags `--role --name --isolation --bg --no-edits --check` | `run-with-role` and `ask-role` |
| Acceptance | `core/acceptance.py::accept`, `checks_with_authority` | Reuse check authority and receipts; `accept()` acquires no workspace lease and reads the parent's `delta_changed`, so those prerequisites must be supplied |
| Session briefs | `context/brief.py` | Bounded "earlier session" sources |
| Grounding | `interfaces/web/grounding.py` | Repository-document sources are files named in the task; flow artifacts and session briefs have separate bounded data envelopes |
| Read model and setup | `core/read_model.py`, `core/setup_view.py`, `GET /api/setup` | Readiness, progress, review-versus-verification separation |
| Dashboard | `interfaces/web/routes.py`, `static/views_sessions.js` | Flow detail exists; no run or flow start route exists |
| Review independence | `flows/review.py::check_independent`, `identities`; `engine.py::_independence` | Exact (runtime, model ID) membership across configured, fallback, consulted, and launched identities; `independent: false` records `policy: waived`. Primary identities use the plan's canonical `runtime_id`; fallback and consult entries use the raw configured `harness` string |
| Role resolution | `runtime/roles.py::plan_role` | Registry lookup only (no subprocess, login, or model call); returns a `RolePlan` with the canonical `runtime_id` |
| Recipes | `config/recipes.py`, `garuda recipe run` | Parameterised prompt sequences already hold one workspace lease; they lack the packaged flow's per-step persisted sessions, session baseline/delta evidence (see `docs/BACKLOG.md`), typed artifacts, and review receipts. `docs/use-cases/automate.md` wrongly says they take no lease |

Constraints carried forward from revision 1, all still true on main:

- `garuda flow run` accepts only `NAME --task --workspace`. Do not document `--bg` or `--isolation` for flows.
- Parallel flow groups are read-only snapshot reviews; they do not authorize parallel writers.
- A flow review is not verification. Show verification only from an acceptance receipt.
- Existing integration prepares a checked ref; it does not merge or push.
- Permission checks are guardrails and worktrees separate changes. Neither is a sandbox boundary.
- Never keep a workspace lease open while waiting on a human.

## 4. User experience

### 4.1 Golden path (CLI first)

```bash
# Proposed starter commands; garuda init already exists.
garuda init                       # proposes scout/planner/coder/reviewer for your connected harnesses
garuda starter list               # five starters, each with readiness and the exact remedy
garuda starter run plan-change --goal "Add reconnect status" --exclude "transport rewrite"
garuda starter result SESSION_ID  # artifacts, review, verification, next action
```

Readiness builds on `setup_view.setup()` and existing role/review identity rules without subprocesses, login, inference, queue rows, or sessions. A missing role names its remedy (`garuda init`).

**Reviewer readiness uses the execution rule, not a copy of it.** Resolve the reviewed and reviewer roles through the existing trusted registry/role resolver without discovery subprocesses, fallback execution, or model calls. Pass those configured `RolePlan` objects (from `runtime/roles.py::plan_role`) and the effective configuration to `flows/review.py::check_independent`. For the reviewer and the reviewed role's primary runtime, this compares canonical runtime IDs, so two aliases of the same primary runtime/model are not independent. Fallback and consulted entries are compared by their raw configured `harness` string today, so an alias in those entries is not detected. Readiness inherits that behavior unchanged: it must not claim alias-proof independence for fallbacks or consults, and it must not patch the rule locally. The fix belongs in `review.py` (§10). If required resolution evidence is unavailable, show `not-checked` instead of inventing an identity.

Read the effective flow's `review.independent` policy as well. Required independence failures report `needs-setup`. An explicitly user-authored `independent: false` configuration can run when other requirements pass; readiness and every result surface show "review not independent" and retain the predicate's evidence. A waiver does not make the identities independent, and the starter layer never creates one.

The current predicate is an exact match of `(runtime, model ID)` pairs. Preserve its configured fallback/consult comparisons; do not introduce a separate stricter algorithm. The engine additionally includes identities actually launched and consulted, so readiness is a configured preflight rather than a guarantee about future execution. The consequences, all stated in the UI:

- A single-harness `garuda init` gives `coder` and `reviewer` the same harness with no model ID. That is the same identity, so the starter is `needs-setup`.
- Distinct resolved runtimes (for example `coder: codex`, `reviewer: claude`) satisfy the identity distinction, subject to the existing fallback/consult comparisons and other setup requirements. Different aliases of the same primary runtime do not.
- Within one harness, two explicitly configured, different model IDs pass the current rule. Show the configured model IDs so the user can see what the review relies on. A missing model ID means "the harness default", and the UI says so.
- Readiness cannot predict a runtime fallback or consult that has not happened yet. It reports configured independence; execution rechecks actual identities and refuses (or records the waiver) exactly as today.

Do not tighten, loosen, or reimplement this predicate inside the starter layer. If a stricter rule (such as treating an unknown model ID as possibly equal) is wanted, it is a separate change to `review.py` with its own test and decision record. If required identity/capability evidence is unresolved, show `not-checked`; explicit doctor/discovery can resolve it.

**Single-harness path.** When `build-review` is `needs-setup` only because of independence, the readiness screen offers three remedies, in this order:

1. **Connect a second harness** and rerun `garuda init` (an independent review).
2. **Build and check:** the `run-with-role` starter with `--role coder --check …`. This uses existing behavior, with no review and trusted checks as the evidence. The card is labelled "no review".
3. **Review that is not independent:** the user adds a same-name flow to their own `garuda.yaml` with `review: {by: reviewer, independent: false}`. `garuda config show --flow plan-build-review` prints the definition to copy. Garuda already records `policy: waived`; the result view must show "review not independent" wherever the review appears, and never shortens it to "reviewed".

Starters never waive independence themselves: no packaged flow, starter flag, or preview default sets `independent: false`. Choosing to waive is visible, authored configuration. Planning and question starters do not require a reviewer, so a single harness can use them immediately.

Missing checks are a verification warning, not permission to invent evidence. The remedy is `garuda init --project` to propose and confirm project checks, or `garuda config trust` for an existing/changed project file. Planning and question starters remain usable without an independent reviewer or checks. Starters grant no tool, network, model, or consult authority.

### 4.2 Form fields

Every starter compiles the same labelled sections. Each starter declares which sections it uses and which are required.

| Field | Used by |
|---|---|
| Goal, feedback, or question | all (one of them) |
| Requirements | `plan-change`, `build-review`, `run-with-role` |
| Out of scope | `plan-change`, `build-review` |
| Constraints to preserve | all except `ask-role` |
| Current and desired behavior | `plan-feedback` |
| Sources (repo-relative paths, session refs) | all |
| Role or flow variant | `run-with-role`, `ask-role`, `build-review` (`pair` versus `plan-build-review`) |
| Execution options | only flags the underlying command supports |

`--preview` prints the resolved flow (with its source: package or `garuda.yaml`), role-to-harness-and-model bindings, read/write posture, sources with digests, checks and their authority, and **the exact existing command it is equivalent to**. Cost shows invocation limits and review rounds; unknown cost stays unknown.

### 4.3 Result and next action (all starters)

Extend the existing session/flow detail with a compact summary: goal, constraints, sources supplied, roles with actual runtime/model, attempts, artifacts, review, verification, and next action. Keep process status, outcome, review, and verification as separate fields. Truthful end states include "plan produced", "review requested changes", "review approved; checks passed", and "review approved; no checks configured".

Next actions are deterministic advice from evidence: inspect the plan, **Implement this plan**, resolve a parked approval, inspect a failed review, configure checks, try it manually, or use the existing resume/recovery path. Reading a result never starts, resumes, or advances anything. A missing or unreadable receipt or journal yields "unknown", never "nothing pending". Coverage is labelled; there is no project-wide claim from a limited list.

**Implement this plan** starts a fresh, explicit `build-review` (`pair` variant), linked to its source flow, producer session, step, attempt, and plan digest. It is never automatic. The current `pair` flow does not automatically consume another flow's plan; the scenario compiler must deliver the selected plan in the actual task input:

1. Resolve the source within the current project and read its authoritative receipt. In v1, full plan-artifact handoff is same-project only; existing separately authorized session-brief sharing remains supported.
2. Select exactly one `plan` artifact by producer step and attempt. Load it through `flows/artifacts.py::load`, preserving version, containment, regular-file, size, and digest validation; never accept an arbitrary caller-provided store path.
3. Carry the full original approved requirements, exclusions, and constraints from the recorded starter inputs. If those are unavailable for a legacy run, require explicit user-supplied constraints instead of guessing.
4. Apply existing redaction and a bounded handoff-input budget. Refuse missing, ambiguous, changed, or oversized required input rather than silently shortening the plan. Record both the source digest and the delivered-input digest/provenance.
5. Place the validated plan in a labelled, escaped artifact-data envelope in the compiled task, using the existing flow-input convention. This bounded artifact handoff is distinct from repository-document grounding: it creates no source file or generated `.context/` file and grants no extra authority.
6. Revalidate the selected artifact and starter inputs before launch. Preview and the equivalent `garuda flow run pair --task ...` command must contain the same approved task data, with proper shell quoting. An identifier or private store path alone is not delivered plan content.

## 5. Starters

### 5.1 `plan-change`: plan a scoped change

`plan-only` flow: scout writes notes, planner writes a plan, both `no-edits`. The brief asks for a work breakdown, likely files, scope risks, and proposed validation. Suggested tests are advice, not checks.

```bash
# Existing equivalent
garuda flow run plan-only --task "Plan reconnect status using the current client; exclude a transport rewrite."
```

**Acceptance:** every supplied field reaches the actual runtime input; a missing role refuses before launch with the `garuda init` remedy; no-edits outputs are withheld on detected changes.

### 5.2 `plan-feedback`: turn feedback into a plan

The same flow as `plan-change` with a different form: feedback, current behavior, desired behavior, and constraints to preserve. It can also take an optional earlier plan through `--plan-artifact FLOW:STEP:ATTEMPT`, delivered by the §4.3 handoff. That plan is a validated flow artifact, not a repository source. The brief asks for a change proposal, the constraints preserved, affected areas, open questions, and validation. Open questions stay visible in the result. This starter is catalog data plus one brief template, reusing the §4.3 handoff; it adds no new code path.

### 5.3 `build-review`: build, review, and check a change

`plan-build-review` (or `pair` with a supplied plan), followed by the project's trusted checks.

**Phasing.** In P1a this starter runs the flow unchanged and ends with verification `unavailable`. The result offers the remedies "configure checks" and "or use Build and check" (§4.1). P1b adds the check phase below. Until P1b lands, no preview, card, or doc may say that `build-review` runs checks, and `flow run --check` does not exist.

```bash
# Existing equivalent (without checks)
garuda flow run plan-build-review --task "Add reconnect status using the current client. Preserve mobile layout."
# Proposed addition
garuda flow run plan-build-review --task "..." --check "pytest -q"
```

**New behavior, `flow run --check`:** add a bounded verification phase to the owning `FlowRunner` lifecycle, rather than checking a released workspace from the outer service:

1. Resolve CLI-requested checks and trusted configured checks through the existing authority rules before admission; freeze their definitions and source digests. `build-review` uses those checks automatically. Planning starters do not automatically execute checks; explicit check-enabled flows obtain a mutating parent lease because host checks may write.
2. Under that lease, capture the flow's starting baseline before any step runs. Retain evidence of files changed throughout the flow and persist the final cumulative `delta_changed` on the parent. Include every step and relevant test-infrastructure changes even if a later step restores them; fail closed if this evidence is incomplete.
3. After all required steps have terminal receipts and their runtimes/descendants are confirmed stopped, pin the final candidate fingerprint. Keep the lease and heartbeat active while running bounded trusted checks through `acceptance.accept()`. Check-authority changes or a candidate mismatch invalidate verification; a check that changes its tree keeps the existing void-receipt behavior. Unsupported check environments remain unavailable, with no host fallback for a Docker check.
4. Publish the candidate-bound acceptance receipts and parent's verification before releasing ownership. Final session-state publication must preserve that verification instead of overwriting it. Flow execution outcome, review, and verification remain separate; no checks means unavailable verification, and failed checks remain a failed verification even when implementation steps completed.
5. An incomplete, interrupted, or quarantined flow runs no checks. Cancellation during verification tears down the owned check process before release; uncertain cleanup retains quarantine. Resume never treats completed steps as proof that a missing verification phase ran: reconcile existing check intent/receipts, and do not replay an uncertain launched check automatically.

This uses the existing acceptance service without treating it as an ownership guard. The shared facade configures finalization once; it must not call `accept()` after `runner.run()` has returned and released the lease.

`accept()`/`run_check()` currently use synchronous subprocess execution. Add an owned asynchronous check runner at the acceptance boundary so verification does not block the flow event loop and heartbeat. Reuse the existing authority, fingerprint, infrastructure-change, and verdict rules. Persist check intent before launch, track the real process/group identity, and publish terminal evidence only after teardown. Cancelling a thread/future is not proof that the check process stopped. The existing synchronous callers keep their supported behavior; the flow finalizer uses the supervised adapter rather than an untracked `to_thread(accept)` call.

**Acceptance:** review independence and bounds are unchanged (`max_rounds: 2` means at most three coder/reviewer pairs); a configured same-name flow shows in preview and is what runs; review approval alone never renders as verified; an untrusted project check does not run. Another mutating session cannot acquire the workspace between the final step and receipt publication. Changes to check-dependent test infrastructure withhold a pass, including changes made in earlier steps.

### 5.4 `run-with-role`: run a task as a chosen role

A thin form over `garuda run --role R --name N [--isolation worktree] [--bg] [--check C]`. The name identifies a session, not a persistent teammate; "Running as coder" is fine, "claimed by coder" is not. Missing or disabled roles refuse before any session or process exists. Explain that uncommitted edits are not copied into a worktree.

### 5.5 `ask-role`: ask a role a question

`garuda run --role R --no-edits` with the question, sources, and an instruction to separate what a source says from the model's own inference.

```bash
# Existing equivalent
garuda run --role reviewer --no-edits \
  --task "Using .context/decisions.md (section 'Reconnect'), can this change reuse the existing reconnect client?"
```

**Sources:** a repo-relative path, optionally with a section name, validated to resolve inside the workspace (reject escapes and outside symlinks). The path and section are named in the brief; the model reads them with its own tools, following the grounding precedent. Record path and digest in session metadata for provenance; a digest change between preview and run requires a fresh preview. Session refs use `context/brief.py` under its existing cross-project authorization. Agents proposing a new durable decision use the existing note-proposal review.

**Trade-off versus consult:** a no-edits run reads the live checkout rather than a stable snapshot. Main's read-only lease can coexist with a mutating holder; its no-edits comparison can therefore withhold an answer if another editor changes the checkout. Show that diagnostic and recommend asking on an idle checkout. Snapshot-backed questions remain deferred (§10); do not advertise live-checkout questions as having consult isolation.

## 6. Architecture

### 6.1 Catalog

Packaged, validated data under `garuda/scenarios/data/*.yaml`, separate from `garuda.yaml` (strict version 1 parsing is untouched). v1 is packaged-only: users customize behavior through `garuda.yaml` flows and roles, which starters already resolve. Loading a catalog from a project file is deferred.

```yaml
version: 1
id: plan-feedback
title: Turn feedback into a plan
launch: {kind: flow, flow: plan-only}      # or {kind: run, no_edits: true}
fields:
  feedback: {type: text, required: true}
  current: {type: text}
  desired: {type: text}
  constraints: {type: text}
  sources: {type: source-refs}
brief: plan-feedback                        # packaged brief template
example: reconnect-feedback
```

Reject unknown keys, duplicate IDs, unknown field types, options the launch kind does not support, and missing templates or examples. Validate through the production parser in tests rather than snapshotting YAML.

### 6.2 Compile and preview

`compile_scenario(entry, inputs, workspace) -> LaunchPlan` is read-only and deterministic for unchanged inputs. It reads configuration, source files, receipts, and plan artifacts, but writes no store bytes and starts no process. It produces scenario ID and version, the brief (labelled, delimited sections), the source manifest with digests, the resolved flow or role binding, options, the equivalent existing command, and a digest of all of the above. Field text is data and cannot change permissions. If required input exceeds a size limit, the compile refuses and asks the user to narrow it; nothing is silently dropped.

The CLI recompiles at run time, so it needs no plan token. HTTP preview and start (P3) compare the plan digest and refuse when inputs, sources, or effective config changed.

### 6.3 Shared services

```python
# Proposed interfaces, not current APIs.
class ScenarioService:
    def list(self, workspace): ...                    # catalog + readiness, pure
    def preview(self, entry_id, inputs, workspace): ...
    async def start(self, plan, *, operation_id=None): ...
    def result(self, session_id): ...                 # read-model projection + next action

class FlowExecutionService:                           # garuda/flows/service.py
    async def run(self, name, task, workspace): ...   # P1b adds: *, checks=()
    async def resume(self, flow_session): ...
```

In P1a the service has no `checks` parameter, so no caller can request flow checks before P1b implements §5.3.

Extract `run_flow` orchestration (resolution, missing-role refusal, `FlowRunner`, result rendering) into `FlowExecutionService`. Add baseline/delta capture and verification finalization at the engine's owning lifecycle as defined in §5.3. Preserve existing step receipt, artifact, review, and fail-closed recovery contracts. The CLI renders the shared result; HTTP later calls the same service, never a shelled-out `garuda`. `run`-kind starters call the existing role-run setup through one shared request function; if that setup is currently CLI-only, extract it once into `agents/setup.py` or a dedicated shared service rather than duplicating its checks.

### 6.4 CLI

```text
# Proposed CLI family
garuda starter list [--json]
garuda starter show ID
garuda starter run ID [field flags] [--source PATH[#SECTION]] [--preview] [--json]
garuda starter run build-review --variant pair --plan-artifact FLOW:STEP:ATTEMPT
garuda starter result SESSION [--json]
garuda starter example reconnect DIR
```

Use **Starters** in dashboard navigation and `garuda starter` for the proposed public CLI. Existing `garuda run`, `flow`, and `recipe` commands stay valid. Internal package/service names and HTTP resources retain `scenarios`; they are not additional user-facing command families. The pair starter variant requires the validated plan input described in §4.3.

### 6.5 HTTP and dashboard

| Route | Phase | Behavior |
|---|---|---|
| `GET /api/scenarios` | P2 | Catalog and readiness; pure |
| `GET /api/scenarios/{id}` | P2 | Fields, requirements, example |
| `POST /api/scenarios/preview` | P2 | Compile; returns plan, digest, and equivalent command; starts nothing |
| `GET /api/scenario-runs/{id}` | P2 | Result and next action from the read model |
| `POST /api/scenarios/start` | P3 | Start a revalidated plan with an operation ID |

All routes reuse the dashboard token, origin/host checks, workspace allowlist, request limits, and (for P3) write enablement, permission ceiling, and approval broker. POST bodies select installed starters and supply data; they never carry definitions.

**P3 launch safety (only for the new write route).** Persist a small private launch-intent record keyed by operation ID and plan digest, using existing strict-store helpers, before execution. A duplicate POST returns the existing session. If a response is lost after possible execution, return the existing run or an explicit uncertain state; never call a launcher again to recover. A flow start must not open a chat session or lease before `FlowRunner` opens its own. A tab disconnect does not stop work; server shutdown cancels through owned teardown and records interrupted work; a restarted server shows state and never relaunches. `run`-kind starts stay copy-command until a shared native/ACP run facade is wired and tested; do not route ACP roles through `LiveChat`.

## 7. Relationship to recipes

`garuda recipe run` already acquires one workspace lease with a heartbeat for the whole recipe and releases it after its steps. Its parameterised prompt sequences pass prior output forward. They do not provide the flow engine's per-step persisted sessions, session baseline/delta evidence (an open `docs/BACKLOG.md` item), validated typed artifact edges, or independent-review receipts. Describe those actual differences rather than treating recipes as unleased. Starters offer the bounded plan/build/review/check path using existing flows and the verification extension above.

- v1 leaves recipes unchanged.
- `docs/use-cases/automate.md` and `docs/use-cases/index.md` point "chain steps" at `build-review` and keep recipes as the option for custom prompt sequences.
- Correct `docs/use-cases/automate.md`, which currently says "recipes don't take a workspace lease". They do; what they lack is a persisted session and baseline. This fixes an existing doc error, independent of starters, and may land on its own.
- Whether to deprecate recipes is a separate decision; record it in `docs/BACKLOG.md` rather than here.
- Do not reuse the word "recipe" for starters.

## 8. File map

| Responsibility | New | Existing owners |
|---|---|---|
| Catalog and schema | `garuda/scenarios/catalog.py`, `data/*.yaml`, `briefs/*.md` | `flows/packaged.py`, `core/setup_view.py` |
| Compile and sources | `garuda/scenarios/compile.py` (path validation, digests, bounded plan handoff included) | `context/brief.py`, `context/redact.py`, `workspace/paths.py`, `flows/artifacts.py` |
| Service and result | `garuda/scenarios/service.py` | `core/read_model.py`, `interfaces/session_service.py` |
| Flow facade (P1a); parent evidence and checks (P1b) | `garuda/flows/service.py` (P1a); `garuda/core/acceptance_runner.py`, owned asynchronous check execution (P1b) | `interfaces/main.py::run_flow`, `flows/engine.py`, `workspace/evidence.py`, `core/acceptance.py`, `runtime/session_state.py` |
| CLI | `garuda/interfaces/scenario_cli.py` | parser registration in `interfaces/main.py` |
| Dashboard (P2) | `static/views_scenarios.js` | `routes.py`, `router.js`, `views_sessions.js` |
| Launch intents (P3 only) | `garuda/scenarios/launch_intents.py` | `runtime/strict_store.py`, `interfaces/jobs.py` |
| Example and docs | `examples/workflows/reconnect/`, `docs/use-cases/workflows.md` | `docs/index.md`, `docs/use-cases/index.md`, `mkdocs.yml`, CLI reference |

Do not create `coordination/`, `workers/`, `garuda/consult/scenario.py`, a team store, claim states, or workflow frontiers. Keep scenario metadata out of model inference types. `docs/use-cases/` is flat, so add one page instead of a `workflows/` subfolder.

**Example project:** a small local Git repository with a reconnect-status feature and a deterministic `pytest` check, initialized only into a directory the user chooses (`garuda starter example reconnect DIR`). It is ordinary testable code, not a staged transcript. Model IDs come from the user's configuration, never hard-coded. Reviewed examples require supported independent coder/reviewer identities; the setup screen makes that explicit.

**Guide content** (one section per starter): setup requirement, the existing command and its starter equivalent, what each role receives and produces, what Garuda enforces versus what the model decides, and interruption and resume behavior. A static "Demo — no agents running" tour is optional and out of the critical path. OpenRig artwork, recordings, findings, and names are not reused.

## 9. Delivery

Rough engineering days for one contributor familiar with the codebase, including tests and docs.

### Start checklist

1. Branch from the then-current `origin/main`, not from an older local branch. Carry this plan file onto that branch; leave unrelated local changes untouched.
2. Add this plan to `docs/plans/index.md`.
3. Optionally land the `docs/use-cases/automate.md` recipe-lease correction (§7) first as its own small docs change.
4. Add the fallback/consult alias gap (§10) to `docs/BACKLOG.md` as an open item.

### P0: Confirm P1a contracts (0.5–1 day)

- Recheck main and the `run`/`flow run` flags.
- Confirm `plan_role` and catalog construction for readiness start no subprocess and make no network or model call.
- Recheck read-only coexistence and no-edits withholding behavior; keep the idle-checkout guidance for `ask-role`.
- Confirm readiness uses the existing reviewer-independence rules and test both one-harness and distinct-identity setups.
- Validate bounded plan-artifact delivery and use the naming/recipes choices in §11.

**Exit:** no starter relies on an invented flag, persistent team, or unproven vendor capability.

### P1a: CLI starters (3–4 days)

- Catalog, schema, read-only compiler, source validation, and briefs.
- `FlowExecutionService` extraction with **no change to flow execution semantics**. The CLI output of `garuda flow run|show|resume` is unchanged.
- `garuda starter list/show/run/result/example`, including readiness via `check_independent`, the single-harness remedies, result and next action, and the validated Implement this plan handoff.
- Example repository and `docs/use-cases/workflows.md`; update recipes wording and correct the recipe lease statement.

**Exit:**

- On a fresh machine with one connected harness, `garuda init` then `garuda starter run plan-change …` produces a plan. `ask-role` and `run-with-role` work.
- `build-review` reports `needs-setup` with the three remedies, and "Build and check" ends with a verification receipt from existing `run --check`.
- With two harnesses, `build-review` on the example repo produces an independent review and ends with verification `unavailable`.
- Every starter's equivalent command is printed and works.

### P1b: Flow verification (3–4 days, separately reviewed)

**Entry gate:** P1a has landed. Before writing code, confirm against then-current main the parent baseline/delta, owned check finalization, cancellation, and receipt-reconciliation contracts in §5.3, and get the design reviewed.

- Parent baseline and full-flow `delta_changed`, check finalization under the flow's lease, and the owned asynchronous check runner (§5.3).
- `flow run --check` and automatic trusted checks for `build-review`; preview, cards, and docs updated only once this lands.
- Record the verification ownership/evidence contract in `.context/decisions.md` on adoption.

**Exit:** with independent identities and trusted checks, `build-review` on the example repo ends with a candidate-bound verification receipt. The §12 ownership, provenance, and recovery tests pass. The existing synchronous `accept()` caller (`garuda run --check`, via `interfaces/main.py::_accept_session`) keeps its behavior.

P1b depends only on P1a's `FlowExecutionService`. P2 does not depend on P1b, so the two can run in parallel.

### P2: Dashboard library, read-only (2–3 days)

- Starters navigation, cards with readiness and remedies, forms, preview with copy-command.
- Result and next-action panel on the existing session/flow detail.
- Live Chrome check per `docs/development/browser-checks.md`.

**Exit:** a dashboard user can find a starter, see whether it is ready, preview it, copy the exact command, and follow the resulting run. No new write route exists.

### P3: One-click start (3–4 days, separately approvable)

- `POST /api/scenarios/start` with launch intents, digest revalidation, and lifecycle semantics (§6.5).
- Flow starts first; `run`-kind starts only after a shared native/ACP run facade passes its tests.

**Exit:** a duplicate or interrupted POST never produces a second or silent execution, and server/client interruption never replays work.

**Total:** about 12–16 days (P0 0.5–1, P1a 3–4, P1b 3–4, P2 2–3, P3 3–4). P1a, about 4 days in, delivers usable CLI starters for single- and multi-harness users. P1b adds trusted checks to reviewed flows; later phases add dashboard discovery and one-click launch.

## 10. Deferred, with reasons

| Deferred | Reason |
|---|---|
| Human-facing consult facade | An idle-checkout role run covers bounded human questions without an asker facade. Revisit separately if users need snapshot isolation from a running editor. |
| General repository-document extraction and pasting | Keep document grounding as named files. Bounded validated flow-artifact and session-brief handoffs already have distinct mechanisms. |
| Project-supplied catalogs | Expands the trust surface; `garuda.yaml` flows already customize behavior. |
| Natural-language progress summaries | Deterministic next actions are enough; if added, use a frozen snapshot and cite IDs. |
| `--bg` and `--isolation` for flows | Not supported by the flow runner today; a separate change. |
| Recipe deprecation | A separate product decision. |
| Canonical alias comparison for fallback and consult identities | A pre-existing gap in `flows/review.py::identities`, which compares raw `harness` strings for fallback and consult entries. Fix it in the review engine with its own test, so flows and readiness both benefit; tracked in `docs/BACKLOG.md`. |

## 11. Planning choices for implementation

1. **User-facing noun.** Use **Starters** in the UI and `garuda starter …` on the CLI, keeping `scenarios` for internal modules and HTTP resources. Existing flow and recipe names remain unchanged.
2. **`ask-role` default role.** Use configured `reviewer` (no-edits, proposed by `init`), with any compatible configured role selectable. Preserve its effective authority and show live-checkout limitations.
3. **`build-review` with no checks configured.** Permit execution if its review requirements are satisfied; end with unavailable verification and the `garuda init --project` remedy. Do not confuse optional checks with mandatory independent-review readiness.
4. **Recipes position.** Keep recipes for custom parameterised prompt sequences; point the common bounded plan/build/review/check journey at starters. No recipe deprecation is adopted here.
5. **Single harness.** Offer, in order: connect a second harness; Build and check (no review); or a user-authored `independent: false` flow override labelled "review not independent". Starters never waive independence themselves.
6. **Independence rule.** Readiness calls the existing `check_independent` with configured resolved role plans and respects the effective review policy. Primary runtimes are compared canonically; fallback and consult aliases are not, until the separate `review.py` fix (§10). Do not turn a waiver into a claim of independence. Any stricter rule for unknown model IDs is out of scope and needs its own change.

## 12. Tests

Follow the test-audit authoring gate. Test new contracts at their owning boundary; do not re-test flow, consult, or queue behavior because a starter invokes it, and do not snapshot YAML or prompt text without a behavioral purpose.

| Contract | Validation | Owner |
|---|---|---|
| Catalog honors config | A same-name `garuda.yaml` flow is what previews and runs; a missing role refuses before any session | `tests/test_scenarios.py` |
| Reviewer readiness | Use explicit expected cases: identical identities and two aliases of the same primary runtime/model report needs-setup; distinct supported runtimes are ready when other requirements pass; configured fallback/consult collisions refuse under the existing rule. Exercise resolved role plans and actual flow enforcement; do not compute expected verdicts by calling the production helper under test | `tests/test_scenarios.py`, existing review owner coverage |
| Waiver visibility | A user-authored `independent: false` flow is runnable despite identical review identities when other requirements pass, and every readiness/result surface shows "review not independent" from the configured/recorded policy; no packaged starter or flag produces a waiver | `tests/test_scenarios.py` |
| Single-harness build path | "Build and check" compiles to `garuda run --role coder --check …` and its result shows verification from the real acceptance receipt, with no review claimed | `tests/test_scenarios.py` |
| Flow facade parity (P1a) | `garuda flow run/show/resume` exit codes, receipts, and output are unchanged after extraction | `tests/test_flows.py` |
| Fields reach execution | Capture the actual runtime input; requirements, exclusions, and constraints survive compile and Implement this plan | `tests/test_scenarios.py` |
| Source boundaries | Escaping path and outside symlink refuse; a digest change after preview refuses; a cross-project session ref is denied | `tests/test_scenarios.py` |
| Plan handoff | Capture actual next-run input with the full validated plan and original constraints; forged, missing, ambiguous, oversized, or wrong-project artifacts refuse before launch | `tests/test_scenarios.py` |
| Flow check ownership (P1b) | A competing real mutating session is refused between the final step and receipt publication; cancellation/uncertain teardown cannot release ownership or claim a pass | `tests/test_flows.py` |
| Flow check provenance (P1b) | Parent evidence includes all-step changes, including a changed/restored check-dependent infrastructure file; final fingerprint mismatch or check mutation invalidates verification; final state preserves the recorded check verdict | `tests/test_flows.py` |
| Flow checks and recovery (P1b) | A passing check records parent verification; review alone stays unverified; untrusted/unsupported checks cannot pass; incomplete flows run no checks; a launched check lacking a terminal receipt is not automatically replayed on resume | `tests/test_flows.py` |
| Pure reads | `list`, `preview`, `result`, and GET routes change no store bytes and start no subprocess; a missing receipt shows unknown | `tests/test_scenarios.py`, `tests/test_read_model.py` |
| CLI/HTTP parity | Same plan digest and equivalent command from both surfaces | `tests/test_scenarios.py` |
| P3 launch safety | Duplicate POST returns the same session; interruption around intent persistence and launch yields one run or explicit uncertainty | dedicated file if the boundary warrants it |
| Example honesty | The example repo's documented commands run in a clean checkout through production parsers | `tests/test_scenarios.py` or docs check |
| Dashboard | Live Chrome: library, preview, copy command, result panel; stale preview and token/origin rejection in P3 | browser checks |

```bash
pytest tests/test_scenarios.py -q
pytest tests/test_flows.py tests/test_read_model.py tests/test_setup_view.py -q
pytest -q
ruff check garuda tests
python scripts/check_docs.py --commands
```

State when live Docker, vendor harness, OS-sandbox, tmux, or Harbor checks were not run. A mocked adapter is not proof of vendor capability.

## 13. Definition of done

A user who has connected an agent runs `garuda init`, discovers the five starters through `garuda starter` and the dashboard's Starters view, and sees exact per-starter setup remedies. With one harness, the planning, question, and role-run starters work immediately, and building has a working path ("Build and check"). Independent reviewed runs require independent identities as decided by the existing `check_independent`, and any waiver is user-authored and visibly labelled. Launches use existing Garuda authority and produce the actual promised artifacts, review where applicable, candidate-bound trusted verification where supported checks exist, and a deterministic next action. A selected plan is delivered through the validated bounded handoff, not merely referenced by an unreadable identifier. No starter promises durable assignments, live specialist messaging, persistent lead memory, or automatic human-gated advancement.

Docs updated with the behavior: `docs/index.md`, `docs/use-cases/index.md`, `docs/use-cases/automate.md`, `mkdocs.yml`, the CLI reference, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, the roadmap, and `docs/plans/index.md`. When implemented, record adopted public-boundary/security decisions in `.context/decisions.md`, including naming/recipes and the flow verification ownership/evidence contract. This plan alone does not edit durable decisions or generated context.
