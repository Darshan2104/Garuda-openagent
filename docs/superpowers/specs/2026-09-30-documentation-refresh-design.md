# Documentation refresh design

**Date:** 2026-09-30

**Status:** Approved direction, awaiting written-spec review

**Scope:** User-facing documentation only, delivered as three pull requests

## Problem

Garuda's user documentation no longer matches the product on `origin/main`.
The root README still calls ACP supervision planned even though ACP execution
and handoff have shipped, the first-run guide stops after three commands, the
CLI reference duplicates a command, and several workspace safety flags appear
only in archived material. Advanced behavior is documented, but it is split
between reference pages and recent design records instead of the guides where
operators look for it.

The refresh must serve two audiences without creating a second monolithic
manual:

1. A first-time user who needs a safe, explicit route from installation to a
   comprehensible first session.
2. An advanced operator who needs exact runtime, workspace, permission,
   integration, observability, and evaluation behavior.

## Goals

- Make a read-only run the default documented first experience.
- Explain cost, credential, confinement, and verification boundaries before a
  user starts a run.
- Describe every shipped user-facing feature once in the guide where its
  operator naturally looks for it, then link to it from a single feature map.
- Move ACP operating guidance out of the CLI reference and into the external
  harness guide while keeping the reference concise.
- Document currently missing workspace and network flags.
- Make command validity and duplicate table rows part of the repository's
  documentation contract.
- Deliver from `origin/main` in three small, ordered pull requests to reduce
  conflicts with the fast-moving runtime work.

## Non-goals

- No behavior changes to Garuda, its parser, or runtime protocols.
- No invented command output and no paid live-model run solely for screenshots
  or examples.
- No rewrite of contributor-facing `docs/ARCHITECTURE.md` or `docs/MODULES.md`.
- No duplication of dual-model or paired-report material already maintained in
  `docs/evaluation/`.
- No new catch-all `advanced-usage.md` page.

## Information architecture

The documentation uses progressive disclosure:

1. `README.md` remains a short product introduction, safe quick start, and
   navigation page.
2. `docs/index.md` is the only documentation map. Its three-column feature
   matrix contains **Feature**, **Command**, and **Guide**. Runtime-dependent
   rows state when native and ACP behavior differs.
3. `docs/guides/getting-started.md` guides a new user through prerequisites,
   installation, model credentials, a read-only first run, interpreting the
   session record, safe next steps, cost awareness, and common failures.
4. `docs/guides/using-garuda.md` covers everyday workflows: run modes,
   sessions, profiles, skills, subagents, MCP, recipes, dashboard, SDK, and
   service entry points. It links to specialist guides instead of restating
   their details.
5. `docs/guides/external-harnesses.md` owns ACP runtime discovery, selection,
   routing, execution, handoff, resume, recovery, reclaim, support bundles,
   authentication boundaries, and result-verification limitations.
6. A dedicated safety and workspaces guide owns permissions, hooks, project
   tools, local/sandbox/tmux/Docker/remote workspaces, network controls, and the
   distinction between guardrails and confinement.
7. `docs/evaluation/` remains the authority for dual-model collection,
   trajectories, paired reports, and evaluation workflows; user guides link to
   those pages.
8. `docs/reference/cli.md` remains an inventory and flag reference. It does not
   carry long workflow explanations.

## Safety and truth rules

- The first documented run uses `--mode readonly` and tells the user to run it
  in a workspace they intentionally selected.
- Documentation states that model-backed runs can cost money and that the
  current default is DeepSeek through OpenRouter. It never presents a provider
  secret as literal example data.
- Docker is described as the isolation boundary for untrusted work. Permission
  rules, macOS Seatbelt, and the `sandbox` workspace kind are described as
  guardrails or blast-radius reduction, not general host-read confinement.
- ACP runs are described separately from native runs: Garuda records their
  lifecycle and workspace delta but does not apply the native completion
  verifier to an ACP result.
- User-owned vendor authentication stays in the vendor CLI. Documentation does
  not instruct Garuda to read, copy, or proxy OAuth credentials.
- Examples describe the shape of output (status, session identifier, storage
  location, events) without fabricating a transcript or success result.
