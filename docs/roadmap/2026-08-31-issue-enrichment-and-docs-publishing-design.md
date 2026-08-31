# Issue enrichment and documentation publishing design

**Status:** approved design; implementation pending review

**Date:** 2026-08-31

**Repository:** `Darshan2104/Garuda-openagent`

## Outcome

Garuda's ACP roadmap issues become self-contained implementation briefs. A
contributor can understand the intended behavior, boundaries, dependencies,
tests, and completion evidence without reconstructing context from planning
conversations. Dependencies link directly to GitHub issues and remain consistent
with the existing epic/sub-issue hierarchy.

The repository documentation becomes a searchable MkDocs Material site deployed
from `main` to:

`https://darshan2104.github.io/Garuda-openagent/`

## Scope

This work covers:

1. Enriching the nine roadmap epic issues and 35 implementation sub-issues.
2. Replacing plain dependency identifiers with direct GitHub issue links.
3. Correcting malformed Markdown, including literal escaped newlines.
4. Keeping an auditable repository-side issue map and synchronization utility.
5. Configuring MkDocs Material and a curated documentation navigation tree.
6. Adding strict documentation validation and GitHub Pages deployment.
7. Enabling and verifying the repository's GitHub Pages environment.

This work does not implement ACP runtimes, harness adapters, runtime routing, or
other roadmap product features.

## Design decisions

### Canonical planning source

The repository remains the canonical planning source. The existing
`docs/roadmap/issue-catalog.md` contains the substantive issue specifications.
A machine-readable YAML mapping at `docs/roadmap/issue-map.yml` records each
roadmap key, GitHub issue number, parent epic, priority, and dependency keys. The
mapping does not duplicate issue prose.

The synchronization utility combines the catalog and mapping to validate or
update live GitHub issues. It supports a read-only dry run and requires an
explicit apply flag before making remote changes.

GitHub issue comments, implementation discussion, and completion evidence remain
GitHub-owned. Synchronization updates the managed issue description but does not
delete comments, labels, assignees, milestones, or project fields.

### Issue contract

Every implementation issue uses this ordered structure:

1. **Outcome** — the observable result and why it matters.
2. **Current context** — existing Garuda behavior and the gap being addressed.
3. **Implementation scope** — concrete components, interfaces, state, and flows.
4. **Suggested repository areas** — files or modules to inspect, expressed as
   guidance rather than a requirement to preserve current filenames forever.
5. **Dependencies** — direct Markdown links to prerequisite GitHub issues, or
   `None` when the issue has no prerequisite.
6. **Implementation sequence** — ordered steps that allow a contributor to start
   work without inventing the design boundary.
7. **Acceptance criteria** — verifiable GitHub task-list items.
8. **Verification** — unit, integration, failure-injection, compatibility, and
   optional real-harness checks appropriate to the issue.
9. **Security and privacy** — permission, credential, redaction, workspace, and
   fail-closed requirements relevant to the issue.
10. **Observability and operations** — required events, diagnostics, metrics, or
    recovery evidence.
11. **Documentation and migration** — user/contributor docs and compatibility
    expectations.
12. **Non-goals** — adjacent work explicitly excluded.
13. **Completion evidence** — PR links, test output, docs, and operational proof
    required before closure.

Epic issues summarize their intended platform outcome, enumerate and link their
ordered sub-issues, explain the delivery gate, and define epic-level completion.
They do not duplicate every child specification.

### Dependency mapping

Dependency keys such as `P0.4` resolve through the repository issue map. Rendered
issue bodies use links such as:

```markdown
- [#11 — P0.4 Define AgentRuntime protocol and event vocabulary](https://github.com/Darshan2104/Garuda-openagent/issues/11)
```

The validator fails when:

- a dependency key is unknown;
- a dependency resolves to the issue being rendered;
- a dependency points to a later issue in a way that creates a cycle;
- a mapped issue number does not exist in the target repository;
- a child has the wrong GitHub parent or more than one parent;
- a dependency is expressed as unlinked roadmap text in a managed live body.

Dependency order follows implementation prerequisites, not merely issue number.
The current P0-to-P2 delivery order remains unchanged.

## Components

### Roadmap issue map

A small repository file under `docs/roadmap/` records stable roadmap keys and
GitHub identities. Each entry contains:

- roadmap key;
- issue number;
- parent roadmap key for sub-issues;
- priority;
- dependency keys.

The issue number is the stable remote identity. Titles may be corrected without
breaking dependency resolution.

### Issue renderer and synchronizer

The focused `scripts/sync-roadmap-issues.py` utility performs four operations:

1. parse the catalog and issue map;
2. validate required sections, dependency graph, issue identities, and hierarchy;
3. render normalized GitHub-flavored Markdown;
4. show a diff in dry-run mode or update bodies with an explicit apply option.

Remote access uses the authenticated GitHub CLI rather than accepting a token on
the command line or storing credentials in repository files. The script checks
that the authenticated account can access `Darshan2104/Garuda-openagent` before
applying changes.

An interrupted update is safe to rerun because each body is rendered
deterministically. The script reports completed and failed issue numbers and
returns a non-zero exit status if any update fails.

### MkDocs site

