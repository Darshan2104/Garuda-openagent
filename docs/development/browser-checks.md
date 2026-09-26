# Browser checks

The dashboard frontend has no build step or importable unit-test surface, so it is validated by loading it in real Chrome. These checks catch runtime JavaScript, CSP, SVG, polling, live-tail, chat, grounding, approval, and nested-trace faults that Python unit tests cannot observe.

```bash
pip install playwright
playwright install chrome
python tests/browser/run_checks.py
python tests/browser/run_checks.py --keep
```

The runner seeds session fixtures, starts dashboards on free ports, and tears them down. It fails on browser console errors and verifies rendered DOM rather than merely checking that pages do not crash.

| File | Role |
|---|---|
| `seed_fixtures.py` | Sessions for approvals, diffs, gates, cancellation, legacy logs, and nested subagents. |
| `fake_run.py` | Writes a live `EventStore` run for tailing checks. |
| `serve_chat.py` | Uses a `ScriptModel` and stubbed URL fetcher; no provider call or API key. |
| `check_inspector.py` | Trace and subagent rendering. |
| `check_live.py` | Tail refresh, expansion state, and polling lifecycle. |
| `check_chat.py` | Browser upload, URL grounding, and approvals. |
| `check_runtimes.py` | Runtimes board: list/select, handoff preview/prepare, diff session-vs-dirt split, recovery classify/run, and read-only control gating. |
