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
| `seed_sessions.py`, `check_sessions.py` | Sessions view over fixtures written by the production session, queue, flow and approval writers: states (queued with position, derived crashed, completed, working, waiting), unknown usage/cost, a self-check not shown as verification, and a flow's steps, attempts, edges, deltas and separate review badge. |
| `check_background.py` | Background control over a write-mode dashboard and fixtures from the production writers: queued/working/waiting/crashed/stopped, outcome vs verification, CLI JSON equal to the web API, the approval inbox (a deliberately stale click loses, an expired request offers no buttons), Stop on a queued session and on a real worker process, and a stream reconnect with neither repeats nor gaps. |
| `check_observability.py` | Observability over fixtures from the production writers (`seed_sessions.py --observability`): a native conversation's per-work-type rows, ACP sessions with cumulative usage (a replayed report adds nothing) and per-turn usage (a duplicate counts once) whose models are `not reported`, a fallback session, a multi-round flow, tags across harnesses and projects with the sharing receipt, usage over 24h/7d/30d and the JSON export, a conversation's agent line (separate native execution identities, identical prompt hashes retained across executions, and explicitly unattributed historical measurements; digests only), and Setup's Agents card (project and packaged agents with digests, declared settings and their sources, Unicode section metrics labeled as bytes/chars/estimated tokens, an unresolvable definition with its problem, and no instruction text), all with no console errors. |
| `check_consults.py` | Consult visibility over fixtures from the production consult service, ledger observer and session store (`seed_sessions.py --consults`): nested lanes for an answered, withheld (changed evidence), failed and timed-out consult, a quarantined consult with no receipt and unknown evidence, the consulted child's session, parent rollups counted once with the original work type and a consult mark, Setup's grants, ceilings and per-adapter transport status, and usage by origin adding up to the total, all with no console errors. |
| `check_runtimes.py` | Runtimes board: list/select, handoff preview/prepare, diff session-vs-dirt split, recovery classify/run, and read-only control gating. |
