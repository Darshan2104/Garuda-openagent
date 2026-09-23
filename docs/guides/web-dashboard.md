# Web dashboard

`garuda web` serves a local dashboard for inspecting sessions and talking to a live agent. It is deliberately local-only: it binds loopback, uses a capability token, validates `Host` and `Origin`, and emits no CORS headers.

```bash
garuda web --workspace .
```

The dashboard shows live turns, model thinking where available, tool calls, approvals, completion gates, diffs, metrics, and nested subagent traces. It can resume a conversation without discarding its workspace or event history.

## Grounding

Uploads and URL sources are saved into the agent workspace under `grounding/`. The agent reads them through its regular tools instead of receiving their full contents in the prompt. URL retrieval uses the same SSRF-vetted fetcher as `web_fetch`.

## Runtime controls

- `GET /api/runtimes` lists configured runtimes with health; `GET
  /api/runtimes/<id>` inspects one, including login guidance and quota (shown
  only when the harness reports it).
- `GET /api/runs/<id>/handoff?to=<runtime>` previews a switch without changing
  anything; `POST` with `{"target": ...}` prepares the handoff package (write
  mode only — read-only dashboards answer 503).
- `GET /api/runs/<id>/diff` shows the authoritative workspace delta;
  `GET|POST /api/runs/<id>/recover` classifies or recovers the session.
- The UI renders exactly these records: unknown versions, logins, and quotas
  display as unknown, never invented.
- The Runtimes board (`#/runtimes`) is the visible control surface: the
  harness table with health/auth/version, click-to-inspect detail
  (capabilities, login guidance, setup), handoff preview plus confirmed
  prepare with state shown, the diff timeline split into session work vs
  pre-existing dirt (sessions without a recorded baseline say so instead of
  inventing one), and recovery classify/run with the report rendered.
  Prepare and recover disable themselves on read-only dashboards.

## Safety

The workspace is selected from a server-side allowlist and requested permission posture cannot exceed `--max-permission`. A browser approval poll also acts as a heartbeat: an abandoned approval is denied rather than leaving a run blocked.

Run real-browser checks described in [Browser checks](../development/browser-checks.md) after changing the dashboard.
