# Development

## Local checks

```bash
pip install -e ".[dev]"
pytest -q
ruff check garuda tests
python scripts/check_docs.py
```

Run narrow tests before broad tests. Live integrations are intentionally opt-in: Harbor needs `.[eval]`, tmux needs the system binary, Docker needs a reachable daemon, and macOS Seatbelt checks need `GARUDA_LIVE_SANDBOX=1`.

## Documentation contract (P0.3)

CI fails on broken local Markdown links, unexpected nested `README.md` files,
hand-maintained test-count claims, and duplicate table rows in maintained user
docs. These checks are stdlib-only and never fetch remote URLs:

```bash
python scripts/check_docs.py
pytest tests/test_docs_contract.py -q
```

After installing Garuda, validate fenced and inline CLI examples against the
real argparse command tree:

```bash
python scripts/check_docs.py --commands
pytest tests/test_docs_commands.py -q
```

Command validation covers `README.md` and user-facing documentation under
`docs/`, including any new documentation directory. It excludes the
contributor architecture/module/backlog/changes pages plus archive, roadmap,
design, and plan records because those may intentionally describe historical
or proposed commands. A deliberately non-executing example can use
`docs-command-ignore` on its fence or line, but exceptions should remain rare
and reviewable.

Only the root `README.md` may exist; historical counts under `docs/archive/`
are exempt. Any other exception must be added explicitly to
`scripts/check_docs.py` with reviewer approval. Do not hand-maintain test
counts in `README.md`, `AGENTS.md`, or `docs/` — cite the CI run instead.

## Documentation site

Install the dedicated site dependencies and run a local preview:

```bash
pip install -e ".[site]" -c constraints.txt
mkdocs serve
```

Before opening a documentation pull request, build the same strict site used by
CI and GitHub Pages:

```bash
mkdocs build --strict
python scripts/check_diagram_links.py site
```

The strict build fails on a page missing from the `nav` in `mkdocs.yml`, a
broken link, or a link to a missing anchor. `check_diagram_links.py` does the
same for Mermaid `click` targets, which MkDocs cannot see. Interactive command
builders (`data-garuda-builder` blocks) have each choice validated by
`check_docs.py --commands`.

Pull requests validate the site but never deploy it. A successful merge to
`main` builds and publishes the `site/` artifact through GitHub's official Pages
actions.

## Reproducible dependency checks

```bash
pip install -e ".[dev,eval]" -c constraints.txt
pytest -q
```

The project also needs an unconstrained dependency check before releases because users install from the declared lower bounds. The MCP dependency is deliberately capped below 2 until an HTTP transport migration is implemented and tested.

## Change discipline

- Add regression coverage at the lowest layer that would have caught the bug.
- Keep security and workspace behavior fail-closed.
- Update relevant docs and durable context when a public boundary or safety claim changes.
- Use `AGENTS.md` as the contributor contract.
