# Documentation refresh design

**Date:** 2026-09-30

**Status:** Approved

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
  State which subcommands accept each flag: the three network/unsandboxed flags
  exist only on `garuda run`, while `--docker-image` (and `--docker-host`) are
  also accepted by `chat`, `serve`, and `recipe run`. Do not imply that `chat`
  or `serve` can relax sandbox network egress.
- Extend `scripts/check_docs.py` and the documentation-contract tests with:
  - extraction of `garuda ...` invocations from shell-like fenced code blocks
    **and from inline code spans** in maintained documentation, so the
    command-dense tables in `docs/reference/cli.md` are covered, not just
    fenced examples;
  - structural validation against `garuda.interfaces.main.build_parser()`
    without dispatching commands or reading credentials (see "Command checker
    mechanics" below);
  - duplicate Markdown table-row detection.

#### Command checker mechanics

- **Walk the parser; do not call `parse_args`.** `parse_args` raises
  `SystemExit` on `--help`, missing required arguments, and unknown flags, and
  `required=True` options (for example `runtime handoff --session/--to` and the
  six required `eval dual-model report` options) would make every abbreviated
  example fail. The checker instead resolves the subcommand chain through the
  parser's subparser actions, then checks each `-x`/`--flag` token against that
  subparser's option strings and, where the action declares `choices`, the
  supplied value. Unknown subcommands, unknown flags, and invalid choice values
  fail; missing required options are not a failure.
- **Reference-syntax normalization.** Inline reference forms are normalized
  before checking: `[...]` optional groups are unwrapped and checked,
  `<placeholder>` and single-letter placeholders (`S`, `R`, `W`) stand for
  values, and a trailing `...` or `…` means "arguments elided". Unknown flags
  inside optional groups still fail. Whitespace inside an inline code span is
  collapsed so wrapped commands remain one invocation. Generic metasyntax such
  as `garuda <command> --help` must be replaced by a concrete command or carry
  the same explicit opt-out marker as a non-executing example.
- **CI placement.** The `docs-contract` CI job is deliberately stdlib-only: it
  runs `python scripts/check_docs.py` on a bare Python 3.12 install, and
  `garuda.interfaces.main` imports LiteLLM, the web interface, and the tool
  registry at module load. The command check must therefore either (1) install
  the package in that job with `pip install -e . -c constraints.txt`, updating
  the job's "stdlib only" comment, or (2) import `build_parser` lazily and run
  only where the package is installed (the main test job), while link, README,
  test-count, and duplicate-row checks stay stdlib-only. Choose (2) so the
  existing contract stays cheap and dependency-free; the checker must then fail
  loudly, not skip silently, when invoked with `--commands` and the package
  cannot be imported. Parser-backed repository checks live in a separate
  `tests/test_docs_commands.py`, not in the stdlib-only
  `tests/test_docs_contract.py`. The installed-package CI job runs that test and
  an explicit `python scripts/check_docs.py --commands` step.

The command checker scans current documentation, not historical or planning
records. At minimum it covers the root README, `docs/index.md`, plus active
pages under `docs/guides/`, `docs/reference/`, `docs/evaluation/`, and
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
flags pass; prove unknown commands, unknown flags, invalid choice values, and
duplicate rows fail; prove an abbreviated example that omits a required option
passes; prove inline-span and optional-group forms are checked; then run
repository-level assertions through both pytest and
`python scripts/check_docs.py`. Fixture parsers should be small, locally built
`argparse` trees so the fixture tests stay stdlib-only. Parser-independent
fixtures remain in `tests/test_docs_contract.py`; the repository assertion
using the real `build_parser()` lives in `tests/test_docs_commands.py`.

### PR (b): first-time and everyday workflows

Branch from the updated `origin/main` after PR (a) merges.

- Rewrite `docs/guides/getting-started.md` as a safe first-session tutorial.
- Rewrite `docs/guides/using-garuda.md` as task-oriented everyday workflows.
- Update `docs/index.md` with the single three-column feature matrix and
  runtime-difference notes needed by those two guides. Matrix rows link only to
  pages that exist when PR (b) merges; rows for the safety/workspaces guide
  point at the current sections of `configuration.md` and `using-garuda.md`,
  and PR (c) retargets them. The link checker would otherwise fail PR (b).
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

If design and plan records move, the command checker's exclusion list moves
with them in the same PR. Plans and specs describe proposed, unshipped commands,
so a move to `docs/design/` or `docs/plans/` that leaves the exclusion keyed on
`docs/superpowers/` would pull those records into the command check and fail
it. This spec is itself one of the records that may move.

## Verification

Each PR runs the narrowest relevant checks first. PR (a) must run:

```bash
pytest tests/test_docs_contract.py -q
python scripts/check_docs.py
ruff check scripts/check_docs.py tests/test_docs_contract.py
python scripts/check_docs.py --commands   # needs the installed package
```

PR (a) also confirms, from the CI run on the PR, that the stdlib-only
`docs-contract` job still passes without the package installed, and that the
command check runs (not skips) in the job that installs it.

PRs (b) and (c) run the docs contract after every content change. Command
examples are validated with both `python scripts/check_docs.py` and
`python scripts/check_docs.py --commands`, not manually assumed valid. Link
and navigation changes are additionally reviewed from a clean checkout of the
PR branch. No live provider call is required for acceptance.

## Delivery order

The pull requests are strictly ordered `(a) -> (b) -> (c)`. Each later branch
starts from the merged predecessor rather than stacking indefinitely on an
unmerged branch. Only the `Darshan2104` GitHub account may be used to push,
open, comment on, or merge these pull requests.
