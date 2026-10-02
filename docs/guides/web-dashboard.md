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

## Conversations

A run's page (`#/runs/<id>`) opens with a **Models used** table from the usage ledger,
grouped by work type × harness × model: a native session's controller, collector,
classifier and summarizer calls each get a row, and the sessions of a flow use their step
roles as work types. A session from before the ledger falls back to its own metrics and says
**from session metrics**. Three things are kept apart: the model that was *selected* is not
the one *reported* (an external harness's internal calls on other models read **not
reported** and are never assigned to the selected model); context snapshots are occupancy,
not billable usage; and ACP turns are not native call counts. Each model call in the trace
carries its model and purpose badge. The panel also lists the runtime lanes, the sessions
this one resumed from or was continued by, the sessions it tagged, and those that tagged it.
`GET /api/sessions/<id>/conversation` serves the same data. Names and tasks are escaped.

## Background sessions

`garuda run --bg` queues a session and returns; the Sessions page shows it
**queued** (with its position), **working**, **waiting** on an approval,
**crashed** (derived when its worker is gone) or **stopped**, with the outcome and
verification kept apart. A session detail page has a Stop button for a background
session (a write-mode dashboard only), a live events panel that reconnects with the
last event id so nothing repeats or is skipped, and, for a flow, its steps and
review. `garuda sessions --json` prints the same rows. The worker's own output is
`worker.log` in the session directory; see the troubleshooting page for worker and
approval diagnostics.

## What you can see

- **Sessions** (`#/sessions`): every session's state, runtime/role/model, outcome and verification (separately), queue position, workspace and branch, approvals waiting, and usage and cost (`unknown` until the ledger exists). A session opens to its pending approvals and, for a flow, its steps, attempts, artifact edges, workspace change per attempt and its review outcome (a review is never shown as verification);

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

| `POST /api/sessions/<id>/cancel` | Stop a background session: leave the queue, or stop its worker (only after its recorded process identity matches); write mode only |
| `GET /api/inbox` | Every pending approval across active sessions, each with the request digest an answer must bind |
| `POST /api/sessions/<id>/approvals/<approval>` | Record an answer (`{"allow": true, "digest": "..."}`); write mode only |

**Approvals.** The Approvals page (`#/inbox`) answers a parked approval — including
one in a background session — by writing a session-bound answer file to that
session's approval channel. It never answers a runtime: the broker validates the
answer (session, request digest, nonce, expiry and the permission ceiling) and
records the single decision, so a successful response means "answer recorded for
the broker", not "the tool will run". The page must send the digest it was shown:
a request that changed since is refused (`stale_request`, 409); a second answer
or one after a terminal decision loses (`already_answered`, 409); an expired
request is refused (`expired`, 410) and will be denied; and a ceiling change
after the answer makes the broker deny it. The usual token, Host and Origin
checks apply, and a `--read-only` dashboard offers no buttons.

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
