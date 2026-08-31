# Development

## Local checks

```bash
pip install -e ".[dev]"
pytest -q
ruff check garuda tests
```

Run narrow tests before broad tests. Live integrations are intentionally opt-in: Harbor needs `.[eval]`, tmux needs the system binary, Docker needs a reachable daemon, and macOS Seatbelt checks need `GARUDA_LIVE_SANDBOX=1`.

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
