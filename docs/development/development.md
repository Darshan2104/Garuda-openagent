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
and hand-maintained test-count claims in release docs. The check is stdlib-only
and never fetches remote URLs:

```bash
python scripts/check_docs.py
pytest tests/test_docs_contract.py -q
```

Only the root `README.md` may exist; historical counts under `docs/archive/`
are exempt. Any other exception must be added explicitly to
`scripts/check_docs.py` with reviewer approval. Do not hand-maintain test
counts in `README.md`, `AGENTS.md`, or `docs/` — cite the CI run instead.

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
