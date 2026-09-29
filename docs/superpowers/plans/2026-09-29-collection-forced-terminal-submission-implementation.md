# Collection forced terminal submission: implementation plan

1. Update `CollectionCoordinator._config` so collection children opt into the
   existing terminal-only forced submission flow after ordinary turns exhaust.
2. Mark `CollectionCompletionGate` as supporting that existing flow. The gate
   remains the sole validator for every accepted structured report.
3. Extend `tests/test_collection_runner.py` with one scripted owner-boundary
   regression: an ordinary evidence read consumes the child budget, then a
   valid `submit_collection` call on the forced terminal turn succeeds. Assert
   the terminal schema contains only `submit_collection` and the resulting
   report is validated.
4. Run the focused collection/terminal tests, Ruff, then the full suite. The
   post-fix live pilot remains capped by the existing disposable configuration.
