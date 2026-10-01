# GitHub Pages documentation implementation plan

## Scope

Publish the existing `docs/` tree as a searchable MkDocs Material site at
`https://darshan2104.github.io/Garuda-openagent/`. This implements only the
documentation-publishing portion of the approved
[issue enrichment and documentation publishing design](../roadmap/2026-08-31-issue-enrichment-and-docs-publishing-design.md).

## Implementation

1. Add a strict `mkdocs.yml` with explicit navigation for current guides,
   contributor material, evaluation, planning records, and the archive.
2. Declare site-building dependencies separately from Garuda's document-reader
   extra and pin the versions used by CI.
3. Add a pull-request workflow that builds the site strictly and runs the
   dependency-free documentation contract.
4. Add a `main`-only workflow that builds and deploys through GitHub's official
   Pages actions with minimal permissions.
5. Document local preview and strict-build commands, and link the public site
   from the README and documentation index.
6. Run both documentation-contract modes, a clean strict site build, focused
   tests, lint, and the full repository suite before merge.
7. Configure the repository Pages source for GitHub Actions, monitor the first
   deployment, and verify the public URL returns the generated site.

## Boundaries

- Pull requests build but never deploy.
- Only documentation from the repository is published; runtime context and
  credentials are not part of the artifact.
- A failed deployment leaves the previous successful Pages release available.
- Roadmap issue synchronization from the broader design remains separate work.
