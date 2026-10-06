# Web dashboard

`garuda web` serves a local dashboard for watching sessions, answering
approvals, checking usage and setup, and talking to the native Garuda agent.

!!! abstract "At a glance"
    - Binds to loopback only, generates a capability token, validates `Host`
      and `Origin`, and emits no CORS headers.
    - `--read-only` shows everything but changes nothing: no conversations,
      Stop, Allow or Deny buttons, handoffs or recovery.
    - The dashboard's controls don't turn a local or tmux workspace into
      confinement. Use Docker for untrusted code.

## Start the dashboard

=== "Watch only"

    ```bash
    garuda web --read-only
    ```

=== "Watch, control and chat"

    ```bash
    garuda web --allow-workspace . --max-permission smart
    ```

    Repeat `--allow-workspace` for more directories. A browser request picks a
    directory by its allowlist index, never by a typed path, and may ask for
    the `--max-permission` posture or a stricter one.

The command prints a URL with a one-time token (`http://127.0.0.1:8787/#t=…`);
open that exact URL.

| Flag | Default | Use |
|---|---|---|
| `--read-only` | off | Serve without any control or conversation |
| `--allow-workspace DIR` | current directory | A directory conversations may use; repeatable |
| `--max-permission` | `smart` | Loosest posture a browser request may ask for, including any subagents it starts |
| `--allow-agent NAME` | any you define | An agent a request may select by name; repeatable |
| `--web-model` | your bindings | Default model for conversations |
| `--web-agent` | `build` | Default agent for conversations |
| `--web-workspace-kind` | `local` | Workspace kind for conversations |
| `--agents-dir DIR` | project directories | Read agents from another directory |
| `--sessions-dir DIR` | configured root | Read sessions from another store |
| `--port` | `8787` | Port to listen on |
| `--no-browser` | off | Don't open a browser automatically |

## Pages

| Page | Route | What it's for |
|---|---|---|
| Runs | `#/runs` | Every run: turns, tool calls, diffs, metrics, models used, consults |
| Sessions | `#/sessions` | Live and past sessions with their state, queue and flow; Stop |
| Approvals | `#/inbox` | Every waiting approval; Allow or Deny |
| Usage | `#/usage` | Totals and breakdowns from the usage ledger; CSV or JSON export |
| Setup | `#/setup` | Roles, flows, agents, where each value came from, diagnostics |
| Providers | `#/providers` | Each harness and API provider: install, login, limits |
| Runtimes | `#/runtimes` | Runtime health, handoffs and recovery |
| Chat | `#/chat` | A conversation with the native agent (not in `--read-only`) |

## Runs

A run's page (`#/runs/<id>`) shows live turns, model thinking where available,
tool calls, approvals, completion checks, diffs, metrics, and nested subagent
traces. A chat can continue without discarding its workspace or event
history. On top of that:

- **Models used** lists every model call grouped by work type, harness and
  model (a native session's controller, collector, classifier and summarizer
  calls each get a row; a flow's sessions use their step roles). The model
  that was *selected* is kept apart from the one *reported*: an external
  harness's internal calls on other models read **not reported**. Context
  snapshots are occupancy, not billable usage, and external-harness turns are
  not native call counts. A session from before the usage ledger says **from
  session metrics**.
- **Links and lanes** show the sessions this one resumed from or was continued
  by, the sessions it tagged with `@name` and those that tagged it, the
  runtime lanes, any fallback, and a receipt for context shared across
  projects (never the shared content).
- **Agent** shows the agent and definition digest each native execution
  started from, and the digest and size of each system prompt it actually
  sent. For an external harness it shows the digest of each request Garuda
  attempted to send; the harness's own internal system prompt stays unknown.
  Older measurements without a recorded execution are labelled
  **Unattributed historical prompts**, and gaps read **runtime tenure
  unknown** rather than being filled in.
- **Consults** lists each question the session asked another role as a lane:
  who asked whom, the identity that ran, its outcome (`answered`, `withheld`,
  `failed`, `timeout`, `quarantined`, `interrupted`), duration, denied
  operations and observed changes, with a link to the consulted session. A
  consult without a receipt reads **no receipt** and its changes read
  **unknown**. The question and answer are never stored or shown.

## Sessions

The Sessions page shows every session's state (**queued** with its position,
**working**, **waiting** on an approval, **crashed** when its worker is gone,
or how it ended), runtime, role and model, outcome and verification as
separate columns, workspace and branch, and approvals waiting. A session's
page adds:

