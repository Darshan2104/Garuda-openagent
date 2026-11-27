# Web dashboard

`garuda web` serves a local dashboard for watching sessions, answering
approvals, previewing Starters, checking usage and setup, and talking to the
native Garuda agent.

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
open that exact URL. It opens on the **Overview**. Right after it starts listening,
the dashboard reads every session record and runs the harness discovery the
Overview's own requests would run, in the background (it never delays startup,
and stops on shutdown), so the first page is fast. Press Ctrl-C to stop it: the
dashboard finishes the flow and chat operations it owns, prints
`Garuda dashboard stopped.` and exits 0. Pasting the link into a tab that is
already open works too: the dashboard takes the token, removes it from the
address bar and history, and reloads its data. If the dashboard restarted (a new
token) or the tab never had one, every page shows **This dashboard was
restarted or the link expired** with a field to paste the new link into; it
returns to the page you were on.

| Flag | Default | Use |
|---|---|---|
| `--read-only` | off | Serve without any control or conversation |
| `--allow-workspace DIR` | current directory | A directory conversations and starter previews may use; repeatable, also in read-only mode |
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
| Overview | `#/` (also `#/overview`) | The landing page: what needs you, what is running, what finished last, and the health of the harnesses your roles use |
| Flows | `#/flows`, `#/flows/<name>?workspace=N` | Every flow you can run (yours, the project's, the packaged examples): its pipeline, readiness, a preview, and an owned Linux start in write mode |
| Starters | `#/starters`, `#/starters/<id>` | Five packaged journeys, configured readiness, preview/copy, and owned Linux flow starts in write mode |
| Runs | `#/runs` | Traces and metrics of what each agent did: turns, tool calls, diffs, tokens, cost, models used, consults |
| Sessions | `#/sessions` | State, queue, approvals and control for each session (flow, Stop) |
| Approvals | `#/inbox` | Every waiting approval; Allow or Deny |
| Usage | `#/usage` | Totals and breakdowns from the usage ledger; CSV or JSON export |
| Setup | `#/setup` | Roles, flows, agents, where each value came from, diagnostics |
| Providers | `#/providers` | Each harness and API provider: install, login, limits |
| Runtimes | `#/runtimes` | Runtime health, handoffs and recovery |
| Chat | `#/chat` | A conversation with the native agent (not in `--read-only`) |

## Finding your way

Runs and Sessions list the same sessions for different jobs, and each says so
under its title: Runs for what an agent did, Sessions for what state it is in
and what you can do about it. Every run row links to its session and every
session row to its trace.

The **Approvals** tab shows a red count whenever a request is waiting, so a
parked approval is visible from every page. The count is a read-only poll of
`/api/inbox` every 20 seconds and pauses while the tab is hidden. On a window
too narrow for every tab (a phone, a split screen), the tabs fold into one menu
button that names the current page and carries the same count; `Esc` closes it.

If the dashboard stops answering, one banner says **Can't reach the
dashboard — is `garuda web` still running? Retrying…** and retries with a
growing gap (at most 30 seconds); when the dashboard answers, the banner goes
away and the page reloads its data. With more than 32 connections open at once
(many tabs), it says **Too many open dashboard tabs** instead and waits as long
as the dashboard asks. Buttons that change something (Recover, Prepare,
Providers Refresh) show **working…** and ignore repeat clicks until the request
finishes; a refusal shows the dashboard's message and error code.

| Key | Does |
|---|---|
| `1`–`9`, `0` | Switch to the nth tab (`0` is the tenth, Runtimes); `?` lists them |
| `c` | Chat (write mode) |
| `/` | Focus the page's search field |
| `r` | Reload the current view, except Chat or a view with unsubmitted edits |
| `t` | Toggle light and dark |
| `?` | Show or hide the shortcut list |
| `Esc` | Close the list or leave a field |

Keys do nothing while you are typing in a field or holding Cmd, Ctrl or Alt.
Reload stays disabled after an edited field loses focus, until its inputs are
successfully submitted or you navigate away.

## How state is drawn

Every page draws a state the same way: a small marker with its own colour
**and** shape, and the word beside it, so a state reads without colour too.
Pills (`working`), state chips (`outcome: completed`), the flow pipeline's step
markers and the coloured stripe at the left of a list row all use it.

| Marker | State | Means |
|---|---|---|
| blue dot with a halo | running, working, live | Work is in progress now |
| orange dot with a halo | waiting | It is waiting on you: an approval to answer |
| grey ring | queued | Waiting for a slot; nothing has run yet |
| green dot | completed, done, passed | It finished, or its check passed |
| red dot | failed, refused | It ended without succeeding, or its check failed |
| crimson diamond | crashed, quarantined | Its worker is gone without recording an end |
| violet two bars | paused | A flow stopped on a resumable pause; `garuda flow resume` (or quota recovery) continues it |
| grey dot | cancelled, stopped | A known end that is neither success nor failure |
| hollow grey ring | unknown, not recorded | Not known: never drawn as success, zero or blank |

Outcome and verification are always two separate chips, and a review has its
own chip that never uses verification's wording. Tags that say what something
*is* rather than its state (flow, consult, example, other project) are outlined
with no marker. A cost an adapter reported is labelled as reported, not
metered. Light and dark themes follow the system setting; `t` or the ◑ button
switches. On a phone each table row becomes a card of labelled values, and
neither the page nor a table inside it scrolls sideways (only code, diffs and
raw payloads may).

## Overview

`#/` answers "what is happening, and does anything need me?" in four sections:

- **Needs attention** lists only what applies, each with its age and a link to
  where you act: approvals waiting (with expired ones counted apart, since they
  will be denied) → **Approvals**; sessions whose worker **crashed** and
  sessions **waiting** on an approval → the session; flows **paused** on a
  resumable stop (a usage-limit pause, shown as **paused: usage limit, resets
  13:24 (in 12 min)** when the harness reported its reset, then how it ends:
  **resumes automatically after the reset**, or the `garuda flow resume <id>`
  command to run after it; a quota
  wait recorded by quota recovery; a step's own
  deadline or silence limit, or a long step's own pause: no measurable
  progress, its turn budget used, or its harness refusing to continue) → the
  session; a running flow step the live view judges near its silence limit
  (**quiet for 17 min, limit 20 — the watchdog will stop the step**) or past
  it; configuration errors →
  **Setup**; a harness a role uses that is unavailable, logged out or not in
  the runtime catalog → **Providers**. With nothing to show it says
  **Nothing needs you right now**. Crashed, waiting and paused rows older than
  a day fold into one line per kind (**3 crashed sessions older than a day**),
  newest first; expanding it shows every one, nothing is hidden or deleted,
  and an expanded fold stays expanded across refreshes.
- **Running now**: queued, working and waiting sessions with runtime, role,
  model, state and elapsed time; a flow shows the step its journal says is in
  flight (**step 1 of 2 · code**) and no step when none is, with that step's
  turn and last activity from the flow's live view. The step's own session is
  listed under its flow, linked by what it recorded (its `flow_step`, or the
  flow's live view naming it), not as a second unrelated row.
- **Recent**: the last eight finished sessions with outcome and verification
  as separate chips (a review is never shown as verification).
- **Harness health**: one row per harness your roles or their fallbacks use,
  with availability and the last recorded login reading and its age (`login
  not checked` until one exists).

The session sections come from one fast read, `GET /api/overview`, which
selects from every session rather than the newest page of the Sessions list, so an old crashed or
waiting session is never missed. Configuration and harness rows come from
the slower Setup and Providers reads, load separately with a placeholder, and
show an inline error with **Retry** if they fail; the rest of the page stays
usable. The page refreshes every 10 seconds (Setup and Providers every
minute) while the tab is visible, updates rows in place, and has no control
that changes anything.

## Flows

**Flows** lists every flow `garuda flow list` shows for the selected workspace:
your own (from your `garuda.yaml`), the project's (from its `garuda.yaml`, with a
warning that it is not your configuration and whether that file is trusted), and
the packaged examples. Each card shows its steps (step → role → harness/model,
with review loops and parallel groups), readiness, missing roles with the fix,
and **Start from dashboard: yes** or **copy-only** with the reason. A flow whose
roles cannot be resolved is listed with its error and its configured steps; a
`garuda.yaml` that cannot be loaded leaves the packaged flows listed beside its
error. Without any flow of your own the page explains how to define one.

Open a flow for its full pipeline, drawn like a finished flow on its session
page: each step's roles with runtime, model, effort, configured and effective
permission, write policy and fallbacks; its inputs (with `from` for an input
taken from a named step), outputs, review, retries, limits, `long_run`, and the
backstop a dashboard start gives it (one hour without `limits.deadline_min`,
else its deadline plus a margin). Values the dashboard cannot know are listed
as unknown with the reason.

Fill **Task** (required), **Sources** and **Explicit checks** (one command per
line) and press **Preview**. The preview shows readiness with every blocker and
warning and its fix, who runs each step with configured and effective permission
against the dashboard ceiling, the checks and their authority, limits, the
equivalent `garuda flow run` command with **Copy**, and what to know before
starting: a step without `deadline_min` is stopped at the backstop (the actual
minutes), a flow started here is never resumed (`garuda flow resume` refuses
it), and stopping the dashboard interrupts it. Editing any field discards the
preview. The task the flow receives starts with a title made of the first words
of your **Task** (lowercase letters and digits only), so its session is named
after your task, as the equivalent `garuda flow run` command names it (`implement-the-textkit-package-so`,
then `-2` for the next one), rather than after the starter's instructions. A
role run from **Starters** (**Run a task as a chosen role**) is named after its
**Goal** the same way when **Session name** is left empty.

**Start flow** appears only in write mode on Linux, for a flow the owned engine
runs (no step retries, no `isolation` other than `shared`, no parallel group, no
`long_run` on a non-ACP role), with every step within the ceiling and readiness
without errors. It sends the `run-flow` starter's preview and authority digests
with one operation id, reused on a retry, so a repeated click returns the same
flow. Once admitted, the page opens the flow's session at
`#/sessions/<id>?operation=<uuid>`, whose operation panel shows the operation and
its approvals: **Allow** or **Deny** them there or on **Approvals** (each waits
up to 5 minutes unanswered, then it is denied), which lists
them beside the session-file requests (marked **dashboard flow**, linking to the
session) and counts them in the nav badge and the Overview; the step's own file request for the same ask is shown and counted once, as this one. Such an approval is
held by this dashboard, not a session file: an answer is the decision at once,
bound to the ask's owner and digest, so any tab that lists it may answer it. Every
run a tab started or opened stays watched from any page of that tab, even
hidden; about 90 seconds after no dashboard tab is watching a run (its tabs were
closed), a waiting approval is denied, recorded as "stopped watching", and the
step sees the denial while the flow keeps running; if the dashboard stops, a
waiting approval is denied at once. Below its waiting asks the operation panel
lists the ones already settled, each with who settled it as the dashboard
recorded it: **denied by you** (or approved), **the dashboard stopped
watching**, or **nobody answered**; the dashboard keeps that record in memory,
so a restart forgets it (the step session's own record keeps only allow, deny
or disconnected). An answered card on **Approvals** leaves at once and does not
come back while the server still lists the request. A finished run's starter card
offers **Run again**, back to the flow with the workspace selected and the
recorded task filled in to edit.

Everything else is copy-only, with its reason: a read-only dashboard
(`dashboard.read_only`), a host without the Linux supervisor
(`starter.ownership_unavailable`), an unsupported flow shape
(`starter.retries_unsupported`, `starter.isolation_unsupported`,
`starter.parallel_ownership_unavailable`, `flow.long_run_unsupported`), a step
above the ceiling (`starter.permission_exceeds_ceiling`), a role the project
file chose (`starter.project_role`), or another readiness error. Preview and Copy
always work.

## Starters

Start with `garuda web --read-only --allow-workspace .` and open **Starters**.
Choose a journey, enter its goal or question, and optionally name repository
regular non-symlink repository sources (`path#section`) or existing context
(`session:NAME`), one per line.
**Load reconnect example** fills the installed example; it does not create files.
The build starter lets you select its flow variant and an explicit validated
plan reference. Select a workspace from the operator's allowlist.

**Preview command** shows the effective flow source, configured runtime/model
bindings, permission posture, review policy, source and configuration digests,
checks and their authority, configured invocation/review bounds, and unknown
cost. Expand the provenance details to inspect these values. Readiness describes
configured preflight; it does not prove current login, versions or observed
capabilities. Named sources do not prove a runtime read them. A question also
shows its live-checkout/no-edits limitation.

With one harness, remedies appear in order: connect a second harness, **Build
and check** (explicitly **no review**), or author a waiver in your configuration.
A user-authored waiver stays labelled **review not independent**. Build-review
previews show authorized configured checks and their authority. Linux CLI starts
and Linux write-mode browser flow starts can produce candidate-bound
verification. Preview itself remains pure and never launches a check.

**Copy command** copies the exact quoted CLI command. Run it explicitly in a
terminal. Changing inputs invalidates the preview; preview again after sources
or configuration change. Equivalent commands use the ordinary flow/run CLI and
record ordinary sessions; `garuda starter run` additionally records structured
starter inputs/source provenance and recompiles at launch. Library/detail/result
reads and POST preview create no sessions, queue rows, files, processes or model
calls.

On Linux, a dashboard started without `--read-only` shows **Start flow** when a
flow-kind preview is ready, fits `--max-permission`, and has the required runtime,
approval and process-owner capabilities. Start creates one durable operation and
runs through the existing flow engine. The operation ID stays with the page while
it polls, so a duplicate request returns the same admission and never launches a
second flow. Source, configuration, role/runtime scope or authority changes after
preview refuse before a child starts. A busy workspace, full runtime capacity or
another refusal raised before the flow reserves its parent session is reported as
`refused`; that operation stays refused, so preview and start again. Closing the
tab does not cancel the owned
operation. Garuda stops new admission during server shutdown, drains its tasks,
and never replays uncertain work after restart. Read-only dashboards, unsupported
hosts and unsupported run options remain copy-only.

The same Linux write-mode dashboard shows **Start run** for a ready
`run-with-role` preview and **Ask role** for `ask-role`. Each runs as one owned
parent session with exactly one role step, under the same operation, digest,
duplicate and refusal rules as **Start flow**. Foreground runs drain on
server shutdown; background runs belong to their detached worker. The role child receives
the exact previewed task, through the shared owned native/ACP role executor;
ACP never runs through a dashboard chat. Ask-role runs read-only with the
no-edits check and withholds output if the checkout changed. Configured and
explicit checks run through the flow engine's candidate-bound verification.
Session names and `worktree`/`auto` isolation are supported. The parent selects
and leases the execution checkout before launching its child, preserving the
previewed source authority. Uncommitted source changes are not copied to a
worktree; its session records that fact. Refused, never-admitted worktrees are
removed after preparation settles; admitted worktrees remain for inspection.
An approval the role child asks appears on the **Approvals** page and waits there
for up to 5 minutes, like any dashboard approval, even though the child runs in
its own process. An answer after that is a denial, and so is a dashboard that
stops while the request waits.
**Queue in background** creates the intent's reserved session and binds its
operation before enqueueing. The detached Linux worker waits without a workspace
lease, revalidates the admitted configuration and sources, then owns the role
and any checks. It continues after tab or dashboard shutdown. Requests with the
same operation ID inspect that session and queue binding, including after a
restart or a lost HTTP response; they never enqueue or spawn again. Answer
background requests in the **Approval inbox**, or with `garuda approvals`.
**Stop** uses the recorded worker identity and waits for owned cleanup; uncertain
closure retains capacity. Queued cancellation removes work before dispatch.
Missing or conflicting publication is shown as uncertainty and is never retried
automatically. A foreground operation remains nonterminal while its completed
parent awaits the matching terminal intent publication; status reads only project
the durable record and do not advance or replay it. Permissions above
`--max-permission` still refuse before any
operation is recorded; copy the equivalent command if that ceiling is unsuitable. An unchecked native ask-role takes a read-only parent lease,
so it can inspect alongside an editor. Configured acceptance checks retain a
mutating parent lease because checks may write. The authority preview discloses
the effective lease mode; concurrent edits can still invalidate a no-edits answer.
Read-only/no-edits ACP roles require a user-configured confinement image and
Docker. Their runtime can be installed only in that image. The parent owns the
probe and execution containers, records their identities before launch, and
verifies removal before releasing its lease or capacity. Failed confinement
never substitutes a host run; ambiguous cleanup retains quarantine.
Roles with fallback chains probe only after durable parent admission. The
preview freezes every candidate and enforces the permission ceiling on each.
Missing CLIs, documented logged-out answers and proved quota exhaustion can
select the next candidate; uncertain status keeps the current one. Probe
descendants close and the selection is recorded before the runtime is prompted.
Image-only read-only candidates retain unknown host availability until owned
confinement starts them; host absence does not select another harness.

Follow the resulting session through **Sessions** (flows and ACP) or its existing
native trace. The shared starter panel keeps process, work, outcome, review and
verification separate; it shows actual recorded identities, historical artifacts
and acceptance receipts, supplied-source provenance, and bounded selected-session
coverage. Ordinary runs show **Recorded run evidence** with absent starter
provenance explicitly unknown. Missing or unreadable records stay unknown;
a partial page does not
prove project completion. Historical receipts do not probe the current workspace.
**Preview implementation of this plan** opens the build form with the validated
plan selected. It requires another explicit preview/copy step and starts nothing.
See [Ready-to-use workflows](../use-cases/workflows.md) for the five journeys.

## Runs

The run list (`#/runs`) covers every session in the store. It loads the newest
100 that match the search and filters and says how many there are in all
(**100 of 1,160 runs loaded**); **Load older runs** under the table brings the
next 100, and the tiles and charts say when they cover only the loaded runs.
Search matches the task or session id across the whole store. The search, the
status and the agent are part of the address (`#/runs?q=fib&status=failed`),
so a filtered list survives a reload, can be shared, and Back undoes a filter.
Each row shows the whole task with the session's name under it; the task links
to the trace and the name to the session. On a phone the rows stack.

The list counts each run by its recorded **outcome**, so a
native run (`success`), an external-harness run or a flow (`completed`) count
alike. The **Completed** tile is completed ÷ finished runs; it says how runs
ended, not whether anyone checked the result, so verified runs are counted
separately. Turns, tokens, cost and duration a run never recorded (an
external harness or a flow records no turns or tokens) show as **—** or
**unknown**, never 0; totals say how many runs they cover. A flow's duration
runs from its start to its last step receipt. The status filter matches the
outcome: **completed** finds native and external runs alike. A flow paused on
a resumable stop is drawn **paused** with its reason and how it resumes
(`pause`, the same field as the Sessions rows), in the list and in its run's
header; like the Sessions list, its filter and the outcome counts read its
recorded outcome (`failed`), so **failed** finds it.

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
- **What Garuda recorded** leads the page for a Claude Code, Codex or other
  ACP run with no native log: the full task, the outcome, the duration (the
  same figure as the run list: recorded start to recorded end, **—** when no
  end was recorded), prompts sent, the stored final message, the harness and adapter
  version, role, model, effort, harness mode and write policy, a per-turn
  count of message events, tool calls, tool results, approval requests and
  errors, and each approval with its outcome (approval requests with no stored
  decision say so). Garuda never stores the harness's own messages or tool
  calls, so one short **Not recorded** note replaces the empty trace,
  context-pressure and raw-event panels. A run that mixes a native lane with
  an external one shows the same facts as an **External harness** card above
  its native trace. **Models used** adds each turn's context snapshot, with
  the model the harness reported and its estimated cost when it gives them.
  The run's **Cost** tile shows that adapter-reported cost, labelled as such,
  or **not reported by the adapter**.
- **A flow's own run** (a flow parent, reached from the run list or the
  session page's **Trace**) has no event log of its own: each step ran in its
  own session. Its page is the flow: the task, how it ended (outcome,
  verification and review as three separate facts, with review rounds and
  coverage), the duration (the run list's figure, recorded start to end), the
  final message, then the steps in order with each attempt: who ran it, its
  status, duration, turn-loop counts, what it consumed and produced, an **Open
  trace** button for that step session's own trace and a link to its session.
  **Measured** shows each attempt's receipt measure (prompt size as an
  estimate, turns, tool calls, wall time, peak context of context size, the
  adapter-reported cost) and the flow's totals; peak context is occupancy, not
  billable tokens, the cost is not metered by Garuda, and anything a receipt
  did not record reads **not recorded**. A stopped flow says why in one
  sentence from its recorded stop; a running flow says which step of how many
  is in progress and refreshes every 10 seconds. Artifact edges name the
  producing and consuming attempts, so two review rounds no longer read as a
  duplicate.
- **Following a live run** patches the page in place every few seconds. A turn
  or panel whose content did not change is left alone, so an expanded turn, a
  loaded subagent trace, the raw-events window with its filter and the focus
  in it, the Models used panel (refreshed once when the run ends), the scroll
  position and a text selection all stay put. A part holding your text
  selection is updated once the selection is cleared. While the run is live,
  its missing `session_end` and its unfinished last turn are shown as **in
  progress**, not as alarms; a run that is no longer live and never finished
  keeps the **No session_end** and **TURN NEVER FINISHED** notices. If the
  dashboard stops answering, the Live card says **can't reach the dashboard —
  retrying**, retries with a doubling wait of up to 30 seconds, and resumes
  on its own.
- **Raw events** loads up to 500 events at a time, and its filter searches
  that loaded window only. A filter with no match names the events it
  searched and offers **Search later events** / **Search earlier events**,
  which walk the rest of the log a window at a time until one matches.
- A link to a run that does not exist says **No such run** and links back to
  Runs and Sessions.
- **Consults** lists each question the session asked another role as a lane:
  who asked whom, the identity that ran, its outcome (`answered`, `withheld`,
  `failed`, `timeout`, `quarantined`, `interrupted`), duration, denied
  operations and observed changes, with a link to the consulted session. A
  consult without a receipt reads **no receipt** and its changes read
  **unknown**. The question and answer are never stored or shown.

## Sessions

The Sessions page lists sessions newest first, 50 at a time: their state
(**queued** with its position, **working**, **waiting** on an approval,
**crashed** when its worker is gone, or how it ended), runtime, role and
model, outcome and verification as separate chips, workspace and branch,
Garuda's usage and cost (`unknown` until metered) with the spend gate in one
line, approvals waiting (expired ones counted apart), and when each was
updated and started. Each name has the session's short id and the first words
of its task beneath it, so two runs of one task are told apart. The header
says how many are shown out of how many exist; **Load older** fetches the next
page. The table fits a laptop screen; on a phone each session is a compact
card: name, state and when it was updated; runtime, role and model;
outcome and verification as two separate indicators; approvals only when one
waits; workspace, start and usage/cost (left out when both are unknown) behind
**details**.

Search (name, id, task, model, agent, role, runtime, workspace or branch) and
the state chips (**crashed 3**, **working 1**…) filter on the server, across
every session rather than only the loaded rows. The filter lives in the
address, as `#/sessions?q=…&state=…`: a reload keeps it, the link can be
shared, Back and Forward step through state-chip changes, and a session's
**All sessions** button returns to it. The list refreshes every 15 seconds while
the tab is visible and changes only the rows that changed.

A session's page leads with the work, and keeps the evidence below:

- **Waiting for you**, at the top when the session has pending approvals, with
  a link to answer them on the Approvals page.
- The task (its first lines; **Show the full task** for the rest); runtime,
  role and model; workspace and branch; when it started, and when it ended and
  how long it took (or how long it has been running), each relative with the
  absolute time beside it. An end that was not recorded says so.
- **Result**: outcome and verification as two chips (a review gets its own),
  the stored final message (it may be clipped, and withheld output is not
  shown), the files the agent's working state recorded it wrote, and for a
  failure or a crash the cause in one sentence with what to do next.
- For a flow, a vertical pipeline of its steps: who ran each (role → harness
  and model), its status, attempts with their duration, workspace change and
  a link to each attempt's session, the artifacts it passed on, and the review
  outcome with its rounds, such as **approved after 2 rounds** (never shown as
  verification), with **coverage: partial** when the reviewer saw only part of
  the change set. A long step's attempt adds its turn loop as its receipt
  recorded it (**7 turns · 1 rotation · 5 continuations · 1 repair**); a
  one-turn step shows none. A stopped attempt names its stop code in a plain
  sentence, in red only while its failure stands: an attempt a later attempt
  of the same step replaced with a success is history, drawn neutral
  (**interrupted · replaced by attempt 2** with its sentence). The step in flight is highlighted. When the flow stopped, the
  Result says why in one sentence and, for a resumable stop (a deadline,
  silence, no progress, the turn budget, a refusal, a failed step or missing
  output), that `garuda flow resume <id>` runs the step again. A paused flow
  leads its Result with the pause: why, and for a usage limit when it resets
  (**resets 13:24 (in 12 min)**), how it ends as the session row's
  `pause.resume` says (**automatic**: quota recovery resumes it after the
  reset; **manual**: the `garuda flow resume <id>` command, with a Copy
  button, to run after the reset — a checked flow stopped on a usage limit is
  manual). Without a recorded reset time it shows the read model's own
  sentence (`pause.action`), any timestamp in it in local time; a raw
  timestamp is never shown. Once the reset time has passed and nothing
  resumed it, it says **reset passed; run the command to continue** rather
  than counting below zero. A pause is not a failure to investigate: its
  failure evidence stays in the collapsed **Details and evidence**. The pause
  is said once, there: a paused flow has no live step card (it is not
  running), the pipeline's paused step reads **paused — usage limit, see
  Result**, its stopped attempt reads **stopped on a usage limit — see
  Result** in the pause style (the raw stop code in its tooltip; the red
  callout is for failures only), and the header, the flow trace, the Overview's paused rows and
  the Sessions list keep one line each. A flow paused on its own pause (a usage limit, a
  deadline, silence, no progress, the turn budget or a refusal) says **paused**
  in its header; its recorded outcome stays as stored (`failed`) and the
  session row's `pause` field says why, the same in `garuda sessions --json`.
  A failed step is a failure, not a pause. An interrupted attempt shows the
  counts its receipt recovered from the step's progress file, labelled **last
  known, from the progress file**, with its last turn's decision and time.
  A flow whose process stopped mid-step (`interrupted`, no recorded stop) says
  so: **Interrupted: the flow's process stopped before step build finished**
  and, for an attempt settled as `flow.interrupted`, that `garuda flow resume
  <id>` runs it again; a launched attempt that recorded no outcome is never
  replayed, and the page says that instead.
  A step killed
  by the supervisor's backstop (`flow.step_backstop`) is quarantined instead,
  and the page says to set `limits.deadline_min` for a step that runs that
  long.
- **Step in flight**, for a flow whose step is running now (the live view's
  `running`; an open attempt whose process is gone shows no card and no quiet
  line): the attempt, when it
  started and for how long, who runs it (role → harness/model, read from the
  step's own session), links to that session and its trace, turns · rotations
  · continuations, the last turn's stop reason and decision in plain words,
  the last activity and how long the step has been idle, context occupancy as
  **56k of 200k (28%)** with a small meter (how full the harness reported its
  window, not billed tokens), time left to the step's deadline, the last
  checkpoint, and the agent's own last progress note (escaped, collapsed when
  long, labelled as its words rather than a Garuda fact). A **Quiet** line
  shows how long the step has been quiet against the silence limit that applies
  (**quiet for 12 min of a 20 min limit**) with the live view's own judgement
  as a state: within its limit; a tool call running (the longer tool limit
  applies); waiting on an approval (silence is not counted); nearing the limit;
  or past it. A step without a silence limit (no `limits.silence_min`) reads
  **no silence limit is set for this step** instead of "within its limit". **Recent turns** lists the attempt's last turns newest first
  (turn, harness session, what happened in words, duration, files changed,
  context), three shown and the rest one click away. A field the live view
  does not report (a step without limits, an older flow) leaves its line out
  rather than guessing. The step never reads
  "not run yet" while it is in flight, nor once a launched attempt's process is
  gone without a recorded outcome (a Stop, a crash): the pipeline then reads
  **interrupted · attempt 1**, says the process exited before it recorded an
  outcome, and links the attempt's step session and trace. A step stopped on something that is
  not a pause (a quarantine) says why here, and `garuda flow resume <id>`
  where the engine lists the stop as resumable. An older flow, or one whose
  records the reader cannot read, says **No live detail recorded for this
  step**. Every value is the shared live view (`garuda flow show --json`'s
  `live`), nothing estimated; the block updates in place on the page's
  refresh. The flow's Trace page shows the same block.
- A finished flow's **Measured** table: what each attempt's receipt measured
  (prompt size, turns, tool calls, wall time, peak context, reported cost) and
  the flow total, the same table as its Trace page.
- For a session started from a Starter, its result and, when there is one, the
  next action (a plain completed flow is no longer told to configure checks).
- **Live events** (not for a flow, which has no event log of its own): the
  session's event count and the last event's type. It
  polls the log every 2 seconds while the tab is visible and holds no
  connection open, so many open session pages do not slow the others down; it
  reads **connected · waiting for events** when the session is quiet, and
  continues from a byte offset, so nothing repeats or is skipped. If the
  dashboard does not answer it says so and retries with growing pauses (up to
  30 seconds) until it is back; a refused request (401, 403, 404) says why and
  stops, with **Retry now**.
- **Usage**: Garuda's usage and cost, what the harness itself reported (an
  external session's adapter figure, or for a flow the total over its step
  sessions, labelled *adapter-reported, not metered by Garuda*), and every
  spend-gate value. A session with no native event log (a flow, whose steps
  have their own; an external harness, whose trail is on its Trace page) and
  no spend snapshot says so in one line instead of showing empty panels.