- The merged `origin/main` implementation and `garuda <command> --help` are the
  source of truth. Roadmaps and archived documents are not treated as current
  behavior.

## Pull request plan

### PR (a): correctness and enforceable contracts

Start from `origin/main`.

- Replace the README's planned-ACP statement with concise shipped behavior and
  an explicit note that ACP results are not verified by Garuda.
- Make the README quick start read-only and add a brief cost warning.
- Remove the duplicated `garuda runtime support` reference row.
- Add `--allow-unsandboxed`, `--allow-network`, `--no-network`, and
  `--docker-image` to the maintained CLI reference with their safety semantics.
- Extend `scripts/check_docs.py` and `tests/test_docs_contract.py` with:
  - extraction of `garuda ...` invocations from shell-like fenced code blocks
    in maintained documentation;
  - validation against `garuda.interfaces.main.build_parser()` without
    dispatching commands or reading credentials;
  - duplicate Markdown table-row detection.

The command checker scans current documentation, not historical or planning
records. At minimum it covers the root README plus active pages under
`docs/guides/`, `docs/reference/`, `docs/evaluation/`, and
`docs/development/`; it excludes `docs/archive/`, `docs/roadmap/`, and
`docs/superpowers/`. Shell continuations are joined before parsing. Prompts and
placeholders are normalized only where the parser needs a value; unknown
commands and flags still fail. Explicitly non-executing examples must use a
documented opt-out marker on the fence or command line, with the exception kept
rare and reviewable.

Duplicate-row detection compares normalized cell text within each Markdown
table, reports both locations, and ignores separator rows. It applies to the
same maintained documentation set.

Tests are fixture-first: prove valid nested subcommands and representative
flags pass; prove unknown commands, unknown flags, and duplicate rows fail;
then run repository-level assertions through both pytest and
`python scripts/check_docs.py`.

### PR (b): first-time and everyday workflows

Branch from the updated `origin/main` after PR (a) merges.

- Rewrite `docs/guides/getting-started.md` as a safe first-session tutorial.
- Rewrite `docs/guides/using-garuda.md` as task-oriented everyday workflows.
- Update `docs/index.md` with the single three-column feature matrix and
  runtime-difference notes needed by those two guides.
- Keep provider, runtime, and workspace setup links shallow and explicit.

The tutorial stops short of claiming a particular model response. It explains
what the user should observe: streamed activity, terminal status, a session ID,
and a persisted session beneath the configured session directory.

### PR (c): specialist guides and reorganization

Branch from the updated `origin/main` after PR (b) merges.

- Expand `docs/guides/external-harnesses.md` into the complete ACP operations
  guide and move the long handoff lifecycle explanation out of the CLI
  reference.
- Add a lower-case, hyphenated safety/workspaces guide for permission posture,
  trusted hooks and tools, workspace kinds, Docker and remote execution, and
  network controls.
- Update `docs/guides/configuration.md`, the web dashboard guide, CLI reference,
  evaluation navigation, and `docs/index.md` only as needed to connect those
  specialist guides without duplicating them.
- Resolve confusing documentation storage:
  - decide whether `docs/superpowers/` records should move to neutral
    `docs/design/` and `docs/plans/` locations, with link updates in the same
    PR, or be retained but clearly indexed and explained;
  - rename or retitle `docs/DECISIONS.md` so it is clearly a major-changes
    ledger, distinct from the durable decisions in `.context/decisions.md`.

The organization decision will be based on repository references and git
history. Existing provenance must be preserved; files are moved with history
where practical rather than copied into duplicate locations.

## Verification

Each PR runs the narrowest relevant checks first. PR (a) must run:

```bash
pytest tests/test_docs_contract.py -q
python scripts/check_docs.py
ruff check scripts/check_docs.py tests/test_docs_contract.py
```

PRs (b) and (c) run the docs contract after every content change. Command
examples are validated by the contract, not manually assumed valid. Link and
navigation changes are additionally reviewed from a clean checkout of the PR
branch. No live provider call is required for acceptance.

## Delivery order

The pull requests are strictly ordered `(a) -> (b) -> (c)`. Each later branch
starts from the merged predecessor rather than stacking indefinitely on an
unmerged branch. Only the `Darshan2104` GitHub account may be used to push,
open, comment on, or merge these pull requests.
