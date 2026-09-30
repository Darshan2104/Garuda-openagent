# Documentation refresh implementation plan

## Scope

Refresh Garuda's user documentation for first-time users and advanced
operators, with executable documentation contracts, in three ordered pull
requests based on the merged `origin/main` branch.

## PR (a): correctness and enforceable contracts

1. Extend `scripts/check_docs.py` with a maintained-document selector, fenced
   and inline Garuda-command extraction, structural argparse validation behind
   `--commands`, and duplicate Markdown table-row detection.
2. Add dependency-free fixture coverage to `tests/test_docs_contract.py` and
   real-parser repository coverage to `tests/test_docs_commands.py`.
3. Wire `python scripts/check_docs.py --commands` into the installed-package CI
   job while preserving the dependency-free docs-contract job.
4. Correct the README's ACP status and safe quick start, remove the duplicate
   CLI row, and document the missing workspace and network flags.
5. Run focused tests, both checker modes, lint, and the full relevant CI gates;
   review, open, and merge the PR using `Darshan2104` only.

## PR (b): first-time and everyday workflows

1. Branch from `origin/main` after PR (a) merges.
2. Rewrite `docs/guides/getting-started.md` around a safe read-only first run,
   cost awareness, session evidence, and troubleshooting.
3. Rewrite `docs/guides/using-garuda.md` around common tasks and clear links to
   specialist material.
4. Turn `docs/index.md` into the single documentation map with a three-column
   feature matrix and explicit native-versus-ACP notes.
5. Run both checker modes, review the rendered information flow, then open and
   merge the PR through `Darshan2104`.

## PR (c): specialist guides and organization

1. Branch from `origin/main` after PR (b) merges.
2. Move ACP operating guidance into `external-harnesses.md` and keep the CLI
   reference concise.
3. Add a dedicated lower-case safety/workspaces guide and connect configuration,
   dashboard, evaluation, and index navigation without duplicating content.
4. Resolve the `docs/superpowers/` and `docs/DECISIONS.md` naming confusion,
   preserving history and moving command-check exclusions with design records.
5. Run both checker modes and relevant tests, review links from a clean
   checkout, then open and merge the final PR through `Darshan2104`.