- **Details and evidence**, collapsed: the four state facts, ownership and
  reconciliation, artifact references, failure evidence (shown open above
  instead when the session failed or crashed), and a Starter's coverage,
  integrity and recorded identities.

While the session is active the page re-reads it every 10 seconds and as soon
as its log shows the state changed or ended, and updates each section in place:
what you opened stays open. **Stop**, for a background session, asks you to
confirm, is disabled while the request is in flight, and shows the server's
answer: the session leaves the queue, or its worker is stopped once its
recorded process identity matches.

`garuda sessions --json` prints the same rows: the CLI and the dashboard read
one model. A session whose record is damaged (a `meta.json` that isn't a JSON
object, a field of the wrong type, a file the dashboard can't read) is left out
of the Runs, Sessions, Approvals and Overview lists, and each page says **N
sessions could not be read**, naming each one with a link to its page; that page
says in one sentence what is wrong with the record (`session_unreadable`)
instead of failing. A flow step's own session names its flow and step in the
list (**step build (attempt 1) of flow …**) and on its page. A queue that can't be read shows as `unknown`, not as empty. A
session row's own usage and cost still read `unknown`; the Usage page has the
totals, and a session's detail adds `reported_usage`, what an external harness's
adapter reported (see [Sessions](sessions-and-flows.md)).

## Chat

`#/chat` starts nothing on its own. It resumes the chat this tab was using (or
the newest open one), and otherwise shows **New chat** with the workspace,
agent, model and permission choices the server accepts. A chat runs Garuda's
own model loop, so the model suggestions leave out what this machine ran that
such a loop cannot call (`flow`, `acp:claude/haiku` and other external agents),
and a chat asked for one is refused with the reason; run an external agent as a
role from **Starters** or **Flows**. If the previous chat is
no longer open on the server (ended, reclaimed after sitting unwatched, or the
dashboard restarted), the page says so instead of opening a new one; its
transcript stays in Runs.

- A typed message is kept per chat while you visit other pages.
- **End chat** asks first when a turn is running or an approval is waiting.
- If the dashboard stops answering, the page says so, retries with backoff (up
  to 30 seconds between tries) and recovers by itself.

A tool call that needs approval shows a card in the chat. The chat's poll is
also its heartbeat: while a turn runs or an approval waits, it keeps going when
the tab is in the background or you are on another dashboard page (the Chat
tab then shows a count). The same holds for every chat the tab has open:
**New chat** inside a chat starts another without dropping the first, and while
you work in one, the others that wait for an answer are listed above it with a
link back (and marked in the New chat page's list of open chats). An approval
nobody answers is denied after five minutes, also while the tab is open; ending
the chat denies it at once. Once the tab is closed, the server denies the waiting
approval after a 90-second grace window, so the run is not blocked for the full
five-minute timeout. 90 seconds is longer than the slowest heartbeat a
background tab can send (browsers may wake a long-hidden tab's timers only once
a minute). The transcript says who decided each denial: **denied by you**,
**the dashboard stopped watching**, **nobody answered** within the timeout, or
**the chat was closed**. A second answer to the same approval gets a 409 that
names the first outcome.

## Approvals

The Approvals page lists every approval waiting in an interactive session (a
`garuda chat`, or a dashboard conversation), every request a background flow
step parked in its session's file channel, and the asks of flows started from
this dashboard. It answers a session-file request by writing a session-bound
answer for the session's approval broker. A successful response
means "answer recorded", not "the tool will run": the broker checks the
session, request digest, nonce, expiry and permission ceiling, and records one
decision.

- The header counts what can still be answered, as the tab's red count does;
  expired requests are listed apart under **Expired** (the broker denies them).
- The page refreshes every 5 seconds while the tab is visible. A new request
  appears and an answered or withdrawn one goes away without rebuilding the
  other cards, so nothing moves under the pointer.
- Both buttons are disabled while an answer is in flight. The page sends the
  digest it showed you. A request that changed since is refused
  (`stale_request`, 409); a second answer, or one after the decision, loses
  (`already_answered`, 409); an expired request is refused (`expired`, 410)
  and denied. The card shows the server's message, code and status.
- If the browser abandons an approval it was polling, Garuda denies it instead
  of leaving the run blocked.
- How long a request waits, by kind (the page says so at the top):
  - a session-file request (a chat, a background flow step) waits until its
    expiry, shown on its card: 300 s, or for a background step
    `approvals.park_timeout_sec` (300 s by default, 30 s to 24 h). After that
    it is denied, and an answer is refused (`expired`, 410);
  - a flow started from this dashboard asks here directly, also when the step
    runs in its own role worker process: the answer is the decision at once.
    It is denied after its timeout unanswered (5 minutes), about 90 seconds
    after no dashboard tab is watching its run, or at once if the dashboard
    stops (the step's process sees its parent gone and takes that as a
    denial).
- Headless runs (`garuda run --bg`) deny approvals at once, so they never
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
- **Unknown tokens are never zero either.** Calls that recorded no token count
  read *unknown*, and a mixed row shows the known sum beside how many calls
  recorded none.
- **ACP turns read *not metered*** while external harnesses' usage is recorded
  only as adapter snapshots (running figures, never summed); the tile counts
  those reports and the sessions that sent them.
- **Export CSV / JSON** downloads fixed schema fields: identities and counts,
  no text, paths or account names.
- These statistics are a report. They never choose a harness or model.
- After project-key recovery, usage groups old IDs through verified aliases;
  an unfinished recovery refuses grouped reporting until
  `garuda doctor --recover-project-ids` completes. Raw exports keep each
  record's original ID.

## Providers

`#/providers` shows a card per harness and per API provider: whether it is
installed, its last recorded **login** check with when it was taken
(**checked 3d ago**; `not checked` until a check runs), and its **limits**.
Runtimes, Providers and Setup all show this one recorded check, so they never
disagree. Reading any of these pages runs no login check and changes
nothing; only **Refresh** (write mode, here or **Refresh login checks** on
the Runtimes page) renews it by running each harness's documented status
command. A limit reading shows its age the same way (**observed 3d ago**).

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
  as `garuda doctor`, except that a login shows its last recorded check and
  its age instead of running it; the version command of each harness your roles use
  does run, and no prompt is sent). Counts per level come first, errors are
  listed before warnings, and a code that repeats more than three times (one
  `lease.stale` per crashed run, say) folds into one expandable row.
- The **role table**: each role's harness, exact model, effort, permissions
  and write policy, harness mode (**by permissions** when the role names
  none), agent and fallback chain.
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

- runtime health, login state (the recorded login check and its age, the
  same reading Providers and Setup show; opening the board never runs it, and
  **Refresh login checks** renews it in write mode), version, capabilities,
  setup guidance, and
  vendor-reported quota where available, without inventing missing data;
- a preview and an explicit **prepare** step for a native-to-ACP handoff;
- the session's own changes, separated from changes that were already in the
  workspace; and
- recovery: classify a session, or run recovery.

The session fields suggest recent session ids and the target field suggests
runtime ids; clicking a runtime fills the target and updates an earlier automatic
selection. Editing the target keeps your value when you click another runtime;
the other session fields also keep what you typed.
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

The HTTP transport checks Host, Origin and token before reading request bodies.
`Content-Length` must contain only decimal digits. Invalid values, differing
duplicate lengths and any `Transfer-Encoding` receive HTTP 400; lengths
above 12 MiB receive HTTP 413, without reading the body. Header refusals close
the connection so an unread body cannot become another request. The dashboard
allows up to 32 simultaneous connections, each with a 30-second read timeout;
one more is answered `503` with `Retry-After` and the message *too many open
dashboard connections; close some tabs*.

**Errors.** Every API error is JSON, `{"error": {"code", "message"}}`, with the
same security headers as any response. `400 invalid_request` means the request
itself was wrong (a malformed session id, query parameter or JSON body). A
fault while serving a valid request is `500 internal_error`, logged with its
traceback on the server; the response carries no internal detail. One damaged
session is `409 session_unreadable` on its own routes; one still being written
by a non-atomic writer is `503 session_being_written` with `Retry-After`. An
agent loop that does not answer in time is `504 unavailable`.

**List responses.** `/api/runs`, `/api/sessions`, `/api/inbox` and `/api/overview` carry
`unreadable_sessions: {"count", "ids", "being_written"}` (at most 50 ids; the
count is exact). The lists are served from an in-memory index of session
records only: queue positions, approvals, leases and flow journals are read on
every request. A record is re-read when its `meta.json`, its session directory
or its publication policy record changes, and sessions created or deleted since
the last request are noticed. Listing never waits on a writer: a record locked
mid-publication shows its previous version, or with none is listed under
`being_written` and read again on the next request. Records over 1 MiB are
reported unreadable rather than read. `/api/runs/<id>/buffers` and
`/api/memory/proposals` take `limit` (default 200, at most 1000) and report
`total`; `/api/runs/<id>/raw` refuses a document over 16 MiB with `413 too_large`.

**Runtime discovery.** `/api/runtimes`, `/api/runtimes/<id>` and
`/api/providers` share one harness discovery for 30 seconds, so moving between
those pages doesn't re-run every harness's version and status checks. A
runtime's detail on its own checks only that runtime. **Refresh** on the
Providers page discards it.

| Route | Purpose |
|---|---|
| `GET /api/flows` | Every available flow for an allowlisted `workspace` index: source, file and trust, steps, roles, missing roles, typed `error`, `readiness`, `pipeline`, and `start` (`supported` and `reasons`), with `host` and recorded `logins`; a pure read, also read-only |
| `GET /api/flows/<name>` | One flow, the same fields; `404` when no flow has that name in the workspace |
| `GET /api/scenarios`, `GET /api/scenarios/<id>` | Installed starter library/detail and configured readiness; optional allowlisted `workspace` index |
| `POST /api/scenarios/preview` | Pure compilation: `starter_id`, bounded `inputs`, optional integer `workspace`; also available read-only |
| `POST /api/scenarios/start` | Write-mode Linux flow admission: exact `operation_id`, `starter_id`, `inputs`, `workspace`, `preview_digest`, and `authority_digest` |
| `GET /api/scenario-operations/<operation_id>` | Durable admission/flow projection for polling and approval-heartbeat refresh |
| `GET /api/scenario-runs/<id>` | Shared selected-session evidence; `limit` 1–200 and non-negative `offset`, optional workspace index for names |
| `GET /api/runs`, `GET /api/runs/<id>` | Runs (`q`, `status`, `agent`, `model`; `limit` up to 500 and `offset` page the whole store; the reply carries the true `total` and every `agents` name) and one run's trace |
| `GET /api/runs/<id>/tail` | Poll a run for new events |
| `GET /api/sessions` | Newest first: `limit` an integer >= 1 (default 100, capped at 500), `offset` >= 0, `q` (search) and `state` (a state label) filter across every session. `sessions` holds each row: the four facts, the derived `crashed` label, `pause` (why a stopped flow is paused on a resumable stop, or `null`), queue position, worker, approvals waiting (`approvals_pending`, of which `approvals_live` not expired), agent digest, resume provenance; with `total`, `matched`, `counts` (per state label, after the search), `next_offset` and `unreadable_sessions` |
| `GET /api/sessions/<id>` | One session, with pending approvals, its stored `final_message`, `files_changed`, `ended_at`/`duration_ms`, `starter_origin` and, for a flow, its steps, attempts, review outcome and `flow_progress` |
| `GET /api/sessions/<id>/stream` | Live events as server-sent events (the session page polls `/api/runs/<id>/tail` instead) |
| `GET /api/sessions/<id>/conversation` | The Models used, links and lanes data |
| `POST /api/sessions/<id>/cancel` | Stop a background session (write) |
| `GET /api/overview` | The Overview's session sections, selected from every session: crashed, waiting and paused sessions (each list `items`, at most 50, and `total`), running sessions with a flow's in-flight step, the last 8 finished, and approval counts (`waiting` includes `live`, the asks this dashboard holds for flows it started) |
| `GET /api/inbox` | Every pending approval across every session, with the digest an answer must bind (`approvals`, from the session files, as the CLI reads them), and `live`: the asks this dashboard holds for flows and runs it started, each with its `owner`, `digest`, operation, session and the broker's timeout and grace; listing them refreshes no heartbeat |
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

Starter preview accepts field data, never runtime, flow or catalog definitions,
and cannot grant cross-project context access. Explicit Build and check uses
`remedy: "build-and-check"` with `starter_id: "build-review"` and optional
`checks`; it reuses the same role-run preview owner. Normal source/private-store
authorization and request body limits apply. Start accepts exactly the fields in
the table; `operation_id` is a canonical UUIDv4 and both digests are lowercase
SHA-256 values returned by the matching preview. The server recompiles and derives
current authority; browser-supplied envelopes, paths, definitions and executables
are never accepted as authority. The same token, Host, Origin, workspace allowlist
and body limits apply. Read-only mode refuses both operation routes.

**The live stream.** Each event's `id` is a byte offset into the session's
log, so reconnecting with `Last-Event-ID` continues with the next event.
Both the run tail and the session stream skip complete records larger than
their 4 MiB window by scanning to the next newline with bounded memory.
The tail counts these records in `malformed`; the stream reports a `skipped`
event with a `malformed` count. Later events remain available,
and the persisted log is unchanged. A torn final record keeps its cursor until
its newline arrives, including when it is oversized.
If reading the session fails mid-stream (its record is locked or damaged), the
stream sends an `error` event, `{"error": {"code": "stream_failed", …}}`, whose
`id` is the last delivered offset, and closes cleanly for the client to reconnect.
The stream sends a heartbeat every 15 seconds,
ends with an `end` event once the session is finished and a poll delivers no
records and makes no cursor progress, including a torn final append, and closes
after a minute for the client to reconnect. A browser `EventSource` can't send
the token header, so read it with `fetch`.

## Safety model

| Control | Limits |
|---|---|
| Capability token | Who can call dashboard routes |
| Loopback bind | Network exposure |
| Workspace allowlist | Which directories conversations and starter previews can use |
| `--max-permission` | What a browser request can ask for, including any subagents it starts |
| `--allow-agent` | Which agents a request can name; a request never sends a definition |

See [Safety and workspaces](safety-and-workspaces.md) for workspace and
permission guidance. Run the opt-in real-browser checks in
[Browser checks](../development/browser-checks.md) after changing dashboard
behavior or presentation.

Closing a chat cancels and waits for its active turn before closing the
session, capturing final workspace evidence and releasing the workspace.
Cancellation of the close request does not abandon this cleanup; another chat
cannot acquire the workspace while the previous turn is still settling.