- its pending approvals;
- a live events panel that reconnects without repeating or skipping events;
- for a flow: steps, attempts, artifacts passed between them, the workspace
  change per attempt, and the review outcome (never shown as verification);
- **Stop**, for a background session: it leaves the queue, or its worker is
  stopped once its recorded process identity matches.

`garuda sessions --json` prints the same rows: the CLI and the dashboard read
one model. A queue that can't be read shows as `unknown`, not as empty. A
session row's own usage and cost still read `unknown`; the Usage page has the
totals.

## Approvals

The Approvals page lists every approval waiting in an interactive session (a
`garuda chat`, or a dashboard conversation) and answers it by writing a
session-bound answer for the session's approval broker. A successful response
means "answer recorded", not "the tool will run": the broker checks the
session, request digest, nonce, expiry and permission ceiling, and records one
decision.

- The page sends the digest it showed you. A request that changed since is
  refused (`stale_request`, 409); a second answer, or one after the decision,
  loses (`already_answered`, 409); an expired request is refused (`expired`,
  410) and denied.
- If the browser abandons an approval it was polling, Garuda denies it instead
  of leaving the run blocked.
- Headless runs, including `--bg` runs, deny approvals at once, so they never
  appear here.

## Usage

`#/usage` summarizes the [usage ledger](configuration.md#usage-ledger) over the
last 24 hours, 7 days or 30 days (rolling UTC windows): totals, the share of
work by type, a harness × model table, tokens per day, and tables by role, by
origin (run, subagent, flow, consult) and by opaque project ID, with the median
tokens and duration per native call.

- **Units stay apart.** Native calls, external-harness turns and snapshots are
  different measures. Percentages compare native calls only, and the page
  lists what it left out.
- **Unknown cost is never zero.** A range of unpriced calls reads *unknown*; a
  mixed one shows the known sum beside the number of unpriced records.
- **Export CSV / JSON** downloads fixed schema fields: identities and counts,
  no text, paths or account names.
- These statistics are a report. They never choose a harness or model.
- After project-key recovery, usage groups old IDs through verified aliases;
  an unfinished recovery refuses grouped reporting until
  `garuda doctor --recover-project-ids` completes. Raw exports keep each
  record's original ID.

## Providers

`#/providers` shows a card per harness and per API provider: whether it is
installed, its last recorded **login** check (`not checked` until a refresh),
and its **limits**.

- A limit always carries its source and when it was observed. It reads
  **unknown** unless Garuda proved that source for the exact version (Claude
  Code has no documented non-interactive source), **known** for a minute after
  a reading, then **stale** with its age. A different version, or a source
  without exactly one account, is flagged.
- A limit **event** reads `active` while its reset is ahead, `expired` once
  past, and `historical` when no reset time was reported. Only an active,
  fresh, same-account reading can make a role's fallback
  [skip that harness](../use-cases/advanced.md#skip-a-harness-whose-subscription-limit-is-used-up).
- **Observed use** over 5 hours, 7 days and 30 days is labelled *through Garuda
  only*: use outside Garuda draws on the same quota.
- **Refresh** (not in `--read-only`) re-runs the documented status reads and
  never sends a prompt.

## Setup

`#/setup` is a read-only view of what is configured and what is wrong:

- **Diagnostics** for configuration, each harness your roles use, leases and
  unpublished worktrees, each with a code and a fix you can copy (the same
  as `garuda doctor`, but no vendor command is started; a login shows its
  last recorded check).
- The **role table**: each role's harness, exact model, effort, permissions
  and fallback chain.
- The **flow list**, marking packaged flows and naming any role a flow needs
  that you haven't defined.
- **Where each value came from**: package, your file, the project's trusted
  file, or the command line, plus anything a project file asked for that is
  withheld until you trust it.
- **Consults**: who may consult whom, the limits, and which adapter versions
  proved the consult transport.
- **Agents**: each agent's source and `extends`, shadowing, the settings it
  declares and where each came from, its definition digest, and its static
  prompt digest with each section's size and estimated tokens. It never shows
  instruction or prompt text (use `garuda agent show NAME` for that). An agent
  that can't resolve is listed with a stable diagnostic code and no source
  text; run `garuda agent check NAME` locally for details.

To change anything, edit your `garuda.yaml` or run `garuda init`.

## Runtimes

The Runtimes board works with the trusted runtime catalog and saved sessions.
Dashboard chat stays native; the board adds:

- runtime health, login state, version, capabilities, setup guidance, and
  vendor-reported quota where available, without inventing missing data;
- a preview and an explicit **prepare** step for a native-to-ACP handoff;
- the session's own changes, separated from changes that were already in the
  workspace; and
- recovery: classify a session, or run recovery.

Prepare and recovery controls are disabled in `--read-only`. See
[External harnesses](external-harnesses.md) for ownership, verification and
handoff failure semantics.

## Grounding files and URLs

In a conversation, uploads and URL sources are saved under `grounding/` in the
selected workspace. The agent reads them with its normal tools instead of
placing the whole source in the first prompt. URL retrieval uses the same
SSRF-vetted fetcher as `web_fetch`.

Uploaded or fetched content is still untrusted input. It can't grant itself
tool authority, but its contents can reach the selected model when the agent
reads it.

## HTTP API

Every route needs the dashboard token in the `X-Garuda-Token` header and passes
the same `Host` and `Origin` checks. Write routes are refused in `--read-only`.

| Route | Purpose |
|---|---|
| `GET /api/runs`, `GET /api/runs/<id>` | Runs and one run's trace |
| `GET /api/runs/<id>/tail` | Poll a run for new events |
| `GET /api/sessions` | Every session's row: the four facts, the derived `crashed` label, queue position, worker, approvals waiting, agent digest, resume provenance |
| `GET /api/sessions/<id>` | One session, with pending approvals and, for a flow, its steps, attempts and review outcome |
| `GET /api/sessions/<id>/stream` | Live events as server-sent events |
| `GET /api/sessions/<id>/conversation` | The Models used, links and lanes data |
| `POST /api/sessions/<id>/cancel` | Stop a background session (write) |
| `GET /api/inbox` | Every pending approval, with the digest an answer must bind |
| `POST /api/sessions/<id>/approvals/<approval>` | Answer one: `{"allow": true, "digest": "…"}` (write) |
| `GET /api/usage?range=24h\|7d\|30d` | Usage statistics |
| `GET /api/usage/export?range=…&format=csv\|json` | Usage export |
| `GET /api/providers`, `POST /api/providers/refresh` | Provider cards; refresh (write) |
| `GET /api/setup` | The Setup view |
| `GET /api/memory/proposals` | Pending memory note proposals (read-only) |
| `GET /api/runtimes`, `GET /api/runtimes/<id>` | Runtime list and detail |
| `GET`, `POST /api/runs/<id>/handoff` | Preview and prepare a handoff |
| `GET /api/runs/<id>/diff` | Session workspace delta |
| `GET`, `POST /api/runs/<id>/recover` | Classify and run recovery |

**The live stream.** Each event's `id` is a byte offset into the session's
log, so reconnecting with `Last-Event-ID` continues with the next event: none
repeats and none is skipped. The stream sends a heartbeat every 15 seconds,
ends with an `end` event once the session is finished and drained, and closes
after a minute for the client to reconnect. A browser `EventSource` can't send
the token header, so read it with `fetch`.

## Safety model

| Control | Limits |
|---|---|
| Capability token | Who can call dashboard routes |
| Loopback bind | Network exposure |
| Workspace allowlist | Which directories a conversation can use |
| `--max-permission` | What a browser request can ask for, including any subagents it starts |
| `--allow-agent` | Which agents a request can name; a request never sends a definition |

See [Safety and workspaces](safety-and-workspaces.md) for workspace and
permission guidance. Run the opt-in real-browser checks in
[Browser checks](../development/browser-checks.md) after changing dashboard
behavior or presentation.