The site uses MkDocs Material, configured by root `mkdocs.yml`, and consumes the
existing `docs/` tree. The docs dependencies are declared in the existing
`project.optional-dependencies` table as the `docs` extra. Root `README.md`
remains the only README and continues to serve as the GitHub landing page.

Primary navigation is:

1. Overview
2. Getting started
3. User guides
4. Architecture and modules
5. CLI/reference
6. Development
7. Evaluation
8. Roadmap
9. Archive

The navigation links existing pages explicitly, which makes missing or renamed
pages fail during strict builds. Archive material remains accessible but is
visually separated from current guidance.

The site configuration includes repository links, edit links, search, code-copy
support, heading anchors, and readable light/dark palettes. No custom theme or
JavaScript application is introduced.

### Validation and deployment workflows

The `.github/workflows/docs.yml` workflow validates pull requests and pushes:

- `mkdocs build --strict`;
- local documentation links;
- the rule that root `README.md` is the only README;
- issue-catalog structure and dependency mapping in read-only mode.

The `.github/workflows/docs-pages.yml` workflow runs only for `main` or manual
dispatch. It builds the same strict site, uploads the generated Pages artifact,
and deploys through GitHub's official Pages actions. Workflow permissions are
limited to repository contents read, Pages write, and OIDC token write. Pull
requests never deploy.

The repository Pages source is configured for GitHub Actions. The first
deployment is considered complete only after the Pages API and public URL both
report success.

## Data flow

### Issue synchronization

```text
issue catalog + issue map
           │
           ▼
 parser and graph validator
           │
           ▼
 deterministic Markdown renderer
           │
     ┌─────┴─────┐
     │           │
 dry-run diff   explicit apply
                 │
                 ▼
          GitHub issue bodies
```

The implementation first performs a complete dry run. Remote updates begin only
after all local validation passes. A post-apply audit reads every managed issue,
checks dependency links and hierarchy, and compares the normalized body with the
local render.

### Documentation publishing

```text
docs/ + MkDocs configuration
             │
             ▼
       strict site build
             │
      ┌──────┴──────┐
      │             │
 pull-request CI   main deployment
                    │
                    ▼
              GitHub Pages
```

## Error handling

- Catalog or mapping errors stop before any remote issue mutation.
- GitHub authentication or permission errors report the active account and
  repository, without printing credentials.
- Partial remote failure produces an exact retry list; deterministic rendering
  makes retries idempotent.
- A hierarchy mismatch is reported but not automatically repaired unless the
  explicit apply operation includes hierarchy synchronization.
- Documentation build warnings are treated as errors.
- A failed Pages deployment leaves the previous successful site available.
- Public URL verification uses bounded retries and reports the workflow URL when
  GitHub is still provisioning Pages.

## Security and privacy

- No personal access token, OAuth token, vendor credential, or harness session is
  written to source files, issue bodies, workflow logs, or generated site output.
- GitHub authentication is delegated to the installed GitHub CLI and Actions
  identity.
- Pages contains repository documentation only; session-managed `.context/`
  files remain gitignored and cannot enter the site artifact.
- Workflows use pinned major versions of official GitHub actions and minimal
  permissions.
- Documentation validation does not fetch arbitrary remote URLs in a privileged
  job.

## Verification plan

### Issue content

- Tests in `tests/test_roadmap_issue_sync.py` cover catalog parsing, rendering,
  dependency links, cycles, missing mappings, and literal escaped-newline
  normalization.
- Fixture tests cover epics, dependency-free children, multi-dependency children,
  and malformed sections.
- Dry-run output is reviewed before applying remote changes.
- Post-apply audit verifies all nine epics, 35 sub-issues, live dependency links,
  parent relationships, labels, priorities, and required sections.
- Representative issues from each priority and area are manually read as a new
  contributor to confirm that the work can be started from the issue alone.

### Documentation site

- Install the documented docs dependency set in a clean environment.
- Run the strict MkDocs build locally.
- Run local-link and single-README validation.
- Exercise the pull-request validation workflow.
- Exercise the `main` Pages deployment workflow.
- Verify navigation, search, code blocks, internal links, repository links, and
  mobile readability at the public URL.

## Rollout sequence

1. Add the issue map, renderer/synchronizer, fixtures, and validation tests.
2. Expand the canonical catalog to the full issue contract.
3. Run a complete dry-run audit and review the generated diffs.
4. Apply issue-body updates and perform a remote post-apply audit.
5. Add MkDocs configuration, dependency declaration, navigation, and local build
   instructions.
6. Add pull-request validation and Pages deployment workflows.
7. Build locally, commit only the intended files, and push to `main`.
8. Configure GitHub Pages for Actions and monitor the deployment to completion.
9. Verify the public site and record its URL in the root README and docs index.

## Completion criteria

This work is complete when:

- all managed dependencies are direct links to the correct GitHub issues;
- every sub-issue satisfies the issue contract and has no malformed Markdown;
- all epic/sub-issue relationships and priorities pass the remote audit;
- future maintainers can detect drift with a documented dry-run command;
- documentation validation passes in CI;
- GitHub Pages deploys automatically from `main`;
- the public site is readable at the approved URL; and
- no unrelated dirty-worktree changes are staged, altered, or committed.
