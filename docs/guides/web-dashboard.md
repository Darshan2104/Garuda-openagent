# Web dashboard

`garuda web` serves a local dashboard for reading sessions and talking to the
native Garuda agent. It binds only to loopback, generates a capability token,
validates `Host` and `Origin`, and emits no CORS headers.

## Start with history-only mode

```bash
garuda web --read-only
```

History-only mode can inspect saved runs but cannot start conversations,
prepare runtime handoffs, or execute recovery. Use `--sessions-dir` to read a
non-default session store and `--no-browser` when another process will open the
URL.

## Enable conversations deliberately

Without `--read-only`, the dashboard can start native conversations. Define the
server-side workspace allowlist and permission ceiling at launch:

```bash
garuda web --allow-workspace . --max-permission smart
```

Repeat `--allow-workspace` for additional directories. Browser requests select
an allowlist index, never submit an arbitrary filesystem path, and may request
the configured maximum permission or a stricter posture. Use `--web-model`,
`--web-agent`, and `--web-workspace-kind` to change conversation defaults.

The dashboard shows live turns, model thinking where available, tool calls,
approvals, completion gates, diffs, metrics, and nested subagent traces. A chat
can continue without discarding its workspace or event history.

## Grounding files and URLs

Uploads and URL sources are saved beneath `grounding/` in the selected
workspace. The agent reads those files through its normal tools instead of
placing the entire source in the initial prompt. URL retrieval uses the same
SSRF-vetted fetcher as `web_fetch`.

Uploaded or fetched content is still untrusted input. It does not grant itself
tool authority, but its contents can reach the selected model when the agent
reads it.

## Inspect external runtimes

The Runtimes board at `#/runtimes` operates on the trusted runtime catalog and
persisted sessions. Dashboard chat remains native; the board provides ACP
operations:

- list runtime health, authentication state, version, capabilities, setup
  guidance, and vendor-reported quota where available;
- inspect one runtime without inventing missing login or quota data;
- preview and explicitly prepare a native-to-ACP handoff;
- separate recorded session changes from pre-existing workspace dirt; and
- classify or execute session recovery.

The underlying routes are:

- `GET /api/runtimes` and `GET /api/runtimes/<id>`;
- `GET|POST /api/runs/<id>/handoff`;
- `GET /api/runs/<id>/diff`; and
- `GET|POST /api/runs/<id>/recover`.

Prepare and recovery controls disable themselves in history-only mode. See
[External harnesses](external-harnesses.md) for ownership, verification, and
handoff failure semantics.

## Safety model

The token protects dashboard routes, the loopback bind limits network exposure,
the workspace allowlist limits selectable roots, and `--max-permission` limits
what a browser request can ask for. These controls do not turn a local or tmux
workspace into confinement. Choose Docker when untrusted code needs an isolation
boundary.

A browser approval poll also acts as a heartbeat. If the browser abandons an
approval, Garuda denies it instead of leaving the run blocked indefinitely. See
[Safety and workspaces](safety-and-workspaces.md) for workspace and permission
guidance.

Run the opt-in real-browser checks from
[Browser checks](../development/browser-checks.md) after changing dashboard
behavior or presentation.
