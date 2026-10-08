# Starter P0 contract confirmation

**Status:** P0 evidence for [issue #303](https://github.com/Darshan2104/Garuda-openagent/issues/303), checked 2026-10-07 against `origin/main` at `d47c16b3c387e264e4562dccc360da0e529591f8`.

The [approved revision-5 plan](2026-10-06-ready-to-use-workflow-scenarios.md)
is published byte-for-byte, with SHA-256
`d39a83cb30a2149b273194fcbeab0bed6652be6d8697819fd65c9d886d06dca9`.
This record confirms existing contracts and constrains P1a implementation;
it does not ship starter commands, a catalog, or flow checks. The implementation
queue is [epic #302](https://github.com/Darshan2104/Garuda-openagent/issues/302).

## Command and owner contracts

| Boundary | Current owner and evidence | P1a constraint |
|---|---|---|
| Flow parsing | `interfaces/main.py::build_parser`: `flow run NAME -t/--task --workspace`; `flow show FLOW_SESSION`; `flow resume FLOW_SESSION` | No flow `--bg`, `--isolation`, `--check`, or invented role flags |
| Role run | Same parser: `run --role --name --isolation --bg --no-edits --check` | Forward only supported options through the existing native/ACP admission path; `--name` names a session, not an assignment |
| Effective flows | `flows/packaged.py::available`, `required_roles`, `missing_roles`; `garuda_yaml.py::load_effective` | Same-name user/trusted-project flow definitions replace packaged definitions; preview and launch resolve the same effective flow; missing roles refuse before a session |
| Execution | `interfaces/main.py::run_flow`, `flows/engine.py::FlowRunner`, `flows/launch.py::launch_step` | Extract shared orchestration once into `flows/service.py`; keep lifecycle, receipts, rendering, exits, and recovery unchanged; no `checks` argument in P1a |
| Review policy | `flows/review.py::check_independent`, `engine.py::_review_loop` and `_independence` | Call the existing predicate for configured readiness; execution retains its checks against actual launched/consulted identities |
| Verification | `core/acceptance.py::accept`, `interfaces/main.py::_accept_session` | Build-and-check uses existing `run --check`; flow review alone has verification `unavailable` until P1b |
| Workspace | `workspace/lease.py`, `workspace/no_edits.py::NoEditsGuard` | A read-only lease can coexist with a writer; a changed or incomplete live-checkout comparison withholds the answer |
| Recipes | `config/recipes.py::run_recipe` | One lease and heartbeat already cover the sequence; recipes lack flow step sessions, typed edges, review receipts, and session baseline/delta evidence |

Public naming is **Starters** and **garuda starter**. The five IDs are
`plan-change`, `plan-feedback`, `build-review`, `run-with-role`, and `ask-role`;
launch kinds are only `flow` and `run`. The compiler/service adds no persistent
team, inbox, lead, assignment, authority, or vendor capability.

## Static readiness and the Setup discovery caveat

`agents/setup.py::prepare_runtime_catalog` reads trusted global runtime
manifests and project references. Its registry rejects unknown/disabled
runtimes and canonicalizes aliases. `runtime/roles.py::plan_role` performs
registry lookup and configured-model validation without discovery, fallback
selection, login, inference, sessions, or queue admission. Packaged-flow
inspection is also non-executing. Project-root resolution is a filesystem
walk (`core/sessions.py::project_root`), not a Git subprocess.

**P0 finding:** the full `core/setup_view.py::setup` entry point is unsuitable
for pure starter readiness as currently written. `_harness_diagnostics` calls
`catalog.discover(cache_ttl=60.0)` for used harnesses. With an installed
executable and a missing/stale cache this can run version/auth probes and write
probe conclusions. The existing `test_viewing_setup_runs_no_vendor_command_and_writes_nothing`
uses missing executables, so it cannot establish purity for installed ones.
Treat revision 5's phrase "builds on `setup_view.setup()`" as reuse of static
configuration/projection contracts, not a call to this discovery entry point.

For [#306](https://github.com/Darshan2104/Garuda-openagent/issues/306), compose
readiness from effective config, the trusted non-executing catalog, role plans,
and read-only evidence. If shared Setup projections need extraction, keep that
in the owning module; do not copy its discovery loop or alter existing Setup
discovery behavior as part of starters. Explicit doctor/runtime inspection
remains the way to gather missing evidence.

Evidence readable without execution:

- Effective roles, flow policy/source, configured exact model/effort, withheld
  project values, trusted manifest version/capability declarations, and global
  disablement. Declarations are not proof of a currently working adapter.
- `acp/login_probe.py::cached_login` reads prior conclusions without probing.
  Missing evidence stays `not-checked`; a no-probe manifest does not establish
  authentication. Prior conclusions retain their timestamp and meaning.
- ACP model/effort support is version-bound in `roles.py::PROVEN_OPTIONS`, and
  the actual session must offer the requested exact values at launch. Static
  readiness does not call an agent to establish those facts. Discovery cache
  contents are not a reason to call `discover()` during a read.

`tests/test_roles.py::test_static_role_planning_executes_nothing_and_preserves_store_bytes`
exercises effective config, catalog construction, canonical role planning, and
packaged-flow inspection with a registered executable that exists. It forbids
subprocesses, socket connections, model construction, discovery, and fallback
selection, and compares bytes across workspace/settings/session/lease roots.
This proves the existing building blocks; actual starter list/preview/result
purity remains an implementation gate in #305–#308.

## Independence, waivers, and live-checkout questions

The current comparison is exact `(runtime_id, model_id)` membership. Two primary
aliases resolving to the same runtime/model collide. Different canonical
runtimes, or different exact model IDs on one runtime, pass this identity rule
when the other requirements pass. An omitted model ID means the harness default;
do not reinterpret it as proof of a particular model or add a stricter unknown-ID
rule locally. Single-harness init supplies the same coder/reviewer identity,
so independent reviewed work needs setup.

Configured fallbacks and consulted roles still contribute raw `harness`
strings in `review.py::identities`; aliases there can evade comparison. The
[open backlog](../BACKLOG.md) links the separate review-engine fix
[#310](https://github.com/Darshan2104/Garuda-openagent/issues/310). P1a inherits
and discloses this limit. The existing review test's "fallback alias" case is a
matching raw identity, not proof of canonical fallback-alias resolution.

Explicit user-authored `review.independent: false` permits a waived review;
the engine records `policy: waived` and its identity decision. Starter readiness
and results must label it **review not independent**. No packaged starter,
default, flag, or remedy silently produces the waiver. Remedies remain ordered:
connect a second harness; build-and-check with no review; copy/edit a flow to
author a visible waiver. Missing checks are a verification warning, not a
review-independence failure.

The read-only lease coexistence test and no-edits owner tests establish why
`ask-role` must recommend an idle checkout: another editor can change the
manifest and cause withholding even if the question's agent writes nothing.
This is a live-checkout run, not a consult snapshot or a sandbox guarantee.

## P1a input and provenance contract

The following implementation contract is owned by
[#305](https://github.com/Darshan2104/Garuda-openagent/issues/305) and
[#307](https://github.com/Darshan2104/Garuda-openagent/issues/307); the cross-flow
handoff does not exist yet.

1. Keep approved structured fields together with the compiled task and source
   provenance. Requirements, exclusions, and constraints must survive both
   initial execution and explicit plan follow-up. Persist bounded, redacted
   inputs through ordinary launch/session metadata, never generated `.context/`
   files or unbounded transcripts. Legacy inputs that cannot be recovered need
   explicit user-supplied constraints.
2. Repository sources remain workspace-contained named files with optional
   sections and content digests. Reject escapes/outside symlinks, and revalidate
   before launch; models read named files with their own authorized tools.
3. `FLOW:STEP:ATTEMPT` resolves only within the current project, selecting exactly
   one completed authoritative producer receipt and one `plan` output. Check
   producer session/step/attempt linkage, not merely a caller-supplied reference.
   Do not accept caller-supplied store paths. Use `flows/artifacts.py::load` for
   artifact-version, containment, regular-file, size, and SHA-256 validation.
   Plans are not workspace-bound artifacts; that differs from patch/review data.
4. Reuse the artifact owner's 64,000-character plan bound. For the starter
   compiled task, use a 128,000-character total delivery budget, with at most
   512,000 UTF-8 bytes; apply the budget after redaction, escaping, labels, and
   all required fields. Refuse required input that exceeds it; do not silently
   trim the plan or constraints. These are host delivery limits, not a promise
   that a vendor accepts that much context.
5. Deliver the full validated/redacted plan inside a labelled, escaped data
   envelope following `FlowRunner._prompt`'s convention. Record original
   artifact identity/digest and delivered-input digest/provenance. Preview,
   launched task, and the shell-quoted equivalent existing command carry the
   same approved data. Revalidate inputs/artifacts at launch. Reads never
   execute a next action.
6. Existing separately authorized session briefs remain distinct:
   `context/tags.py::resolve` owns same-project selection and explicit
   cross-project user grants; `brief.py::render` owns bounded/redacted data.
   Passing `workspace` when building a brief can capture Git evidence, so the
   pure preview path must not use live workspace capture. Readiness needs no
   brief. P1a must preserve the grant and evidence semantics at the actual
   handoff boundary; a plan-artifact selector never grants cross-project access.

## Validation and next issues

Run focused owner coverage and documentation checks from the implementation
worktree:

```bash
python3.12 -m pytest tests/test_roles.py tests/test_flow_review.py tests/test_setup_view.py tests/test_workspace_lease.py tests/test_no_edits.py tests/test_flows.py tests/test_session_tags.py tests/test_acp_discovery.py -q
python3.12 scripts/check_docs.py --commands
ruff check garuda tests
mkdocs build --strict
python3.12 scripts/check_diagram_links.py site
```

Production-parser inspection confirms the supported run/flow flags and rejects
flow checks/background/isolation today. The PR records actual check outcomes;
this document does not maintain test counts. No live vendor, Docker, OS-sandbox,
tmux, Harbor, or dashboard behavior is certified by this P0 change.

After P0, [#304](https://github.com/Darshan2104/Garuda-openagent/issues/304)
extracts the flow facade, then #305–#309 implement the first CLI release.
The P1b design gate remains after P1a has landed; P2 can proceed after P1a
independently of P1b. P3 remains separately approvable after P2. The recipe-doc
correction [#317](https://github.com/Darshan2104/Garuda-openagent/issues/317)
is optional and is not a P0 exit gate. No adopted runtime/security decision or
session-generated context changes in P0.
