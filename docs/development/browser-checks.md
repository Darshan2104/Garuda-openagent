# Browser checks

The dashboard frontend has no build step or importable unit-test surface, so it is validated by loading it in a real browser. These checks catch runtime JavaScript, CSP, SVG, polling, live-tail, chat, grounding, approval, and nested-trace faults that Python unit tests cannot observe.

```bash
pip install playwright
playwright install chromium        # Linux runners: playwright install --with-deps chromium
python tests/browser/run_checks.py
python tests/browser/run_checks.py --keep
python tests/browser/run_checks.py --channel chrome            # an installed Google Chrome instead
python tests/browser/run_checks.py --workdir /tmp/garuda-bc    # fixtures, logs and screenshots here
# Linux only: owned native/ACP starts in Google Chrome
python tests/browser/run_checks.py --owned-starts-only --keep
```

The runner seeds session fixtures, starts dashboards on free ports, and tears them down. It fails on browser console errors and verifies rendered DOM rather than merely checking that pages do not crash.

- **Browser.** Checks launch Playwright's bundled Chromium (headless) unless `--channel` or `GARUDA_CHECK_CHANNEL` names another channel. The owned-start checks always use Google Chrome.
- **Imports.** The runner puts its own checkout first on `PYTHONPATH` for every seeder, dashboard and check, so a worktree is tested against its own `garuda`, not whatever an editable install points at.
- **Failures.** A section whose fixture or dashboard cannot start is reported as failed and the remaining sections still run. The summary lists every failed section; on failure the working directory (dashboard logs `web*.log`, `screenshots/`) is kept and its path printed.
- **Timeouts.** A dashboard has `GARUDA_CHECK_START_TIMEOUT` seconds (default 120) to listen, which covers a cold first import; each check script has `GARUDA_CHECK_TIMEOUT` seconds (default 600).
- **Waiting.** Checks wait for the condition they assert (`tests/browser/browser_support.py`), not for a fixed time. A remaining fixed wait is an observation window, such as counting requests while an idle page sits still.
- On Linux the full run also includes the owned starts; `--skip-owned-starts` leaves them out.

## CI

The `browser checks` job in `.github/workflows/ci.yml` runs the suite on `ubuntu-latest` for every pull request and push to main: the pinned package with `.[dev]`, Playwright's Chromium with its system dependencies, and `run_checks.py --skip-owned-starts`. On failure it uploads the screenshots and dashboard logs as the `browser-checks` artifact. The owned starts need Google Chrome and the Linux descendant supervisor, and run in `.github/workflows/browser-owned-starts.yml`, which retains its own screenshots and logs.

## Checks

