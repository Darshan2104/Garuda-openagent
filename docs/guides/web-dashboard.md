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

(The conversation links also show a cross-project **sharing receipt**, never the shared content, and a fallback's story.) A run's page (`#/runs/<id>`) opens with a **Models used** table from the usage ledger,
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

**Agent.** A run's page shows the current session definition and separate native
execution rows. Each native row carries the compiled agent name and definition digest
captured when that execution started, plus the system-message digests and character
counts actually sent by it. Identical prompts in different executions remain in
both rows. Runtime blocks can change the actual prompt digest from the static one
in Setup. Historical measurements without a valid execution binding are labelled
**Unattributed historical prompts**; the current session definition does not supply
their missing identity. The API returns at most 20 execution rows and five distinct
prompt digests per row, with total counts. ACP execution rows show the sending runtime, recorded role definition when present,
and the digest/character count of each **ACP request (attempted)**. These measure
the complete host request text, including role instructions and attached context;
they do not prove delivery. The external agent's **internal system prompt remains
unknown**. Repeated external session ids do not collapse separate executions.
Execution ids remain separate from runtime handoff segments. Each row displays its
recorded runtime-tenure number when the full reference matches that persisted
tenure; missing or inconsistent references display **runtime tenure unknown**.
The tenure list reports recorded execution counts and explicitly unknown agent
or prompt provenance when measurement evidence is absent. Current metadata,
repeated external session ids and event cursors do not fill historical gaps.
If the bounded execution window omits recorded history, its tenure reports the
omitted count rather than calling that history unknown.

**Consults.** When the session asked other roles questions, a **Consults** panel lists each as
a lane under it: who asked whom, the identity that ran, its admission, outcome (`answered`,
`withheld` when the snapshot changed or could not be compared, `failed`, `timeout`,
`quarantined`, `interrupted`), duration, denied operations and observed changes, with a link
to the consulted session. A consult with no receipt (for example a quarantined one) reads
**no receipt** and its changes read **unknown**, never "unchanged". The question and answer
are not stored, so they are not shown. In **Models used**, the consulted role's calls are
rolled into its asker once, marked **consult**, with the original work type (a consulted
summarizer call is still a summarizer call). The Usage view's **By origin** table groups the
original events by run, subagent, flow and consult, and adds up to the total. Setup lists who
may consult whom, the ceilings, and for each captured ACP adapter whether it proved the three
transport gates; none is offered the tool today.

## Providers and limits

`#/providers` shows one card per harness and per API provider. For a harness: whether it is
installed, its last recorded **login** conclusion (`not checked` until a refresh), and its
**limits**. A limit value always carries its source and when it was observed. A harness
whose limit source Garuda has not proved for its exact version reads **unknown** (Claude
Code has no documented non-interactive source); a proved one reads **known** within a
minute of its observation and **stale**, with its age, after that; a version other than the
one the source was proved for is flagged, as is a source that supplied no account id or more
than one account seen. A limit **event** reads `active` while its reset is ahead, `expired`
once past, and `historical` when no reset time was ever reported, which never counts as a
ban. **Observed use** over 5 hours, 7 days and 30 days (sessions, native calls, ACP turns,
tokens, known and unpriced cost) is labelled *through Garuda only*, because use outside
Garuda draws on the same quota. **Refresh** (write mode) re-runs the documented status
reads and never sends a prompt. `GET /api/providers` and `POST /api/providers/refresh` serve
this.

## Usage statistics

`#/usage` summarizes the usage ledger over the last 24 hours, 7 days or 30 days (rolling UTC
windows; your time zone only changes how a client displays them): totals, the share of work
by type, a harness × model table, tokens per day, and tables by role and by opaque project
id, with the median tokens and duration per native call. **Units stay apart**: native calls,
ACP turns and snapshots are different measures. A percentage compares native calls only with
native calls, and the page lists what it left out (ACP turns, and snapshots, which are
occupancy or running totals and are never summed). **Unknown cost is never zero**: a range of
unpriced calls reads *unknown*, and a mixed one shows the known sum beside the number of
unpriced records. Replayed or duplicate reports cannot change a total, because the ledger
writes each record once. **Export CSV / JSON** downloads the fixed schema fields (identities
and counts, no text, paths or accounts). These statistics are a report and are never used to
choose a harness or a model. `GET /api/usage?range=` and `GET /api/usage/export?range=&format=`
serve them.

## Setup

`#/setup` is a read-only view of what is configured and what is wrong. **Diagnostics**
(configuration, each harness your roles use, leases and unpublished worktrees) each come
with a code and a fix you can copy; a harness's login is the last recorded check (or *not
checked*), because this page starts no vendor command and edits nothing. The **role table**
shows each role's harness, exact model id, effort, permissions and fallback chain; the
**flow list** marks the packaged examples and names any role a flow needs that you have not
defined; and **where each value came from** lists the layer behind every effective value
(package, your file, the project's trusted file, the command line), plus anything a project
file asked for that is withheld until you trust it. The **Agents** card lists every agent
definition: its source (packaged, yours or the project's) and `extends`, a shadowing note, the
settings it declares with the layer each came from, its definition digest, its static prompt
digest and each prompt section's UTF-8 bytes, characters and estimated tokens
(`chars/4`). It never shows instruction or prompt
text (use `garuda agent show NAME` for that) and a definition that cannot resolve is listed
with a source-free message and a stable diagnostic code beside the others. Legacy
warning messages and prompt-inspection failures also exclude source text. Use
`garuda agent check NAME` locally for detailed diagnostics; HTTP responses do not
copy exception snippets, even when definitions are malformed. The digests are the ones `garuda agent show` and
`garuda agent prompt` print. To change anything, edit your `garuda.yaml` or run `garuda init`.
`GET /api/setup` serves it.

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
