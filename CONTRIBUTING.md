# Contributing to Garuda

Start with [AGENTS.md](AGENTS.md), then the
[Development guide](docs/development/development.md) and
[Architecture](docs/ARCHITECTURE.md). Open work lives in the
[backlog](docs/BACKLOG.md).

From your checkout, set up a contributor environment and run the checks:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pip install ruff
python -m pytest tests/test_<area>.py -q   # replace <area> with the area changed
python -m pytest -q
ruff check garuda tests
python scripts/check_docs.py
python scripts/check_docs.py --commands
```

Keep diffs focused, preserve existing work, and add regression coverage for
behavior changes. Follow the docs contract in AGENTS.md: a new public command
needs a demonstration in `docs/use-cases/` or the
[cheat sheet](docs/reference/cheat-sheet.md), plus a row in the
[feature index](docs/reference/features.md).

Report security issues privately as described in [SECURITY.md](SECURITY.md).
