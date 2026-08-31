# Web dashboard

`garuda web` serves a local dashboard for inspecting sessions and talking to a live agent. It is deliberately local-only: it binds loopback, uses a capability token, validates `Host` and `Origin`, and emits no CORS headers.

```bash
garuda web --workspace .
```

The dashboard shows live turns, model thinking where available, tool calls, approvals, completion gates, diffs, metrics, and nested subagent traces. It can resume a conversation without discarding its workspace or event history.

## Grounding

Uploads and URL sources are saved into the agent workspace under `grounding/`. The agent reads them through its regular tools instead of receiving their full contents in the prompt. URL retrieval uses the same SSRF-vetted fetcher as `web_fetch`.

## Safety

The workspace is selected from a server-side allowlist and requested permission posture cannot exceed `--max-permission`. A browser approval poll also acts as a heartbeat: an abandoned approval is denied rather than leaving a run blocked.

Run real-browser checks described in [Browser checks](../development/browser-checks.md) after changing the dashboard.
