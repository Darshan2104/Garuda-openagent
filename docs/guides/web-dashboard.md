# Web dashboard

`garuda web` serves a local dashboard for reading sessions and talking to the
native Garuda agent.

!!! abstract "At a glance"
    - Binds to loopback only, generates a capability token, validates `Host`
      and `Origin`, and emits no CORS headers.
    - `--read-only` shows history only. Without it, conversations are limited to
      the directories you allow and the permission ceiling you set.
    - The dashboard's controls don't turn a local or tmux workspace into
      confinement. Use Docker for untrusted code.

## Start the dashboard

=== "History only"

    ```bash
    garuda web --read-only
    ```

    Inspect saved runs. You can't start conversations, prepare handoffs, or
    run recovery in this mode.

=== "Conversations"

    ```bash
    garuda web --allow-workspace . --max-permission smart
    ```

    Repeat `--allow-workspace` for more directories. A browser request picks a
    directory by its allowlist index, never by a typed path, and may ask for
    the `--max-permission` posture or a stricter one.

| Flag | Default | Use |
|---|---|---|
| `--read-only` | off | Serve run history only |
| `--allow-workspace DIR` | current directory | A directory conversations may use; repeatable |
| `--max-permission` | `smart` | Loosest posture a browser request may ask for |
| `--web-model` | your bindings | Default model for conversations |
| `--web-agent` | `build` profile | Default profile for conversations |
| `--web-workspace-kind` | `local` | Workspace kind for conversations |
| `--sessions-dir DIR` | configured root | Read sessions from another store |
| `--port` | `8787` | Port to listen on |
| `--no-browser` | off | Don't open a browser automatically |

## What you can see

- live turns, model thinking where available, and tool calls;
- approvals, completion checks, diffs, and metrics;
- nested subagent traces.

A chat can continue without discarding its workspace or event history.

## Sessions, the queue and approvals

`garuda sessions --json` and the dashboard read **one model**
(`garuda/core/read_model.py`), so the CLI and the web API agree on a session's
state, outcome, verification and provenance:

| Route | Purpose |
|---|---|
| `GET /api/sessions` | Every session's row: the four facts (`process`, `work`, `outcome`, `verification`), the derived `crashed` label, queue position, worker, approvals waiting, agent digest and resume provenance |
| `GET /api/sessions/<id>` | One session, with its pending approvals and, for a flow, its steps, attempts and review outcome |
| `GET /api/sessions/<id>/stream` | Live events as server-sent events |

Usage and cost read `unknown` until the usage ledger lands; a flow's review
outcome is shown on its own and never as verification; a queue that cannot be
read shows as `unknown`, not as empty. The stream's `id` is a byte offset into
the session's log, so reconnecting with `Last-Event-ID` continues with the next
event: none repeats and none is skipped. It sends a heartbeat every 15 seconds,
ends with an `end` event once the session is finished and drained, and closes
after a minute for the client to reconnect. It needs the same token and Host
checks as every route; a browser `EventSource` cannot send the token header, so
read it with `fetch`. Polling `/api/sessions/<id>/tail` still works.

## Grounding files and URLs

Uploads and URL sources are saved under `grounding/` in the selected workspace.
The agent reads them with its normal tools instead of placing the whole source
in the first prompt. URL retrieval uses the same SSRF-vetted fetcher as
`web_fetch`.

Uploaded or fetched content is still untrusted input. It can't grant itself
tool authority, but its contents can reach the selected model when the agent
reads it.

## Runtimes board

The Runtimes board at `#/runtimes` works with the trusted runtime catalog and
saved sessions. Dashboard chat stays native; the board adds ACP operations:

- list runtime health, authentication state, version, capabilities, setup
  guidance, and vendor-reported quota where available;
- inspect one runtime without inventing missing login or quota data;
- preview and explicitly prepare a native-to-ACP handoff;
- separate recorded session changes from pre-existing workspace changes; and
- classify or run session recovery.

| Route | Purpose |
|---|---|
| `GET /api/runtimes`, `GET /api/runtimes/<id>` | Runtime list and detail |
| `GET`, `POST /api/runs/<id>/handoff` | Preview and prepare a handoff |
| `GET /api/runs/<id>/diff` | Session workspace delta |
| `GET`, `POST /api/runs/<id>/recover` | Classify and run recovery |

Prepare and recovery controls disable themselves in history-only mode. See
[External harnesses](external-harnesses.md) for ownership, verification, and
handoff failure semantics.

## Safety model

| Control | Limits |
|---|---|
| Capability token | Who can call dashboard routes |
| Loopback bind | Network exposure |
| Workspace allowlist | Which directories can be selected |
| `--max-permission` | What a browser request can ask for, including any subagents it starts |

A browser's approval poll also acts as a heartbeat. If the browser abandons an
approval, Garuda denies it instead of leaving the run blocked. See
[Safety and workspaces](safety-and-workspaces.md) for workspace and permission
guidance.

Run the opt-in real-browser checks in
[Browser checks](../development/browser-checks.md) after changing dashboard
behavior or presentation.