| File | Role |
|---|---|
| `seed_fixtures.py` | Sessions for approvals, diffs, gates, cancellation, legacy logs, and nested subagents. |
| `fake_run.py` | Writes a live `EventStore` run for tailing checks. |
| `serve_chat.py` | Uses a `ScriptModel` and stubbed URL fetcher; no provider call or API key. |
| `browser_support.py` | Shared by the checks: which browser to launch, and waiting for a condition instead of a fixed time. |
| `check_inspector.py` | Trace and subagent rendering. |
| `check_live.py` | Tail refresh, expansion state, and polling lifecycle. |
| `check_chat.py` | Browser upload, URL grounding, approvals, and collapsed labelled child previews: accepted submit advances the turn, literal HTML, expansion before queued toggle events and across polls, newest text visible, parent separation and removal at completion. |
| `seed_sessions.py`, `check_sessions.py` | Sessions view over fixtures written by the production session, queue, flow and approval writers: states (queued with position, derived crashed, completed, working, waiting), unknown usage/cost, a self-check not shown as verification, and a flow's steps, attempts, edges, deltas and separate review badge. |
| `check_unreadable.py` | One damaged session (`meta.json` is `[]`) beside a readable run: the Overview, Runs, Sessions and Approvals pages still render, list the readable run, and each says "1 session could not be read" with the id in its tooltip. |
| `check_overview.py` | The Overview landing page over fixtures from the production writers (`seed_sessions.py --overview`: more than 200 sessions newer than the crashed, waiting and running ones, a flow with a journaled step, flows paused on a deadline and a quota wait, a verified result, a role on an unknown harness) and an empty write-mode store: the token URL lands on it, each section's rows, links and ages, verification apart from outcome, sessions rendering while Setup is held, a failing Providers read shown inline with a working Retry while the rest stays usable, in-place polling, no poller after navigating away, tab shortcuts, empty states, and no horizontal scroll at 390 px in light and dark. |
| `check_background.py` | Background control over a write-mode dashboard and fixtures from the production writers: queued/working/waiting/crashed/stopped, outcome vs verification, CLI JSON equal to the web API, the approval inbox (a deliberately stale click loses, an expired request offers no buttons), Stop on a queued session and on a real worker process, and a stream reconnect with neither repeats nor gaps. |
| `check_observability.py` | Observability over fixtures from the production writers (`seed_sessions.py --observability`): a native conversation's per-work-type rows, ACP sessions with cumulative usage (a replayed report adds nothing) and per-turn usage (a duplicate counts once) whose models are `not reported`, a fallback session, a multi-round flow, tags across harnesses and projects with the sharing receipt, usage over 24h/7d/30d and the JSON export, a conversation's agent line (separate native execution identities, identical prompt hashes retained across executions, and explicitly unattributed historical measurements; ACP request attempts with distinct sending ids, repeated hashes, recorded role identity and unknown internal system prompts, matching runtime-tenure links and explicit unknown provenance for empty/historical tenures; digests only), and Setup's Agents card (project and packaged agents with digests, declared settings and their sources, Unicode section metrics labeled as bytes/chars/estimated tokens, an unresolvable definition with its problem, and no instruction text), all with no console errors. |
| `check_consults.py` | Consult visibility over fixtures from the production consult service, ledger observer and session store (`seed_sessions.py --consults`): nested lanes for an answered, withheld (changed evidence), failed and timed-out consult, a quarantined consult with no receipt and unknown evidence, the consulted child's session, parent rollups counted once with the original work type and a consult mark, Setup's grants, ceilings and per-adapter transport status, and usage by origin adding up to the total, all with no console errors. |
| `check_runtimes.py` | Runtimes board: list/select, handoff preview/prepare, diff session-vs-dirt split, recovery classify/run, and read-only control gating. |
| `seed_starters.py`, `check_scenarios.py` | Real flow/ACP/role/acceptance records (the readonly ACP scout runs inside the same simulated Docker boundary as pytest's `fake_acp_confinement`: Garuda builds the confined launch and the fixture checks its read-only mount, image and command), a copied equivalent through the public CLI, and a ScriptModel-backed native trace: five forms, installed examples, allowed workspace selection, exact clipboard command, stale preview invalidation, required/waived/missing readiness, explicit Build and check, full validated plan handoff, missing-artifact unknowns, historical verification, ordinary-run provenance disclosure, transport denial, read-only Start refusal and unchanged workspace/configuration/session bytes and harness launch count. Positive owned flow and role-run lifecycle is Linux-only and additionally covered by `tests/test_web_flow_starts.py` and `tests/test_web_run_starts.py`; run that test on Linux or in the documented Docker environment. |
| `serve_flow_starts.py`, `check_run_starts.py` | Linux-only Google Chrome native run with real approval, shared native ask, ACP worktree run and detached background run: actual owned children, terminal progress, session/inbox links, duplicate projection and unchanged source. The `browser-owned-starts.yml` workflow retains screenshots and logs. |
| `serve_flow_starts.py`, `check_flow_starts.py` | Linux-only live Chrome flow Start through the real write-mode dashboard, local protocol model and owned native child: approval, durable progress, exact duplicate projection, session link, stale-source feedback and token/origin refusal. |

The observability Setup fixture also checks direct/inherited unsupported agent
field names, provenance and safe diagnostic codes, with private hook/instruction
values excluded from the page.
