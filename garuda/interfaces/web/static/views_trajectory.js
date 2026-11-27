/* The trace view: one run, turn by turn. The reason this project exists.
 *
 * A turn is rendered as the four things a turn *is*, in the order they happened:
 *
 *   IN       what the model was given — the task, or an interjection, or (the usual case)
 *            the previous turn's tool results
 *   THINKING its reasoning, when the provider returned any
 *   SAYS     its assistant text
 *   DOES     the tool calls it made, each coloured by what family of thing it is
 *
 * Colour carries the family, not the outcome — reading vs writing vs running a command vs
 * reaching the network vs delegating to a subagent — because "did it touch anything" is the
 * question you scan a trace for. The outcome is a badge, so a failed read still reads as a
 * read. All of it lives in app.css: no JS here picks a colour.
 *
 * The gate lane is aligned to the timeline by CSS grid, not by measuring pixels: each turn
 * contributes exactly two children to one grid, so turn N's card and turn N's gate share a
 * row and auto-size together. Expanding a turn cannot desync them.
 *
 * Everything rendered here is model-written — task text, tool arguments, tool output, gate
 * feedback — so every interpolation goes through esc(). */

"use strict";

/* Output a card shows before you ask for the buffer. Tool output is capped by the shaper at
 * run time, but a shaped 8k result still buries the page. */
var STEP_OUTPUT_CHARS = 1800;
var DIFF_MAX_LINES = 40;
/* Above this many turns, start collapsed. Below it, a trace you can read top to bottom
 * without clicking anything is the whole point. */
var AUTO_EXPAND_LIMIT = 15;

/* What kind of thing each tool is. Drives the colour and the icon, and nothing else — an
 * unknown tool falls through to `other` and still renders, which matters because a profile
 * can carry MCP tools this file has never heard of. */
var TOOL_FAMILY = {
  read_file: "read", ls: "read", glob: "read", grep: "read", search_tool: "read",
  read_pdf: "read", read_spreadsheet: "read", image_read: "read", task_output: "read",
  buffer_grep: "read", buffer_list: "read", buffer_query: "read", buffer_slice: "read",
  write_file: "write", edit: "write", multi_edit: "write",
  bash: "exec", bash_background: "exec", kill_task: "exec",
  tmux_exec: "exec", tmux_capture: "exec",
  web_fetch: "net", web_search: "net",
  invoke_subagent: "subagent",
  task_complete: "gate", contract: "gate",
  todo: "plan", update_goal: "plan", use_tool: "plan"
};

var FAMILY_ICON = {
  read: "◇", write: "✎", exec: "▸", net: "⇅", subagent: "⑃",
  gate: "◈", plan: "☰", other: "•"
};

function toolFamily(name) {
  return TOOL_FAMILY[name] || "other";
}

function runDetailView(sessionId) {
  var base = "/api/runs/" + encodeURIComponent(sessionId);
  return loadTrajectory(base, {
    detail: base,
    events: base + "/events",
    tail: base + "/tail",
    buffers: base + "/buffers/",
    subagents: base + "/subagents/"
  });
}

function loadTrajectory(detailUrl, sources) {
  // A fresh navigation, so forget which turns were open: they were another run's turn
  // indices, and reapplying them here would expand arbitrary turns of this one.
  STATE.openTurns = null;
  STATE.subagentRuns = {};
  render('<div class="loading" id="trace-loading">Loading run…</div>');
  // The response is wanted only while this placeholder is still on screen. Navigating away
  // before it arrived used to draw the trace over the next view — and, for a live run, start
  // a tail poller nothing would ever stop.
  var marker = el("trace-loading");
  // Two-argument then, not then().catch(): a bug while drawing the trace must surface as an
  // error, not be reported to the reader as "could not load run".
  return api(detailUrl).then(
    function (payload) {
      if (!marker.isConnected) return;
      STATE.run = payload;
      // Drawn: a reconnect polls this page in place (below) instead of re-mounting it.
      reconnectInPlace();
      if (isFlowParent(payload)) { loadFlowTrace(payload, sources, marker); return; }
      renderTrajectory(payload, sources);
    },
    function (err) {
      if (isSuperseded(err)) return;
      if (!marker.isConnected) return;
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      render(loadErrorHtml(err));
    });
}

/* A missing run is a dead end without a way out, so both lists are linked. */
function loadErrorHtml(err) {
  var missing = err.status === 404;
  return (
    '<div class="notice err" id="trace-error"><div class="notice-title">' +
    (missing ? "No such run" : "Could not load run") + "</div>" +
    '<div class="notice-body">' + esc(err.message) + "</div>" +
    '<div class="notice-body">' +
    (missing ? "It may have been deleted, or the link came from another session store. " : "") +
    '<a href="#/runs">Back to Runs</a> · <a href="#/sessions">Sessions</a></div></div>'
  );
}

/* --- a flow parent -----------------------------------------------------------
 *
 * A flow's own session has no event log by design: each step runs in a session of its own.
 * Its trace page is therefore the flow — what was asked, how it ended (outcome, verification
 * and review as three facts), the steps in order with every attempt and a link to that step
 * session's own trace, and what the receipts measured. Everything comes from the shared
 * session read model (`/api/sessions/<id>`, the row `garuda session show --json` prints), so
 * none of the native-log statements apply: no "No session_end", no "empty log", no zero tiles,
 * no empty context-pressure or raw-events cards.
 *
 * The step pipeline is the session page's own `flowHtml` (views_sessions.js), not a second
 * rendering of the same data. */

var FLOW_REFRESH_MS = 10000;
var FLOW_REFRESH_MAX_MS = 60000;

function isFlowParent(payload) {
  return !!(payload && payload.meta && payload.meta.kind === "flow");
}

function loadFlowTrace(payload, sources, marker) {
  var sessionUrl = "/api/sessions/" + encodeURIComponent(payload.run.session_id);
  return api(sessionUrl).then(
    function (row) {
      if (!marker.isConnected) return;
      renderFlowTrace(payload, row, sources, sessionUrl);
    },
    function (err) {
      if (isSuperseded(err) || !marker.isConnected) return;
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      render(loadErrorHtml(err));
    });
}

function renderFlowTrace(payload, row, sources, sessionUrl) {
  var run = payload.run;
  STATE.raw = null;
  var region = function (id) { return '<div id="' + id + '"></div>'; };
  render(
    '<div class="trace-page trace-flow" id="trace-root">' +
    '<div class="page-head">' +
    '<a href="#/runs">← Runs</a>' +
    "<h1>" + esc(fmt.truncate(run.task, 90)) + "</h1>" +
    '<span class="trace-status" id="trace-status"></span>' +
    '<span class="meta">' + esc(run.session_id) + "</span>" +
    '<a href="#/sessions/' + encodeURIComponent(run.session_id) + '">Session page</a>' +
    '<span class="meta" id="flow-refresh-meta"></span>' +
    "</div>" +
    region("flow-progress") +
    '<div class="grid cols-4" id="trace-tiles"></div>' +
    region("flow-summary") +
    region("flow-steps") +
    region("flow-measured") +
    '<div id="starter-result-host"></div><div id="conversation-host"></div>' +
    "</div>"
  );
  var root = el("trace-root");
  root._row = row;
  root._refresh = function () { updateFlowTrace(STATE.run, root._row); };
  updateFlowTrace(payload, row, true);
  if (typeof loadConversationPanel === "function") loadConversationPanel(run.session_id);
  loadStarterResult(run.session_id);
  syncFlowRefresh(row, { detail: sources.detail, session: sessionUrl });
}

function updateFlowTrace(payload, row, first) {
  var root = el("trace-root");
  if (!root || !payload) return;
  root._row = row;
  var active = isActive(row.state);
  var ctx = { live: active };
  TRACE_DEFERRED = false;
  keepScrollPosition(function () {
    patchRegion(el("trace-status"),
      '<span class="pill ' + flowPillClass(row) + '">' + esc(row.pause ? "paused" : (row.label || "unknown")) + "</span>", first);
    patchRegion(el("flow-progress"), active ? flowProgressHtml(row) : "", first);
    patchRegion(el("trace-tiles"), flowTilesHtml(payload.run, row, ctx), first);
    patchRegion(el("flow-summary"), flowSummaryHtml(payload.run, row, ctx), first);
    patchRegion(el("flow-steps"), flowHtml(row.flow, row.flow_progress), first);
    patchRegion(el("flow-measured"), flowMeasuredHtml(row.flow), first);
  });
  if (TRACE_DEFERRED) retryWhenSelectionClears(root);
}

function flowPillClass(row) {
  if (isActive(row.state)) return "running";
  if (row.pause) return "paused";
  var outcome = (row.state || {}).outcome;
  if (outcome === "completed") return "success";
  if (outcome === "failed" || outcome === "cancelled" || outcome === "refused" || row.crashed) {
    return "failed";
  }
  return "unknown";
}

/* A running flow: the step in flight, as the read model exposes it (`flow_progress`, from the
 * flow's journal), and nothing guessed when no step is in flight. */
function flowProgressHtml(row) {
  var progress = row.flow_progress;
  var line = progress
    ? "Step " + progress.index + " of " + progress.total + " in progress: " + progress.step +
      " (attempt " + progress.attempt + ")."
    : "Running; no step is in flight right now (between steps, or checking the result).";
  return '<div class="card live-card" id="flow-in-progress"><h2>In progress</h2>' +
    '<p id="flow-progress-line">' + esc(line) + "</p>" + flowInFlightHtml(row) + "</div>";
}

/* What the step in flight is doing right now. Empty today: a richer live view arrives as a
 * shared read-model field (`row.flow.in_flight`) from the flow runtime, and this is where it
 * renders. Deliberately no reader of the progress file of its own here. */
function flowInFlightHtml(row) {
  void row;
  return "";
}

function flowTilesHtml(run, row, ctx) {
  var flow = row.flow || {};
  var steps = flow.steps || [];
  var done = steps.filter(function (s) {
    var attempts = s.attempts || [];
    return attempts.length && attempts[attempts.length - 1].status === "done";
  }).length;
  var attempts = steps.reduce(function (n, s) { return n + (s.attempts || []).length; }, 0);
  var review = flow.review;
  var took = runDuration(run, ctx);
  var total = flow.measure || {};
  var cost = typeof total.reported_cost_usd === "number" ? total.reported_cost_usd : null;
  return [
    tile("Steps", done + " of " + steps.length, "steps done"),
    tile("Attempts", String(attempts),
         review && review.rounds ? "review rounds: " + review.rounds : "no review rounds recorded"),
    tile("Duration", took.value, took.note),
    tile("Reported cost", cost === null ? "not recorded" : fmt.cost(cost),
         cost === null ? "no step reported a cost"
           : "adapter-reported, not metered by Garuda" + (total.cost_complete ? "" : " · incomplete"))
  ].join("");
}

/* Outcome, verification and review as three separate facts, then how it stopped (one plain
 * sentence from the recorded flow state and stop code), the task and the final message. */
function flowSummaryHtml(run, row, ctx) {
  var state = row.state || {};
  var flow = row.flow || {};
  var review = flow.review;
  var took = runDuration(run, ctx);
  var stopped = !ctx.live && state.outcome !== "completed";
  var sentence = stopped && typeof failureSentence === "function" ? failureSentence(row) : null;
  return (
    '<div class="card" id="flow-summary-card"><h2>' + (ctx.live ? "So far" : "How it ended") + "</h2>" +
    (sentence ? '<div class="notice ' + (row.pause ? "paused" : "err") + '" id="flow-stopped"><div class="notice-title">' +
      esc(sentence.cause) + '</div><div class="notice-body">' + esc(sentence.next) + "</div></div>" : "") +
    '<dl class="trace-facts">' +
    "<dt>Outcome</dt><dd>" + chip("outcome", ctx.live ? "in progress" : (state.outcome || "unknown"),
                                  row.pause ? "chip-pause" : outcomeClass(state.outcome)) +
    (flow.state ? " " + chip("flow", flow.state, "chip-off") : "") + "</dd>" +
    "<dt>Verification</dt><dd>" + verificationChip(row.verification) +
    ' <span class="stat-sub">whether the result passed its checks</span></dd>' +
    "<dt>Review</dt><dd>" + (review
      ? chip("review", reviewLabel(review), "chip-off") + reviewCoverageHtml(flow.review_coverage) +
        ' <span class="stat-sub">a review is advice, not verification</span>'
      : '<span class="stat-sub">no review recorded</span>') + "</dd>" +
    // The Runs list's own figure: a flow ends at the later of its last record and its last
    // step receipt.
    "<dt>Duration</dt><dd>" + esc(took.value) + ' <span class="stat-sub">' + esc(took.note) +
    "</span></dd>" +
    "</dl>" +
    '<div class="block block-input"><div class="block-label">task</div><pre>' +
    esc(run.task || "") + "</pre></div>" +
    '<div class="block block-output" id="flow-final"><div class="block-label">final message</div>' +
    (row.final_message ? "<pre>" + esc(row.final_message) + "</pre>"
      : '<div class="block-note">' + (ctx.live ? "None yet — the flow is still running."
        : "No final message was stored.") + "</div>") + "</div>" +
    '<p class="stat-sub">A flow has no event log of its own: each step ran in its own session. ' +
    "Open a step's trace below for its turns.</p></div>"
  );
}

/* --- what the receipts measured (#832) ------------------------------------------------ */

function groupedNumber(value) {
  return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

var MEASURE_LABELS = {
  prompt_chars: "Prompt", turns: "Turns", tool_calls: "Tool calls", duration_ms: "Wall time",
  peak_context_used: "Peak context", cost_usd: "Reported cost"
};

function measuredCell(key, raw, text) {
  var known = raw !== null && raw !== undefined;
  return '<td class="num' + (known ? "" : " not-recorded") + '" data-k="' + esc(key) + '"' +
    ' data-th="' + esc(MEASURE_LABELS[key] || key) + '"' +
    (known ? ' data-v="' + esc(raw) + '"' : "") + ">" + esc(known ? text : "not recorded") + "</td>";
}

function countCell(key, value) {
  return measuredCell(key, typeof value === "number" ? value : null,
                      typeof value === "number" ? groupedNumber(value) : "");
}

function peakCell(usage) {
  var peak = usage && typeof usage.peak_context_used === "number" ? usage.peak_context_used : null;
  var size = usage && typeof usage.context_size === "number" ? usage.context_size : null;
  return measuredCell("peak_context_used", peak, peak === null ? "" :
    groupedNumber(peak) + " of " + (size === null ? "unknown" : groupedNumber(size)));
}

function costCell(cost, complete) {
  var known = typeof cost === "number";
  return measuredCell("cost_usd", known ? cost : null,
    known ? "$" + cost.toFixed(4) + (complete === false ? " (incomplete)" : "") : "");
}

function promptCell(prompt) {
  var chars = prompt && typeof prompt.chars === "number" ? prompt.chars : null;
  return measuredCell("prompt_chars", chars, chars === null ? "" :
    "~" + groupedNumber(prompt.est_tokens) + " tok · " + groupedNumber(chars) + " chars");
}

function flowMeasuredHtml(flow) {
  if (!flow) return "";
  var rows = [];
  (flow.steps || []).forEach(function (step) {
    (step.attempts || []).forEach(function (attempt) {
      var m = attempt.measure;
      var usage = m && m.usage;
      rows.push(
        '<tr class="measure-row" data-step="' + esc(step.id) + '" data-attempt="' + esc(attempt.attempt) +
        '"' + (m ? "" : ' data-measured="no"') + '><td class="measure-name">' + esc(step.id) + " #" +
        esc(attempt.attempt) + "</td>" +
        (m
          ? promptCell(m.prompt) + countCell("turns", m.turns) + countCell("tool_calls", m.tool_calls) +
            measuredCell("duration_ms", typeof m.duration_ms === "number" ? m.duration_ms : null,
                         typeof m.duration_ms === "number" ? fmt.duration(m.duration_ms) : "") +
            peakCell(usage) +
            costCell(usage ? usage.cost_usd : null, usage ? usage.cost_complete : null)
          : '<td class="not-recorded" colspan="6">not recorded (this receipt predates measuring)</td>') +
        "</tr>");
    });
  });
  if (!rows.length) return "";
  var total = flow.measure || {};
  var measured = total.measured_attempts || 0;
  var walls = [];
  (flow.steps || []).forEach(function (s) {
    (s.attempts || []).forEach(function (a) { if (a.measure) walls.push(a.measure.duration_ms); });
  });
  var wallSum = walls.length && walls.every(function (w) { return typeof w === "number"; })
    ? walls.reduce(function (a, b) { return a + b; }, 0) : null;
  var totalRow =
    '<tr class="measure-total"><th scope="row" class="measure-name">Flow total · ' + esc(measured) + " of " +
    esc(total.attempts) + " attempts measured</th>" +
    (measured
      ? measuredCell("prompt_chars", total.prompt_chars, "~" + groupedNumber(total.prompt_est_tokens) +
                     " tok · " + groupedNumber(total.prompt_chars) + " chars") +
        countCell("turns", total.turns) + countCell("tool_calls", total.tool_calls) +
        measuredCell("duration_ms", wallSum, wallSum === null ? "" : fmt.duration(wallSum)) +
        measuredCell("peak_context_used", total.peak_context_used,
                     typeof total.peak_context_used === "number" ? groupedNumber(total.peak_context_used) +
                     " (largest)" : "") +
        costCell(total.reported_cost_usd, total.cost_complete)
      : '<td class="not-recorded" colspan="6">not recorded</td>') + "</tr>";
  return (
    '<div class="card" id="flow-measured-card"><h2>Measured</h2>' +
    '<div class="table-wrap"><table class="table table-stack" id="flow-measure"><thead><tr><th>Attempt</th>' +
    '<th class="num">Prompt</th><th class="num">Turns</th><th class="num">Tool calls</th>' +
    '<th class="num">Wall time</th><th class="num">Peak context</th><th class="num">Reported cost</th>' +
    "</tr></thead><tbody>" + rows.join("") + "</tbody><tfoot>" + totalRow + "</tfoot></table></div>" +
    '<p class="stat-sub" id="flow-measure-note">From each attempt\'s receipt. Prompt tokens are an ' +
    "estimate (characters ÷ 4), not a tokenizer count. Peak context is how full the harness " +
    "reported its context window, which is occupancy, not billable tokens. Cost is what the " +
    "adapter reported; Garuda did not meter or price it. Anything a receipt did not record reads " +
    "\"not recorded\", never 0.</p></div>"
  );
}

/* --- a running flow refreshes ---------------------------------------------------------
 * The run detail (its duration once it ends) and the session row, every few seconds while the
 * flow is active; patched in place like the native trace, so nothing the reader is doing is
 * reset. Stops when the flow is no longer active, and by navigation (router: stopLive). */
function syncFlowRefresh(row, urls) {
  if (!isActive(row.state)) { stopLive(); return; }
  stopLive();
  var state = { urls: urls, failures: 0, flow: true };
  STATE.live = state;
  STATE.poller = new Poller(function () { return flowPoll(state); }, FLOW_REFRESH_MS);
  // The page was just drawn from a fresh read: the first refresh waits one interval.
  STATE.poller.timer = setTimeout(function () { if (STATE.live === state) STATE.poller.start(); },
                                  FLOW_REFRESH_MS);
}

function flowPoll(state) {
  // While the dashboard is unreachable the app's one probe asks (core.js); onReconnect polls.
  if (CONNECTION.down) return Promise.resolve();
  return Promise.all([api(state.urls.detail), api(state.urls.session)])
    .catch(tagFetchError)
    .then(function (both) {
      if (STATE.live !== state) return;
      state.failures = 0;
      STATE.poller.interval = FLOW_REFRESH_MS;
      setText("flow-refresh-meta", "");
      STATE.run = both[0];
      updateFlowTrace(both[0], both[1]);
      if (!isActive(both[1].state)) {
        stopLive();
        if (typeof loadConversationPanel === "function") loadConversationPanel(both[0].run.session_id);
        toast("Flow finished — " + (both[1].label || "done"));
      }
    })
    .catch(function (err) {
      if (isSuperseded(err) || STATE.live !== state) return;
      if (!err || !err.fromApi) { surface(err); return; }
      if (err.status === 401) { stopLive(); render(tokenRequiredPanel()); return; }
      state.failures += 1;
      STATE.poller.interval = Math.min(FLOW_REFRESH_MAX_MS,
                                       FLOW_REFRESH_MS * Math.pow(2, state.failures));
      setText("flow-refresh-meta", err.status
        ? "the dashboard answered " + err.status + " — retrying"
        : "can't reach the dashboard — retrying");
    });
}

/* --- mounting and updating -------------------------------------------------
 *
 * The page is a fixed shell of regions, drawn once per navigation. A live run's structure
 * refresh then *patches* the regions whose HTML changed and leaves the rest alone — the old
 * full re-render reset everything a reader had done every three seconds: a loaded raw-events
 * window, its filter and the focus in it, an opened subagent trace, the Models used panel,
 * the scroll position and any text selection.
 *
 * The rules each patch follows:
 *   - a region (or turn card) whose HTML is unchanged is not touched at all;
 *   - one that holds the reader's text selection is left as it is until the selection is
 *     gone, so copying from a live trace never loses what was selected;
 *   - focus inside a replaced region moves to the equivalent control;
 *   - the element at the top of the viewport stays where it was (manual scroll anchoring;
 *     the CSS turns the browser's own off so the two cannot both adjust).
 * Regions with state of their own — the raw events pane, the starter result, the Models
 * used panel — are outside the patched set entirely. */

function renderTrajectory(payload, sources) {
  var run = payload.run;
  var external = externalOnly(payload);

  // Set before rendering, not after: `outputHtml` asks whether buffers exist while building
  // the step bodies, and reading it later would answer with the previous view's source.
  STATE.raw = {
    url: sources.events,
    buffers: sources.buffers || null,
    subagents: sources.subagents || null,
    total: payload.event_count, start: 0, count: 0, events: [], loaded: false
  };

  var region = function (id) { return '<div id="' + id + '"></div>'; };
  var hosts = '<div id="starter-result-host"></div><div id="conversation-host"></div>';
  render(
    '<div class="trace-page' + (external ? " trace-external" : "") + '" id="trace-root">' +
    '<div class="page-head">' +
    '<a href="#/runs">← Runs</a>' +
    "<h1>" + esc(fmt.truncate(run.task, 90)) + "</h1>" +
    '<span class="trace-status" id="trace-status"></span>' +
    '<span class="meta">' + esc(run.session_id) + "</span>" +
    "</div>" +
    region("trace-live") +
    '<div class="grid cols-4" id="trace-tiles"></div>' +
    region("trace-flags") +
    // An external harness's run leads with what Garuda did record about it.
    (external
      ? region("trace-external") + hosts
      : hosts + region("trace-external") + region("trace-overview") +
        region("trace-pressure") + region("trace-gates") + timelineHeadHtml() +
        '<div class="trajectory" id="traj"></div>') +
    // An external run with no native log has no raw events to check anything against.
    (external && !payload.event_count ? "" : rawEventsHtml(payload)) +
    "</div>"
  );

  var root = el("trace-root");
  root._external = external;
  root._sources = sources;
  root._refresh = function () { if (STATE.run) updateTrajectory(STATE.run, sources); };
  root.addEventListener("click", onTraceClick);
  var traj = el("traj");
  if (traj) traj.addEventListener("click", onTurnClick);
  var filter = el("raw-filter");
  if (filter) filter.addEventListener("input", function () { renderRawEvents(); });

  updateTrajectory(payload, sources, true);
  if (typeof loadConversationPanel === "function") loadConversationPanel(run.session_id);
  loadStarterResult(run.session_id);
  restoreOpenTurns(payload.trajectory.turns || []);
  syncLive(payload, sources);
}

/* Fill or patch every derived region from `payload`. `first` is the mount's own fill. */
function updateTrajectory(payload, sources, first) {
  var root = el("trace-root");
  if (!root) return;
  if (root._external !== externalOnly(payload)) {
    // The run changed shape (a native lane appeared under an external one, or the reverse):
    // a different set of regions, so mount again rather than patch.
    renderTrajectory(payload, sources);
    return;
  }
  var traj = payload.trajectory;
  var turns = traj.turns || [];
  var ctx = traceContext(payload);
  var hasGates = turns.some(function (t) { return (t.gates || []).length; }) ||
                 (traj.phases || []).some(function (p) { return p.critic; });
  if (STATE.raw) STATE.raw.total = payload.event_count;

  TRACE_DEFERRED = false;
  keepScrollPosition(function () {
    patchRegion(el("trace-status"), statusPill(payload.run.status) +
      (ctx.live ? '<span class="pill live" id="live-badge">live</span>' : ""), first);
    patchRegion(el("trace-live"), ctx.live && sources.tail ? liveStreamHtml() : "", first);
    patchRegion(el("trace-tiles"), detailTilesHtml(payload, ctx), first);
    patchRegion(el("trace-flags"), runFlagsHtml(payload, ctx), first);
    patchRegion(el("trace-external"), root._external
      ? externalSummaryHtml(payload, ctx) : externalTrailHtml(payload.external), first);
    if (!root._external) {
      patchRegion(el("trace-overview"), overviewHtml(traj), first);
      patchRegion(el("trace-pressure"), '<div class="card">' + pressureChartHtml(payload) +
                  "</div>", first);
      patchRegion(el("trace-gates"), hasGates || traj.gate_stack
        ? '<div class="card">' + gateStackHtml(traj) + "</div>" : "", first);
      setText("trace-turn-count", turns.length + (turns.length === 1 ? " turn" : " turns"));
      patchTimeline(el("traj"), timelinePieces(traj, hasGates, ctx), hasGates, first);
    }
    setText("raw-total", String(payload.event_count));
  });
  if (TRACE_DEFERRED) retryWhenSelectionClears(root);
}

/* `live`, not `status === "running"`: the status is a permanent lie for a killed run, and
 * trusting it started a tail poller on month-old sessions that never stopped. */
function isRunLive(payload) {
  return !!(payload.run && payload.run.live);
}

/* What every renderer below needs to know about the run as a whole. `lastIndex` is the turn
 * that may still be in progress while the run is live. */
function traceContext(payload) {
  var turns = payload.trajectory.turns || [];
  return {
    live: isRunLive(payload),
    legacy: isLegacyLog(payload.trajectory),
    lastIndex: turns.length ? turns[turns.length - 1].index : null
  };
}

/* An unfinished turn is "in progress" only while the run is live and only if it is the last
 * one; anywhere else, no turn_metrics means it stopped there. */
function turnInProgress(turn, ctx) {
  return !!(ctx && ctx.live && !turn.finished && turn.index === ctx.lastIndex);
}

/* --- patching primitives --------------------------------------------------- */

/* Set when a patch was held back because the reader had text selected inside it. */
var TRACE_DEFERRED = false;

function setText(id, text) {
  var node = el(id);
  if (node && node.textContent !== text) node.textContent = text;
}

function holdsSelection(node) {
  var selection = window.getSelection ? window.getSelection() : null;
  if (!node || !selection || selection.isCollapsed || !selection.rangeCount) return false;
  return selection.containsNode(node, true);
}

/* A selector for the focused control inside `node`, so the same control can take focus back
 * after its region is redrawn. */
function focusKey(node) {
  var active = document.activeElement;
  if (!active || active === document.body || !node.contains(active)) return null;
  if (active.id) return "#" + CSS.escape(active.id);
  var attrs = ["data-toggle", "data-scroll", "data-subagent", "data-buffer", "data-jump",
               "data-page", "data-search"];
  for (var i = 0; i < attrs.length; i++) {
    if (active.hasAttribute(attrs[i])) {
      return "[" + attrs[i] + '="' + CSS.escape(active.getAttribute(attrs[i])) + '"]';
    }
  }
  return null;
}

function refocus(node, key) {
  if (!key) return;
  var target = node.querySelector(key);
  if (target) target.focus({ preventScroll: true });
}

/* Replace `host`'s content with `html` when it differs. `force` is the first fill. */
function patchRegion(host, html, force) {
  if (!host || host._html === html) return;
  if (!force && holdsSelection(host)) { TRACE_DEFERRED = true; return; }
  var key = focusKey(host);
  host.innerHTML = html;
  host._html = html;
  refocus(host, key);
}

function nodeFromHtml(html) {
  var template = document.createElement("template");
  template.innerHTML = html;
  return template.content.firstElementChild;
}

/* Keyed reconciliation of the timeline grid: one element per piece (phase header, turn card,
 * gate cell, critic strip). A turn whose HTML is unchanged keeps its node — and with it the
 * open state, a loaded subagent trace, the focus and any selection. A changed turn is
 * replaced with its open state and loaded subagent traces carried over. */
function patchTimeline(traj, pieces, hasGates, first) {
  if (!traj) return;
  traj.classList.toggle("with-lane", !!hasGates);
  if (first) {
    // The fast path for the mount: one innerHTML for the whole grid (a 1,500-turn trace is
    // one string), then each child is tagged with the piece it came from.
    traj.innerHTML = pieces.map(function (p) { return p.html; }).join("");
    var byKey = {};
    var child = traj.firstElementChild;
    for (var i = 0; i < pieces.length && child; i++, child = child.nextElementSibling) {
      child._key = pieces[i].key;
      child._html = pieces[i].html;
      byKey[pieces[i].key] = child;
    }
    traj._byKey = byKey;
    return;
  }
  var existing = traj._byKey || {};
  var next = {};
  for (var j = 0; j < pieces.length; j++) {
    var piece = pieces[j];
    var node = existing[piece.key];
    if (node && node._html !== piece.html) {
      if (holdsSelection(node)) {
        TRACE_DEFERRED = true;
      } else {
        var fresh = nodeFromHtml(piece.html);
        var key = focusKey(node);
        carryTurnState(node, fresh);
        node.replaceWith(fresh);
        fresh._key = piece.key;
        fresh._html = piece.html;
        refocus(fresh, key);
        node = fresh;
      }
    } else if (!node) {
      node = nodeFromHtml(piece.html);
      node._key = piece.key;
      node._html = piece.html;
    }
    if (traj.children[j] !== node) traj.insertBefore(node, traj.children[j] || null);
    next[piece.key] = node;
    delete existing[piece.key];
  }
  Object.keys(existing).forEach(function (key) {
    if (existing[key].parentNode === traj) traj.removeChild(existing[key]);
  });
  traj._byKey = next;
}

function carryTurnState(old, fresh) {
  if (!fresh || !old.classList.contains("turn")) return;
  if (old.classList.contains("open")) {
    fresh.classList.add("open");
    var body = fresh.querySelector(".turn-body");
    if (body) body.hidden = false;
  }
  var loaded = old.querySelectorAll(".sub-body");
  for (var i = 0; i < loaded.length; i++) {
    if (!loaded[i].firstChild) continue;
    var targets = fresh.querySelectorAll(".sub-body");
    for (var k = 0; k < targets.length; k++) {
      if (targets[k].id !== loaded[i].id) continue;
      while (loaded[i].firstChild) targets[k].appendChild(loaded[i].firstChild);
      break;
    }
  }
}

/* Keep the element at the top of the viewport where it is while `fn` changes the page. */
function keepScrollPosition(fn) {
  var anchor = window.scrollY > 0 ? scrollAnchor() : null;
  fn();
  if (!anchor) return;
  var byKey = (el("traj") || {})._byKey || {};
  var node = anchor.key !== null ? byKey[anchor.key] : anchor.node;
  if (!node || !node.isConnected) return;
  var delta = node.getBoundingClientRect().top - anchor.top;
  if (Math.abs(delta) >= 1) window.scrollBy(0, delta);
}

function scrollAnchor() {
  var root = el("trace-root");
  if (!root) return null;
  for (var child = root.firstElementChild; child; child = child.nextElementSibling) {
    var box = child.getBoundingClientRect();
    if (box.bottom <= 0 || box.height === 0) continue;
    if (child.id === "traj") {
      for (var row = child.firstElementChild; row; row = row.nextElementSibling) {
        var rowBox = row.getBoundingClientRect();
        if (rowBox.bottom > 0) return { key: row._key, node: null, top: rowBox.top };
      }
    }
    return { key: null, node: child, top: box.top };
  }
  return null;
}

/* A patch held back for a selection is applied once the selection is gone — even after the
 * run finished and nothing else would ever refresh it. One listener at a time, and it removes
 * itself once the view it belongs to is gone. */
function retryWhenSelectionClears(root) {
  if (root._selectionRetry) return;
  var onChange = function () {
    if (!root.isConnected) {
      document.removeEventListener("selectionchange", onChange);
      return;
    }
    var selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    document.removeEventListener("selectionchange", onChange);
    root._selectionRetry = null;
    if (root._refresh) root._refresh();
  };
  root._selectionRetry = onChange;
  document.addEventListener("selectionchange", onChange);
}

/* One click handler for the page's controls outside the timeline. Delegated, so a region
 * that is redrawn keeps working without re-wiring anything. */
function onTraceClick(event) {
  var target = event.target;
  if (target.closest("#expand-all")) { setAllTurns(true); return; }
  if (target.closest("#collapse-all")) { setAllTurns(false); return; }
  if (target.closest("#raw-load")) { loadRawEvents(STATE.raw ? STATE.raw.start : 0); return; }
  var page = target.closest("#raw-body [data-page]");
  if (page) { loadRawEvents(Number(page.getAttribute("data-page"))); return; }
  var search = target.closest("#raw-body [data-search]");
  if (search) { searchRawEvents(search.getAttribute("data-search") === "earlier" ? -1 : 1); return; }
  var row = target.closest("#trace-overview [data-scroll]");
  if (row) {
    event.preventDefault();
    var index = row.getAttribute("data-scroll");
    setTurnOpen(index, true);
    var card = document.querySelector('.turn[data-turn="' + CSS.escape(index) + '"]');
    if (card) card.scrollIntoView({ block: "center" });
  }
}

/* --- the overview strip ---------------------------------------------------
 *
 * One row per turn, one chip per tool, coloured by family. The point is to see the shape of
 * a run — read read read, then a burst of edits, then a subagent, then the gate — without
 * reading a word of it. Clicking a row scrolls to that turn. */

function overviewHtml(traj) {
  var turns = traj.turns || [];
  if (turns.length < 2) return "";
  var rows = turns.map(function (turn) {
    var steps = turnSteps(turn);
    var chips = steps.map(function (step) {
      return '<span class="fchip f-' + toolFamily(step.name) +
             (step.is_error || step.status === "error" ? " fchip-err" : "") +
             '" title="' + esc(step.name + " · " + step.status) + '">' +
             esc(FAMILY_ICON[toolFamily(step.name)]) + "</span>";
    }).join("");
    return (
      '<a class="flow-row" href="#turn-' + esc(turn.index) + '" data-scroll="' + esc(turn.index) + '">' +
      '<span class="flow-no">' + esc(turnNumber(turn)) + "</span>" +
      '<span class="flow-chips">' + (chips || '<span class="fchip f-none">·</span>') + "</span>" +
      '<span class="flow-note">' + esc(flowNote(turn, steps)) + "</span></a>"
    );
  }).join("");
  return (
    '<div class="card"><h2>How the run went</h2>' +
    '<div class="stat-sub">One row per turn, one mark per tool call. ' +
    familyLegendHtml() + "</div>" +
    '<div class="flow" id="flow">' + rows + "</div></div>"
  );
}

function familyLegendHtml() {
  return ["read", "write", "exec", "net", "subagent", "gate", "plan"].map(function (family) {
    return '<span class="flegend"><span class="fchip f-' + family + '">' +
           esc(FAMILY_ICON[family]) + "</span>" + esc(family) + "</span>";
  }).join("");
}

function flowNote(turn, steps) {
  if (!steps.length && !turn.model_calls.length) return "nothing recorded";
  var names = {};
  steps.forEach(function (s) { names[s.name] = (names[s.name] || 0) + 1; });
  var parts = Object.keys(names).map(function (name) {
    return names[name] > 1 ? name + "×" + names[name] : name;
  });
  if (!parts.length) parts.push("no tools — text only");
  var errors = turnErrors(turn);
  if (errors) parts.push(errors + " errored");
  if ((turn.subagents || []).length) parts.push((turn.subagents || []).length + " subagent");
  return parts.join(", ");
}

function turnSteps(turn) {
  return (turn.model_calls || []).reduce(function (list, call) {
    return list.concat(call.tool_steps || []);
  }, []);
}

function turnNumber(turn) {
  return turn.number === null || turn.number === undefined ? "?" : turn.number;
}

/* How many tool calls in this turn failed.
 *
 * The larger of two counts, deliberately. `turn.tool_errors` is the harness's own figure from
 * `turn_metrics` — authoritative, and absent on a log with no turn structure, where it reads 0
 * while the steps plainly carry errors. Counting the steps covers that vintage; taking the max
 * keeps the harness's number when it is higher, which happens when a failure never became a
 * step at all. Reporting "none errored" over a column of red marks was the visible symptom. */
function turnErrors(turn) {
  var counted = turnSteps(turn).filter(function (step) {
    return step.is_error || step.status === "error";
  }).length;
  return Math.max(turn.tool_errors || 0, counted);
}

/* --- live tail ------------------------------------------------------------
 *
 * Polling, not SSE: `EventSource` cannot set a request header, and the token only travels in
 * `X-Garuda-Token`. One `Poller` (a setTimeout chain, never setInterval) carries events and
 * status in a single response, so nothing arrives out of order.
 *
 * The cursor starts at `run.events_bytes` — the file's size when the structure was read —
 * not at 0. Everything before that offset is already in the timeline and everything after is
 * new, so there is neither a gap nor a re-transfer of a long log. */

var LIVE_INTERVAL_MS = 750;
var LIVE_MAX_INTERVAL_MS = 3000;
/* A mid-burst re-poll floor. NOT zero: a torn-tail read reports `eof: false`, and setting the
 * interval to 0 there made the backoff compute Math.min(max, 0 * 1.5) === 0 on every later
 * poll — permanently. One poll landing mid-write turned into a hot request loop for the rest
 * of the session. Every interval below is derived from the base rather than from the current
 * value, so it cannot latch. */
var LIVE_BURST_MS = 120;
var LIVE_BACKOFF = 1.5;
var LIVE_EMPTY_BEFORE_BACKOFF = 10;
var LIVE_STRUCTURE_MS = 3000;
var LIVE_MAX_ROWS = 60;
/* When the dashboard cannot be reached: double the wait per failed poll, up to this. The
 * old loop retried every 750 ms forever and printed a new error each time. */
var LIVE_OFFLINE_MAX_MS = 30000;

function liveStreamHtml() {
  return (
    '<div class="card live-card">' +
    '<h2>Live <span class="meta" id="live-meta">connecting…</span></h2>' +
    '<div id="live-body" class="live-body"><div class="stat-sub">Waiting for the next event…' +
    "</div></div></div>"
  );
}

/* The invariant: a poller exists exactly when the rendered view is live. Called after every
 * fill, so a run that finished between two polls stops being polled as soon as it is drawn as
 * finished — and a refresh of a still-live run keeps the poller it has rather than starting
 * a second one. */
function syncLive(payload, sources) {
  if (!(isRunLive(payload) && sources.tail)) { stopLive(); return; }
  if (STATE.live && STATE.live.sources === sources && STATE.poller) {
    STATE.live.eventCount = payload.event_count;
    return;
  }
  startLive(sources, payload);
}

function startLive(sources, payload) {
  // Stop whatever was polling first: two pollers on one view would each believe they own it.
  var previous = STATE.live;
  stopLive();
  var state = {
    sources: sources,
    offset: payload.run && typeof payload.run.events_bytes === "number"
      ? payload.run.events_bytes : 0,
    // Carried across a remount: a stream pane that empties is worse than no stream pane.
    rows: previous ? previous.rows : [],
    empty: 0,
    failures: 0,
    eventCount: payload.event_count,
    sinceStructure: 0
  };
  STATE.live = state;
  STATE.poller = new Poller(function () { return livePoll(state); }, LIVE_INTERVAL_MS);
  STATE.poller.start();
  if (state.rows.length) renderLiveRows(state);
}

// Back from an outage: a live trace (native tail or a running flow) polls at once.
onReconnect(function () {
  if (STATE.live && STATE.poller) STATE.poller.now();
});

/* Mark a rejection as the network's (or the server's), as opposed to a bug in the code that
 * handles the response — only the first kind is "can't reach the dashboard". */
function tagFetchError(err) {
  if (err && typeof err === "object") err.fromApi = true;
  throw err;
}

/* A bug must not be swallowed by the Poller (which ignores rejections by design); rethrown
 * outside the promise chain, it reaches the console and the browser checks. */
function surface(err) {
  setTimeout(function () { throw err; }, 0);
}

function livePoll(state) {
  if (CONNECTION.down) return Promise.resolve();  // the app's probe asks; onReconnect polls
  var join = state.sources.tail.indexOf("?") === -1 ? "?" : "&";
  return api(state.sources.tail + join + "offset=" + state.offset)
    .catch(tagFetchError)
    .then(function (payload) {
      if (STATE.live !== state) return;   // the view changed under us
      state.failures = 0;
      if (payload.truncated) {
        state.offset = 0;
        state.rows = [];
        setLiveMeta("log was replaced — restarting from the beginning");
        return;
      }
      state.offset = payload.offset;

      if (payload.events.length) {
        state.empty = 0;
        state.rows = state.rows.concat(payload.events).slice(-LIVE_MAX_ROWS);
        renderLiveRows(state);
      } else {
        state.empty += 1;
      }
      // Derived from the base every time, never from the previous interval, so a single fast
      // poll cannot pin the rate low for the rest of the session.
      var quiet = Math.max(0, state.empty - LIVE_EMPTY_BEFORE_BACKOFF);
      STATE.poller.interval = payload.eof
        ? Math.min(LIVE_MAX_INTERVAL_MS, LIVE_INTERVAL_MS * Math.pow(LIVE_BACKOFF, quiet))
        : LIVE_BURST_MS;

      state.sinceStructure += STATE.poller.interval;
      var finished = !payload.running && payload.eof;
      if (finished || state.sinceStructure >= LIVE_STRUCTURE_MS) {
        state.sinceStructure = 0;
        return refreshStructure(state, finished);
      }
      setLiveMeta(liveMetaText(state, payload));
    })
    .catch(function (err) {
      if (isSuperseded(err)) return;
      if (STATE.live !== state) return;
      if (!err || !err.fromApi) { surface(err); return; }
      if (err.status === 401) { stopLive(); render(tokenRequiredPanel()); return; }
      liveOffline(state, err);
    });
}

/* One calm line, the same one on every failed poll, and a wait that doubles each time. The
 * next successful poll puts the normal status back by itself. */
function liveOffline(state, err) {
  state.failures += 1;
  if (STATE.poller) {
    STATE.poller.interval = Math.min(LIVE_OFFLINE_MAX_MS,
                                     LIVE_INTERVAL_MS * Math.pow(2, state.failures));
  }
  setLiveMeta(err.status
    ? "the dashboard answered " + err.status + " — retrying"
    : "can't reach the dashboard — retrying");
}

function liveMetaText(state, payload) {
  var behind = payload.size - state.offset;
  return state.rows.length + " events streamed · " +
         (behind > 0 ? fmt.tokens(behind) + " bytes pending" : "up to date") +
         " · polling every " +
         Math.round((STATE.poller.interval || LIVE_INTERVAL_MS) / 100) / 10 + "s";
}

function setLiveMeta(text) {
  setText("live-meta", text);
}

/* Newest first. Held back while the reader has text selected in the pane, and a pane the
 * reader scrolled into keeps the rows they were looking at in place as new ones land on top. */
function renderLiveRows(state) {
  var node = el("live-body");
  if (!node || holdsSelection(node)) return;
  var top = node.scrollTop;
  var height = node.scrollHeight;
  node.innerHTML = state.rows.slice().reverse().map(function (event) {
    var payload = event.payload === undefined ? event : event.payload;
    return '<div class="raw-row"><span class="raw-idx">' +
           esc(String(event.timestamp || "").slice(11, 19)) + "</span>" +
           '<span class="raw-type">' + esc(event.type) + "</span>" +
           '<span class="raw-payload mono">' +
           esc(fmt.truncate(JSON.stringify(payload), 220)) + "</span></div>";
  }).join("");
  if (top > 0) node.scrollTop = top + (node.scrollHeight - height);
}

/* Refetch the turn structure and patch the page with it. Turn grouping stays server-side in
 * exactly one place; what changed on screen is decided by comparing HTML, region by region
 * and turn by turn (see `updateTrajectory`). */
function refreshStructure(state, finished) {
  return api(state.sources.detail)
    .catch(tagFetchError)
    .then(function (payload) {
      if (STATE.live !== state) return;
      if (!finished && payload.event_count === state.eventCount) {
        setLiveMeta(state.rows.length + " events streamed · structure unchanged");
        return;
      }
      state.eventCount = payload.event_count;
      STATE.run = payload;
      updateTrajectory(payload, state.sources);
      syncLive(payload, state.sources);
      if (STATE.live !== state) {
        // It stopped being live: the Models used panel is loaded once per view, so it is
        // brought up to date now, unless the reader is selecting text in it.
        var host = el("conversation-host");
        if (host && !holdsSelection(host) && typeof loadConversationPanel === "function") {
          loadConversationPanel(payload.run.session_id);
        }
        if (finished) toast("Run finished — " + (payload.run.status || "done"));
      }
    });
}

function stopLive() {
  if (STATE.poller) { STATE.poller.stop(); STATE.poller = null; }
  STATE.live = null;
}

/* --- expansion state ---------------------------------------------------------
 *
 * Turn cards survive a live refresh (see `patchTimeline`), so their open state needs no
 * bookkeeping between refreshes; this is only the first paint. */

/* First render of a short run opens every turn: a trace you have to click fifteen times to
 * read is not a trace. Long runs stay closed, with the overview strip above carrying the
 * shape and "Expand all" one click away. */
function restoreOpenTurns(turns) {
  var open = STATE.openTurns;
  if (!open) {
    if (turns.length && turns.length <= AUTO_EXPAND_LIMIT) setAllTurns(true);
    return;
  }
  Object.keys(open).forEach(function (index) { setTurnOpen(index, true); });
}

function setTurnOpen(index, on) {
  var body = el("turn-body-" + index);
  if (!body) return;
  body.hidden = !on;
  body.closest(".turn").classList.toggle("open", on);
}

function setAllTurns(on) {
  var nodes = document.querySelectorAll(".turn");
  for (var i = 0; i < nodes.length; i++) {
    setTurnOpen(nodes[i].getAttribute("data-turn"), on);
  }
}

/* Static: only the count changes, so the buttons keep focus across a live refresh. */
function timelineHeadHtml() {
  return (
    '<div class="page-head sub-head"><h2>Trace</h2>' +
    '<span class="meta" id="trace-turn-count"></span>' +
    '<button type="button" class="btn" id="expand-all">Expand all</button>' +
    '<button type="button" class="btn" id="collapse-all">Collapse all</button></div>'
  );
}

/* --- tiles and run-level flags -------------------------------------------- */

/* `tile()` escapes all three of its arguments, so nothing passed to it is escaped here:
 * escaping twice drew a model called `R&D` as `R&amp;D`. */
function detailTilesHtml(payload, ctx) {
  if (externalOnly(payload)) {
    var ext = payload.external;
    var calls = (ext.turns || []).reduce(function (n, t) { return n + (t.tool_call || 0); }, 0);
    var errored = (ext.turns || []).reduce(function (n, t) { return n + (t.error || 0); }, 0);
    var took = runDuration(payload.run, ctx);
    return [
      tile("Turns", fmt.num((ext.turns || []).length), "external harness"),
      tile("Duration", took.value, took.note),
      reportedCostTile(ext),
      tile("Tool calls", fmt.num(calls), errored ? errored + " errors" : "no errors reported")
    ].join("");
  }
  var traj = payload.trajectory;
  var run = payload.run;
  var usage = traj.usage || {};
  var extras = (traj.turns || []).filter(function (t) { return t.is_extra; }).length;
  var toolCount = (traj.turns || []).reduce(function (n, t) {
    return n + turnSteps(t).length;
  }, 0);
  var errors = (traj.turns || []).reduce(function (n, t) { return n + turnErrors(t); }, 0);
  return [
    tile("Turns", fmt.num(traj.turn_count),
         extras ? extras + " extra band" + (extras === 1 ? "" : "s") : "no extra bands"),
    tile("Tokens", fmt.tokens((usage.prompt_tokens || 0) + (usage.completion_tokens || 0)),
         fmt.tokens(usage.prompt_tokens) + " in / " + fmt.tokens(usage.completion_tokens) + " out"),
    tile("Cost", fmt.cost(traj.cost_usd), traj.model || run.model || "—"),
    tile("Tool calls", fmt.num(toolCount), errors ? errors + " errored" : "none errored")
  ].join("");
}

/* An external run's cost is only what its adapter reported (#691): labelled so, never a
 * Garuda-metered figure. */
function reportedCostTile(ext) {
  var rep = ext.reported_usage;
  if (!rep || typeof rep.cost_usd !== "number") return tile("Cost", "—", "not reported by the adapter");
  var who = (ext.identity || {}).model_id || ext.runtime || "—";
  return tile("Cost", fmt.cost(rep.cost_usd),
              "adapter-reported · " + ((rep.models || []).join(", ") || who));
}

/* How long the run took: the server's `duration_ms`, the figure the Runs list shows
 * (`reads._duration_ms`): recorded start to recorded end, a flow ending at its last step
 * receipt, and null when no end was recorded. Never recomputed here, so the two pages cannot
 * disagree, and never a clock reading passed off as a duration. */
function runDuration(run, ctx) {
  var ms = typeof run.duration_ms === "number" ? run.duration_ms : null;
  if (ctx && ctx.live) return { value: "—", note: "still running" };
  if (ms === null) return { value: "—", note: "no end was recorded" };
  return { value: fmt.duration(ms), note: "recorded start to end" };
}

/* An external harness's run, mixed with a native lane: who ran it and the shape Garuda
 * recorded. Its own messages and tool calls are not stored, so the counts are the whole
 * trail, and the card says so. */
function externalTrailHtml(ext) {
  if (!ext) return "";
  return '<div class="card" id="external-trail"><h2>External harness</h2>' +
    externalFactsHtml(ext) + externalTurnsHtml(ext) +
    '<div class="stat-sub">Prompts sent: ' + esc(ext.prompts_sent) + "</div>" +
    externalApprovalsHtml(ext) +
    '<p class="stat-sub">' + esc(ext.note) + "</p></div>";
}

function externalFactsHtml(ext) {
  var who = ext.identity;
  var adapter = ext.adapter || {};
  // No identity record at all means none of these were recorded — not that each was the
  // harness default.
  var unknown = "not recorded";
  var facts = [
    ["Harness", (ext.runtime || "—") + (adapter.version ? " " + adapter.version : "")],
    ["Role", who ? who.name || "—" : unknown],
    ["Model", who ? who.model_id || "harness default" : unknown],
    ["Effort", who ? who.effort || "—" : unknown],
    ["Harness mode", (ext.options || {}).mode || (who ? who.harness_mode || "harness default" : unknown)],
    ["Write policy", who ? who.write_policy || "—" : unknown]
  ];
  return '<dl class="trace-facts">' + facts.map(function (f) {
    return "<dt>" + esc(f[0]) + "</dt><dd>" + esc(f[1]) + "</dd>";
  }).join("") + "</dl>";
}

function externalTurnsHtml(ext) {
  var rows = (ext.turns || []).map(function (t) {
    return "<tr><td>" + esc(t.turn) + '</td><td class="num">' + esc(t.message) +
      '</td><td class="num">' + esc(t.tool_call) + '</td><td class="num">' + esc(t.tool_result) +
      '</td><td class="num">' + esc(t.approval_request) + '</td><td class="num">' + esc(t.error) +
      "</td></tr>";
  }).join("");
  return '<div class="table-wrap"><table class="table" id="external-turns"><thead><tr>' +
    '<th>Turn</th><th class="num">Message events</th><th class="num">Tool calls</th>' +
    '<th class="num">Tool results</th><th class="num">Approval requests</th>' +
    '<th class="num">Errors</th></tr></thead><tbody>' +
    (rows || '<tr><td colspan="6">No turns recorded</td></tr>') + "</tbody></table></div>";
}

function externalApprovalsHtml(ext) {
  var approvals = (ext.approvals || []).map(function (a) {
    var detail = a.tool_kind && a.reason ? " (" + esc(a.tool_kind) + ", " + esc(a.reason) + ")" : "";
    return '<li class="external-approval"><span class="mono">' + esc(a.action || "permission") +
      "</span> — " + esc(a.outcome || "unknown") + detail + "</li>";
  }).join("");
  var requested = (ext.turns || []).reduce(function (n, t) { return n + (t.approval_request || 0); }, 0);
  if (approvals) return "<h3>Approvals</h3><ul id=\"external-approvals\">" + approvals + "</ul>";
  // Requests seen in the trail with no decision stored: say exactly that, without
  // guessing what the decision was.
  if (requested) {
    return '<p class="stat-sub" id="external-approvals">' + esc(requested) + " approval request" +
      (requested === 1 ? "" : "s") + " in the harness trail; no decision record was stored.</p>";
  }
  return '<p class="stat-sub" id="external-approvals">No approvals requested.</p>';
}

/* The page an external-only run leads with: what Garuda did record — the task, the outcome
 * and how long it took, the prompts it sent, per-turn counts, approvals and the final
 * message — and one short note for everything it does not store, in place of a column of
 * empty panels each explaining its own absence. */
function externalSummaryHtml(payload, ctx) {
  var run = payload.run;
  var ext = payload.external;
  var took = runDuration(run, ctx);
  var outcome = ctx.live ? "in progress"
    : run.status === "running" ? "unknown — no outcome was recorded"
    : (run.state_label || run.status || "unknown");
  var final = run.final_message
    ? "<pre>" + esc(run.final_message) + "</pre>"
    : '<div class="block-note">' + (ctx.live ? "None yet — the run is still going."
                                             : "No final message was stored.") + "</div>";
  return (
    '<div class="card" id="external-summary"><h2>What Garuda recorded</h2>' +
    '<dl class="trace-facts">' +
    "<dt>Outcome</dt><dd>" + statusPill(run.status) + " " + esc(outcome) + "</dd>" +
    "<dt>Duration</dt><dd>" + esc(took.value) + ' <span class="stat-sub">' + esc(took.note) +
    "</span></dd>" +
    "<dt>Prompts sent</dt><dd>" + esc(fmt.num(ext.prompts_sent)) + "</dd>" +
    "</dl>" +
    '<div class="block block-input"><div class="block-label">task</div><pre>' +
    esc(run.task || "") + "</pre></div>" +
    '<div class="block block-output" id="external-final"><div class="block-label">' +
    "final message</div>" + final + "</div>" +
    "<h3>Who ran it</h3>" + externalFactsHtml(ext) +
    "<h3>Per turn</h3>" + externalTurnsHtml(ext) +
    externalApprovalsHtml(ext) +
    '<p class="stat-sub" id="external-unknown"><b>Not recorded:</b> ' + esc(ext.note) +
    " So there is no turn-by-turn trace, per-call token count or context-pressure chart for " +
    "this run.</p></div>"
  );
}

/* A log with no turn structure at all — the 1.1.0 vintage most archived runs are written in.
 * `finished` is meaningless there, because nothing in the log could ever have set it, so every
 * per-turn completion flag must be suppressed. Raising "TURN NEVER FINISHED" on all of them
 * would be a false alarm on the majority of real data. */
function isLegacyLog(traj) {
  return (traj.warning_codes || []).indexOf("legacy_log_format") !== -1;
}

/* An external harness's run with no native log: its trail is the summary card, so
 * native-log alarms (no session_end, empty log, no turns) would be false here. */
function externalOnly(payload) {
  return !!payload.external && !(payload.trajectory.turns || []).length;
}

/* The reader's warnings that only say a run is still being written. While it is live, the log
 * has no session_end and its tail may be torn mid-write; the turn in progress has no
 * turn_metrics, may not have its model call or tool results yet. The same warnings on a run
 * that is not live — or on any earlier turn — are findings, and are kept. */
var LIVE_RUN_EXPECTED = { no_session_end: 1, torn_tail: 1 };
var LIVE_TURN_EXPECTED = {
  turn_never_finished: 1, tool_call_unanswered: 1, tool_step_unexecuted: 1, no_model_call: 1,
  missing_budget_snapshot: 1
};

function expectedWhileLive(anomaly, ctx) {
  if (!ctx.live) return false;
  if (LIVE_RUN_EXPECTED[anomaly.code]) return true;
  return !!LIVE_TURN_EXPECTED[anomaly.code] && anomaly.turn_index === ctx.lastIndex;
}

/* Everything that says "look here first". A killed run, a rejected gate and an unpriceable
 * cost are all things you want to see before scrolling.
 *
 * A live run has no session_end and an unfinished last turn *because it is running*: neither
 * is an alarm then. Both are kept for a run that is not live, where they mean it stopped. */
function runFlagsHtml(payload, ctx) {
  if (externalOnly(payload)) return "";
  ctx = ctx || traceContext(payload);
  var traj = payload.trajectory;
  var legacy = ctx.legacy;
  var notes = [];

  if (!legacy && !ctx.live && (traj.success === null || traj.success === undefined)) {
    notes.push(["err", "No session_end in the log",
                "The run stopped without recording an outcome — it was killed, or its process " +
                "died. This is not a failure verdict: the harness never got to record one."]);
  }
  var unfinished = legacy ? [] : (traj.turns || []).filter(function (t) {
    return !t.finished && !turnInProgress(t, ctx);
  });
  if (unfinished.length) {
    notes.push(["err", "TURN NEVER FINISHED",
                "Turn " + unfinished.map(turnNumber).join(", ") +
                " has no turn_metrics. That is where it stopped."]);
  }
  if (traj.cost_usd === null && (traj.turns || []).length) {
    notes.push(["", "Cost unavailable",
                "At least one model call could not be priced, so no total is reported " +
                "rather than an understated one."]);
  }

  // The reader's own words, not a code chip: it explains what a warning means for this run,
  // and one anomaly per code is enough to say it.
  var seen = {};
  (traj.warnings || []).forEach(function (anomaly) {
    if (expectedWhileLive(anomaly, ctx)) return;
    if (seen[anomaly.code]) return;
    seen[anomaly.code] = 1;
    var where = anomaly.turn_index !== null && anomaly.turn_index !== undefined
      ? " (turn band " + (anomaly.turn_index + 1) + ")" : "";
    notes.push(["", "Reader note · " + anomaly.code, anomaly.message + where]);
  });

  // Every part is plain text, escaped exactly once here.
  return notes.map(function (note) {
    return '<div class="notice' + (note[0] ? " " + note[0] : "") + '">' +
           '<div class="notice-title">' + esc(note[1]) + "</div>" +
           '<div class="notice-body">' + esc(note[2]) + "</div></div>";
  }).join("");
}

/* --- context pressure ----------------------------------------------------- */

function pressureChartHtml(payload) {
  var traj = payload.trajectory;
  var series = (traj.turns || []).map(function (t) {
    var budget = t.budget;
    var fraction = budget && typeof budget.fraction === "number" ? budget.fraction : null;
    var compactions = (t.compactions || []);
    var overflow = compactions.some(function (c) { return c.reason === "context_overflow"; });
    var name = "turn " + turnNumber(t) + (t.label ? " (" + t.label + ")" : "");
    var text;
    if (fraction === null) {
      text = name + " — no budget snapshot recorded";
    } else {
      text = name + " — " + Math.round(fraction * 100) + "% of " +
             fmt.tokens(budget.capacity_tokens) + " tokens";
    }
    if (compactions.length) text += " · " + compactions.map(compactionLabel).join(", ");
    return {
      value: fraction,
      label: text,
      tick: t.label ? "final" : String(turnNumber(t)),
      marks: compactions.length > 0,
      overflow: overflow
    };
  });
  if (series.length > 24) {
    series.forEach(function (point, index) { if (index % 3) point.tick = ""; });
  }
  var thresholdOpt = typeof payload.condenser_threshold === "number"
    ? payload.condenser_threshold : undefined;
  return pressure(series, {
    title: "Context pressure",
    threshold: thresholdOpt,
    emptyNote: "No budget snapshots in this log."
  }) + pressureLegendHtml(traj);
}

function pressureLegendHtml(traj) {
  var compactions = (traj.turns || []).reduce(function (list, t) {
    return list.concat(t.compactions || []);
  }, []);
  var missing = (traj.turns || []).filter(function (t) {
    return !t.budget || typeof t.budget.fraction !== "number";
  }).length;
  var parts = [];
  if (compactions.length) {
    parts.push(compactions.length + " compaction" + (compactions.length === 1 ? "" : "s") +
               ": " + compactions.map(compactionLabel).join(", "));
  }
  if (missing) {
    parts.push(missing + " turn" + (missing === 1 ? "" : "s") + " with no snapshot (shown as " +
               "a gap, not interpolated — the emission is best-effort)");
  }
  if (!parts.length) return "";
  return '<div class="stat-sub">' + esc(parts.join(" · ")) + "</div>";
}

function compactionLabel(c) {
  var what = c.action || c.strategy || "compaction";
  if (c.action === "prune" && c.pruned) what = "pruned " + c.pruned;
  var saved = typeof c.tokens_before === "number" && typeof c.tokens_after === "number"
    ? " (" + fmt.tokens(c.tokens_before) + "→" + fmt.tokens(c.tokens_after) + ")" : "";
  var why = c.reason && c.reason !== "proactive" ? " [" + c.reason + "]" : "";
  return what + saved + why;
}

/* --- the timeline (with the gate lane interleaved as grid cells) ---------- */

/* The timeline as keyed pieces, one grid child each, in display order — what `patchTimeline`
 * reconciles. Keys are stable across refreshes: a turn's index, a phase's position. */
function timelinePieces(traj, hasGates, ctx) {
  var turns = traj.turns || [];
  if (!turns.length) {
    return [{ key: "empty", html:
      '<div class="empty span-all"><p>No turns in this log' + (ctx.live ? " yet" : "") + ".</p>" +
      (ctx.live
        ? "<p>The run has not finished its first turn; turns appear here as they complete.</p>"
        : "<p>Either the run failed before its first model call, or the log is from a " +
          "build that did not record enough to segment.</p>") + "</div>" }];
  }
  var phases = traj.phases || [];
  var showPhases = phases.length > 1;
  var pieces = [];
  for (var i = 0; i < turns.length; i++) {
    if (showPhases) {
      for (var a = 0; a < phases.length; a++) {
        if (phases[a].turn_start === i) {
          pieces.push({ key: "phase-" + a, html: phaseHeaderHtml(phases[a]) });
        }
      }
    }
    pieces.push({ key: "turn-" + turns[i].index,
                  html: turnCardHtml(turns[i], turns[i - 1], traj, ctx) });
    if (hasGates) pieces.push({ key: "gate-" + turns[i].index, html: gateCellHtml(turns[i]) });
    if (showPhases) {
      // `turn_end` is EXCLUSIVE — Phase(0, 3) covers bands 0..2 — so the critic strip belongs
      // after band turn_end-1. Comparing it to `i` directly would render the critic one row
      // into the *next* phase.
      for (var b = 0; b < phases.length; b++) {
        if (phases[b].critic && phases[b].turn_end === i + 1) {
          pieces.push({ key: "critic-" + b, html: phaseCriticHtml(phases[b]) });
        }
      }
    }
  }
  return pieces;
}

function phaseHeaderHtml(phase) {
  var count = phase.turn_end - phase.turn_start;
  return (
    '<div class="phase-head span-all">' +
    '<span class="phase-kind">' + esc(phase.kind) + "</span>" +
    (phase.attempt !== null && phase.attempt !== undefined
      ? '<span class="meta">round ' + esc(phase.attempt) + "</span>" : "") +
    '<span class="meta">' +
    (count === 1 ? "1 band" : "bands " + esc(phase.turn_start + 1) + "–" + esc(phase.turn_end)) +
    "</span>" +
    (phase.summary
      ? '<span class="stat-sub">' + esc(fmt.truncate(phase.summary, 140)) + "</span>" : "") +
    "</div>"
  );
}

function phaseCriticHtml(phase) {
  var critic = phase.critic;
  return (
    '<div class="phase-critic span-all">' +
    '<span class="pill ' + verdictClass(critic.approved) + '">critic ' +
    verdictWord(critic.approved) + "</span>" +
    (critic.feedback
      ? '<span class="stat-sub">' + esc(fmt.truncate(critic.feedback, 400)) + "</span>" : "") +
    "</div>"
  );
}

function turnCardHtml(turn, previous, traj, ctx) {
  var steps = turnSteps(turn);
  var fraction = turn.budget && typeof turn.budget.fraction === "number"
    ? Math.round(turn.budget.fraction * 100) + "%" : "—";

  return (
    '<div class="turn" data-turn="' + esc(turn.index) + '" id="turn-' + esc(turn.index) + '">' +
    '<button type="button" class="turn-head" data-toggle="' + esc(turn.index) + '">' +
    '<span class="turn-no">' + esc(turnNumber(turn)) + "</span>" +
    '<span class="turn-flags">' + turnFlagsHtml(turn, steps, ctx) + "</span>" +
    '<span class="turn-stats">' +
    turnStat("model", fmt.duration(turn.model_ms)) +
    turnStat("tools", String(steps.length)) +
    turnStat("tokens", fmt.tokens(turn.prompt_tokens)) +
    turnStat("ctx", fraction) +
    turnStat("cost", fmt.cost(turn.cost_usd)) +
    "</span>" +
    '<span class="turn-caret">▸</span>' +
    "</button>" +
    '<div class="turn-body" id="turn-body-' + esc(turn.index) + '" hidden>' +
    turnBodyHtml(turn, previous, traj, steps, ctx) +
    "</div></div>"
  );
}

function turnStat(label, value) {
  return '<span class="turn-stat"><span class="turn-stat-k">' + esc(label) + "</span>" +
         '<span class="turn-stat-v">' + esc(value) + "</span></span>";
}

function turnFlagsHtml(turn, steps, ctx) {
  var flags = [];
  var running = turnInProgress(turn, ctx);
  if (turn.label) flags.push(["accent", turn.label]);
  if (!turn.finished && !ctx.legacy) {
    flags.push(running ? ["accent", "in progress"] : ["err", "never finished"]);
  }
  var pending = steps.filter(function (s) { return s.status === "pending"; }).length;
  // A dispatched call with no result yet is the normal state of a turn in progress.
  if (pending) flags.push(running ? ["", pending + " running"] : ["err", pending + " pending"]);
  var errors = turnErrors(turn);
  if (errors) flags.push(["err", errors + " tool error" + (errors === 1 ? "" : "s")]);
  if ((turn.permission_denials || []).length) {
    flags.push(["warn", turn.permission_denials.length + " denied"]);
  }
  if ((turn.subagents || []).length) {
    flags.push(["accent", turn.subagents.length + " subagent" +
                (turn.subagents.length === 1 ? "" : "s")]);
  }
  if (turn.failure_steer !== null && turn.failure_steer !== undefined) {
    flags.push(["warn", "failure streak " + turn.failure_steer]);
  }
  if (turn.environment_unavailable) flags.push(["err", "env unavailable"]);
  if ((turn.compactions || []).length) flags.push(["warn", "compacted"]);
  if ((turn.budget_reviews || []).length) flags.push(["", "budget notice"]);
  return flags.map(function (f) {
    return '<span class="flag' + (f[0] ? " flag-" + f[0] : "") + '">' + esc(f[1]) + "</span>";
  }).join("");
}

/* The four parts of a turn, in the order they happened. */
function turnBodyHtml(turn, previous, traj, steps, ctx) {
  var out = inputHtml(turn, previous, traj);

  (turn.model_calls || []).forEach(function (call, index) {
    if (call.truncated) {
      out += '<div class="notice err"><div class="notice-title">Response truncated</div>' +
             '<div class="notice-body">The provider cut this response at max_tokens. Any ' +
             "tool call it was mid-way through writing is not in the log.</div></div>";
    }
    var suffix = (turn.model_calls.length > 1) ? " · call " + (index + 1) : "";
    // Which model did this call: the badge is the reported one, separate from the selected.
    out += '<div class="call-model"><span class="chip chip-off" data-call-model="' + esc(call.model || "") + '">' +
           esc(call.model || "model not reported") + (call.call_purpose ? " · " + esc(call.call_purpose) : "") +
           "</span></div>";
    if (call.reasoning) out += blockHtml("thinking" + suffix, call.reasoning, "reasoning");
    if (call.content) out += blockHtml("says" + suffix, call.content, "output");
    if (!call.reasoning && !call.content && !(call.tool_steps || []).length) {
      out += '<div class="stat-sub">This model call returned neither text nor a tool call.' +
             "</div>";
    }
  });

  (turn.compactions || []).forEach(function (c) {
    out += '<div class="event-note"><span class="flag flag-warn">compaction</span> ' +
           '<span class="mono">' + esc(compactionLabel(c)) + "</span>" +
           (c.recovered === false
             ? ' <span class="flag flag-err">did not recover</span>' : "") + "</div>";
  });

  (turn.budget_reviews || []).forEach(function (review) {
    out += '<div class="event-note"><span class="flag">budget</span> ' +
           '<span class="mono">' + esc(JSON.stringify(review)) + "</span></div>";
  });

  (turn.permission_denials || []).forEach(function (denial) {
    out += '<div class="event-note"><span class="flag flag-warn">denied</span> ' +
           '<span class="mono">' + esc(denial.name || denial.action || "?") + "</span> " +
           '<span class="stat-sub">' + esc(denial.reason || "no reason recorded") +
           "</span></div>";
  });

  if (turn.environment_unavailable) {
    out += '<div class="event-note"><span class="flag flag-err">environment unavailable</span> ' +
           '<span class="mono">' + esc(JSON.stringify(turn.environment_unavailable)) +
           "</span></div>";
  }

  if (steps.length) {
    out += '<div class="section-label">does</div>' +
           steps.map(function (step) { return stepHtml(step, turn, ctx); }).join("");
  }

  if ((turn.events || []).length) {
    out += '<div class="stat-sub">events ' + esc(turn.events[0]) + "–" +
           esc(turn.events[turn.events.length - 1]) + " of the log · " +
           '<a href="#" data-jump="' + esc(turn.events[0]) + '">show raw</a></div>';
  }
  return out || '<div class="stat-sub">Nothing but the turn boundary was recorded here.</div>';
}

/* What the model was given. Three cases, and saying which one it is matters more than the
 * text: turn 1 gets the task, a turn with a `user_message` in its band was steered or
 * interjected on, and everything else continues from the previous turn's tool results —
 * which is the normal case and the one a reader is most likely to get wrong. */
function inputHtml(turn, previous, traj) {
  var messages = turn.user_messages || [];
  var out = "";
  if (turn.index === 0 && traj.task) {
    out += blockHtml("in · the task", traj.task, "input");
  }
  messages.forEach(function (message) {
    if (!message.content) return;
    out += blockHtml("in · message during this turn", message.content, "input");
  });
  if (!out) {
    var carried = previous ? turnSteps(previous).length : 0;
    // "turn ?" for a log with no turn numbers reads as a rendering bug rather than as missing
    // data, so an unnumbered predecessor is named by position instead.
    var whence = previous && turnNumber(previous) !== "?"
      ? "turn " + turnNumber(previous) : "the previous turn";
    var text = previous
      ? "Continues from " + whence + " — its " + carried +
        (carried === 1 ? " tool result is" : " tool results are") + " this turn's input."
      : "No recorded input: the log starts here.";
    out += '<div class="block block-input block-quiet"><div class="block-label">in</div>' +
           '<div class="block-note">' + esc(text) + "</div></div>";
  }
  return out;
}

function blockHtml(label, text, cls) {
  var body = String(text);
  var limit = STEP_OUTPUT_CHARS * 2;
  var clipped = body.length > limit;
  return (
    '<div class="block block-' + cls + '">' +
    '<div class="block-label">' + esc(label) + "</div>" +
    "<pre>" + esc(clipped ? body.slice(0, limit) : body) + "</pre>" +
    (clipped ? '<div class="stat-sub">' + (body.length - limit) +
               " more characters not shown</div>" : "") +
    "</div>"
  );
}

/* --- tool steps ----------------------------------------------------------- */

var STEP_CLASS = {
  ok: "success",
  error: "failed",
  pending: "running",
  denied: "failed",
  unexecuted: "unknown",
  orphan: "unknown",
  gate_approved: "success",
  gate_rejected: "failed",
  gate_unknown: "unknown"
};

/* What each status means, since the difference between them is the whole point of having
 * them: four distinct causes used to collapse into "the tool didn't run". */
var STEP_WHY = {
  pending: "dispatched, no result in the log — this is where a hang shows up",
  unexecuted: "the model asked for it; the loop never dispatched it",
  denied: "refused by the permission engine before it ran",
  orphan: "a result with no matching call in any response",
  gate_unknown: "task_complete ran but no verdict was recorded"
};

function stepHtml(step, turn, ctx) {
  var cls = STEP_CLASS[step.status] || "unknown";
  var family = toolFamily(step.name);
  var why = STEP_WHY[step.status];
  if (step.status === "pending" && turnInProgress(turn, ctx)) {
    why = "dispatched; its result has not been written yet";
  }
  return (
    '<div class="step step-' + family + '">' +
    '<div class="step-head">' +
    '<span class="fchip f-' + family + '">' + esc(FAMILY_ICON[family]) + "</span>" +
    '<span class="mono step-name">' + esc(step.name) + "</span>" +
    '<span class="pill ' + cls + '">' + esc(step.status) + "</span>" +
    (step.duration_ms !== null && step.duration_ms !== undefined
      ? '<span class="meta">' + esc(fmt.duration(step.duration_ms)) + "</span>" : "") +
    (step.call_id ? '<span class="meta">' + esc(fmt.truncate(step.call_id, 18)) + "</span>" : "") +
    "</div>" +
    (why ? '<div class="stat-sub">' + esc(why) + "</div>" : "") +
    (step.denial_reason ? '<div class="stat-sub">' + esc(step.denial_reason) + "</div>" : "") +
    argumentsHtml(step) +
    outputHtml(step) +
    subagentHtml(step, turn) +
    "</div>"
  );
}

/* A subagent's own turns are in a *separate* log, because it ran on a separate event store —
 * interleaving them into the parent's would corrupt the parent's turn segmentation. So the
 * parent's handoff event names the child's session id, and this offers to open it. Fetched on
 * click rather than up front: a run can delegate a dozen times and each child is a full
 * trajectory. */
function subagentHtml(step, turn) {
  if (toolFamily(step.name) !== "subagent") return "";
  var handoffs = turn.subagents || [];
  // One `invoke_subagent` step pairs with one handoff, in order. Matching by position within
  // the turn rather than by id, because the handoff event carries no tool_call_id — it is
  // appended by the subagent runner, not by the tool runner.
  var index = turnSteps(turn).filter(function (s) {
    return toolFamily(s.name) === "subagent";
  }).indexOf(step);
  var handoff = handoffs[index];
  if (!handoff) {
    return '<div class="stat-sub">No handoff recorded for this subagent — it was denied, ' +
           "never dispatched, or the run died inside it.</div>";
  }
  var id = handoff.session_id;
  return (
    '<div class="subagent">' +
    '<span class="pill ' + (handoff.success ? "success" : "failed") + '">' +
    esc(handoff.subagent || "subagent") + "</span>" +
    (typeof handoff.turns === "number"
      ? '<span class="meta">' + esc(handoff.turns) + " turns inside</span>" : "") +
    (id
      ? '<button type="button" class="btn btn-small" data-subagent="' + esc(id) + '">' +
        "Open its trace</button>"
      : '<span class="stat-sub">This run predates subagent traces being kept, so only the ' +
        "handoff was recorded.</span>") +
    '<div class="sub-body" id="sub-' + esc(id || "none") + '"></div>' +
    "</div>"
  );
}

function openSubagent(subSessionId) {
  var node = el("sub-" + subSessionId);
  if (!node) return;
  if (node.innerHTML) { node.innerHTML = ""; return; }   // a second click closes it
  if (!STATE.raw || !STATE.raw.subagents) {
    toast("Subagent traces are only kept beside a local session.");
    return;
  }
  node.innerHTML = '<div class="loading">Loading subagent trace…</div>';
  // Looked up again when the answer arrives: a live refresh may have replaced the turn card
  // (carrying this placeholder into the new one), and the view may be gone altogether.
  var fill = function (html) {
    var target = el("sub-" + subSessionId);
    if (target) target.innerHTML = html;
  };
  api(STATE.raw.subagents + encodeURIComponent(subSessionId))
    .then(function (payload) {
      fill(subTraceHtml(payload));
    }, function (err) {
      if (isSuperseded(err)) return;
      fill('<div class="notice"><div class="notice-body">' +
           esc(err.status === 404
             ? "No trace was kept for this subagent. Runs from before subagent " +
               "logs were persisted only recorded the handoff."
             : err.message) + "</div></div>");
    });
}

/* The nested trace, deliberately flatter than the parent's: what it said and what it did, per
 * turn. A full second inspector inside a tool step would be unreadable, and the parent view
 * is one click away for anything deeper. */
function subTraceHtml(payload) {
  var traj = payload.trajectory;
  var turns = traj.turns || [];
  if (!turns.length) {
    return '<div class="stat-sub">Its log has no turns — it failed before its first model ' +
           "call.</div>";
  }
  return (
    '<div class="stat-sub">' + turns.length + " turns · " +
    esc(fmt.tokens((traj.usage || {}).prompt_tokens)) + " prompt tokens · " +
    esc(fmt.cost(traj.cost_usd)) + " · " + esc(traj.model || "?") + "</div>" +
    turns.map(function (turn) {
      var steps = turnSteps(turn);
      var text = (turn.model_calls || [])
        .map(function (c) { return c.content || ""; })
        .filter(Boolean).join("\n");
      return (
        '<div class="sub-turn">' +
        '<span class="turn-no">' + esc(turnNumber(turn)) + "</span>" +
        '<div class="sub-turn-body">' +
        (text ? "<pre>" + esc(fmt.truncate(text, 700)) + "</pre>" : "") +
        // Name *and* a clipped result. Names alone tell you it read four files without
        // telling you what it found, which is the only reason to open a subagent's trace.
        steps.map(function (step) {
          return (
            '<div class="sub-step">' +
            '<span class="fchip f-' + toolFamily(step.name) +
            (step.is_error ? " fchip-err" : "") + '">' +
            esc(FAMILY_ICON[toolFamily(step.name)]) + "</span>" +
            '<span class="mono sub-step-name">' + esc(step.name) + "</span>" +
            (step.content
              ? '<span class="sub-step-out mono' + (step.is_error ? " out-err" : "") + '">' +
                esc(fmt.truncate(String(step.content).replace(/\s+/g, " "), 220)) + "</span>"
              : '<span class="meta">' + esc(step.status) + "</span>") +
            "</div>"
          );
        }).join("") +
        "</div></div>"
      );
    }).join("")
  );
}

function argumentsHtml(step) {
  var args = step.arguments || {};
  var rewritten = step.requested_arguments
    ? '<div class="stat-sub">A hook rewrote these arguments before the call ran.</div>' : "";
  var diff = diffHtml(step.name, args);
  if (diff) return rewritten + diff;
  var keys = Object.keys(args);
  if (!keys.length) return rewritten;
  return rewritten + '<pre class="args">' +
         esc(fmt.truncate(JSON.stringify(args, null, 1), 1200)) + "</pre>";
}

/* A two-colour diff for the three tools that change files. Argument names are the real ones
 * (`path`, `old_string`, `new_string`, `edits`, `content`) — not guessed. */
function diffHtml(name, args) {
  if (name === "write_file" && typeof args.content === "string") {
    return diffFrame(args.path, [["ins", args.content]]);
  }
  if (name === "edit" && typeof args.old_string === "string") {
    return diffFrame(args.path, [["del", args.old_string], ["ins", args.new_string || ""]],
                     args.replace_all ? "replace_all" : "");
  }
  if (name === "multi_edit" && Array.isArray(args.edits)) {
    var hunks = [];
    args.edits.forEach(function (edit) {
      if (!edit || typeof edit !== "object") return;
      hunks.push(["del", String(edit.old_string === undefined ? "" : edit.old_string)]);
      hunks.push(["ins", String(edit.new_string === undefined ? "" : edit.new_string)]);
    });
    return diffFrame(args.path, hunks, args.edits.length + " edits");
  }
  return "";
}

function diffFrame(path, hunks, note) {
  var body = hunks.map(function (hunk) {
    var lines = String(hunk[1]).split("\n");
    var shown = lines.slice(0, DIFF_MAX_LINES);
    var marker = hunk[0] === "del" ? "-" : "+";
    var html = shown.map(function (l) {
      return '<div class="' + hunk[0] + '">' + esc(marker + " " + l) + "</div>";
    }).join("");
    if (lines.length > shown.length) {
      html += '<div class="diff-more">… ' + (lines.length - shown.length) + " more lines</div>";
    }
    return html;
  }).join("");
  return (
    '<div class="diff">' +
    '<div class="diff-head"><span class="mono">' + esc(path || "(no path)") + "</span>" +
    (note ? '<span class="meta">' + esc(note) + "</span>" : "") + "</div>" +
    body + "</div>"
  );
}

function outputHtml(step) {
  if (step.content === null || step.content === undefined || step.content === "") return "";
  var text = String(step.content);
  var clipped = text.length > STEP_OUTPUT_CHARS;
  var buffered = text.match(/\[buffer:([A-Za-z0-9._-]+)\]/);
  return (
    '<pre class="out' + (step.is_error ? " out-err" : "") + '">' +
    esc(clipped ? text.slice(0, STEP_OUTPUT_CHARS) : text) + "</pre>" +
    (clipped ? '<div class="stat-sub">' + (text.length - STEP_OUTPUT_CHARS) +
               " more characters not shown</div>" : "") +
    (buffered
      ? '<div class="stat-sub">Archived in buffer ' +
        (STATE.raw && STATE.raw.buffers
          ? '<a href="#" data-buffer="' + esc(buffered[1]) + '">' + esc(buffered[1]) + "</a>"
          : '<span class="mono">' + esc(buffered[1]) + "</span>") +
        "</div>"
      : "")
  );
}

/* --- the gate lane ------------------------------------------------------- */

/* The header. A gate that is OFF must read differently from one that never fired — without
 * the resolved posture from session_start there is no way to tell the two apart, which is
 * exactly why that event now carries it. */
function gateStackHtml(traj) {
  var stack = traj.gate_stack;
  if (!stack) {
    return (
      "<h2>Gate stack</h2>" +
      '<div class="stat-sub">Not recorded. This log predates <code>session_start</code> ' +
      "carrying the resolved config, so a gate being off is indistinguishable from a gate " +
      "that never fired. Re-run to get the posture.</div>"
    );
  }
  var chips = Object.keys(stack).map(function (name) {
    var on = stack[name] === true;
    return '<span class="chip ' + (on ? "chip-on" : "chip-off") + '">' +
           esc(name.replace(/^(enable_|require_)/, "")) + "</span>";
  }).join("");
  return (
    "<h2>Gate stack</h2>" +
    '<div class="chips">' + chips + "</div>" +
    '<div class="stat-sub">mode <span class="mono">' + esc(traj.mode || "?") + "</span>" +
    (traj.permission_mode
      ? ' · permissions <span class="mono">' + esc(traj.permission_mode) + "</span>" : "") +
    " · a filled chip is on, an outlined one off</div>"
  );
}

function gateCellHtml(turn) {
  var gates = turn.gates || [];
  if (!gates.length) return '<div class="gate-cell"></div>';
  return '<div class="gate-cell">' + gates.map(gateHtml).join("") + "</div>";
}

function gateHtml(gate) {
  return (
    '<div class="gate gate-' + verdictClass(gate.approved) + '">' +
    '<div class="gate-head">' +
    '<span class="pill ' + verdictClass(gate.approved) + '">' +
    verdictWord(gate.approved) + "</span>" +
    (gate.self_check ? '<span class="meta">self-check: ' + esc(gate.self_check) + "</span>" : "") +
    (gate.attempt !== null && gate.attempt !== undefined
      ? '<span class="meta">attempt ' + esc(gate.attempt) + "</span>" : "") +
    (gate.contract_action ? '<span class="meta">' + esc(gate.contract_action) + "</span>" : "") +
    "</div>" +
    (gate.summary
      ? '<div class="gate-summary">' + esc(fmt.truncate(gate.summary, 600)) + "</div>" : "") +
    checklistHtml(gate.checklist) +
    evidenceHtml(gate) +
    (gate.outstanding && gate.outstanding.length
      ? '<div class="gate-block"><div class="block-label">outstanding</div>' +
        gate.outstanding.map(function (id) {
          return '<span class="chip chip-off">' + esc(id) + "</span>";
        }).join("") + "</div>"
      : "") +
    (gate.feedback
      ? '<div class="gate-block"><div class="block-label">feedback</div><pre>' +
        esc(fmt.truncate(gate.feedback, 1400)) + "</pre></div>"
      : "") +
    (gate.answer_rationale
      ? '<div class="gate-block"><div class="block-label">rationale</div><pre>' +
        esc(fmt.truncate(gate.answer_rationale, 800)) + "</pre></div>"
      : "") +
    "</div>"
  );
}

function checklistHtml(checklist) {
  var keys = Object.keys(checklist || {});
  if (!keys.length) return "";
  return (
    '<div class="chips">' +
    keys.map(function (key) {
      var value = checklist[key];
      var on = value === true;
      var off = value === false;
      return '<span class="chip ' + (on ? "chip-on" : off ? "chip-bad" : "chip-off") + '">' +
             esc(key) + (on || off ? "" : " " + esc(String(value))) + "</span>";
    }).join("") +
    "</div>"
  );
}

/* Evidence is `list[dict]` from the verifier — {command, class, exit_code, stdout, stderr} —
 * but an older log carries bare command strings, so both shapes render. */
function evidenceHtml(gate) {
  var evidence = gate.evidence || [];
  var declared = gate.verification_commands || [];
  if (!evidence.length && !declared.length) return "";
  if (!evidence.length) {
    return (
      '<div class="gate-block"><div class="block-label">verification commands (not run)</div>' +
      declared.map(function (cmd) {
        return '<div class="mono ev-cmd">' + esc(fmt.truncate(cmd, 200)) + "</div>";
      }).join("") + "</div>"
    );
  }
  return (
    '<div class="gate-block"><div class="block-label">evidence</div>' +
    evidence.map(function (entry) {
      if (typeof entry === "string") return '<div class="mono ev-cmd">' + esc(entry) + "</div>";
      var code = entry.exit_code;
      var okExit = code === 0;
      return (
        '<div class="ev">' +
        '<span class="pill ' + (okExit ? "success" : "failed") + '">exit ' +
        esc(fmt.num(code)) + "</span>" +
        '<span class="mono ev-cmd">' + esc(fmt.truncate(entry.command, 160)) + "</span>" +
        (entry["class"] ? '<span class="meta">' + esc(entry["class"]) + "</span>" : "") +
        (entry.stderr ? "<pre>" + esc(fmt.truncate(entry.stderr, 500)) + "</pre>" : "") +
        (entry.stdout && !okExit ? "<pre>" + esc(fmt.truncate(entry.stdout, 500)) + "</pre>" : "") +
        "</div>"
      );
    }).join("") + "</div>"
  );
}

function verdictClass(approved) {
  if (approved === true) return "success";
  if (approved === false) return "failed";
  return "unknown";
}

function verdictWord(approved) {
  if (approved === true) return "approved";
  if (approved === false) return "rejected";
  return "no verdict";
}

/* --- interaction ---------------------------------------------------------- */

function onTurnClick(event) {
  var jump = event.target.closest("[data-jump]");
  if (jump) {
    event.preventDefault();
    loadRawEvents(Number(jump.getAttribute("data-jump")));
    return;
  }
  var sub = event.target.closest("[data-subagent]");
  if (sub) {
    event.preventDefault();
    openSubagent(sub.getAttribute("data-subagent"));
    return;
  }
  var bufferLink = event.target.closest("[data-buffer]");
  if (bufferLink) {
    event.preventDefault();
    showBuffer(bufferLink.getAttribute("data-buffer"));
    return;
  }
  var toggle = event.target.closest("[data-toggle]");
  if (!toggle) return;
  var index = toggle.getAttribute("data-toggle");
  var body = el("turn-body-" + index);
  if (!body) return;
  setTurnOpen(index, body.hidden);
}

function showBuffer(bufferId) {
  var raw = STATE.raw;
  if (!raw || !raw.buffers) {
    toast("Buffers are only stored beside a local session.");
    return;
  }
  api(raw.buffers + encodeURIComponent(bufferId))
    .then(function (payload) {
      var node = el("raw-body");
      if (STATE.raw !== raw || !node) return;   // the view is gone
      raw.search = null;
      node.innerHTML =
        '<div class="block-label">buffer ' + esc(bufferId) + " · " + esc(payload.bytes) +
        " bytes" + (payload.truncated ? " (clipped)" : "") + "</div><pre>" +
        esc(payload.content) + "</pre>";
      el("raw-card").scrollIntoView({ block: "start" });
    }, function (err) {
      if (isSuperseded(err)) return;
      if (STATE.raw === raw) toast(err.message);
    });
}

/* --- the raw-event escape hatch ------------------------------------------ */

function rawEventsHtml(payload) {
  return (
    '<div class="card" id="raw-card">' +
    "<h2>Raw events</h2>" +
    '<div class="stat-sub"><span id="raw-total">' + esc(payload.event_count) + "</span>" +
    " events in the log. Everything above is derived from these; this is the check on the " +
    "derivation. The filter searches the loaded window of up to 500 events.</div>" +
    '<div class="filters">' +
    '<input type="search" id="raw-filter" placeholder="Filter loaded events by type or text…"' +
    ' aria-label="Filter loaded raw events">' +
    '<button type="button" class="btn" id="raw-load">Load events</button>' +
    "</div>" +
    '<div id="raw-body"></div></div>'
  );
}

/* Load the window that starts at `start`. `quiet` skips scrolling the pane into view, for a
 * load the reader started from inside it. */
function loadRawEvents(start, quiet) {
  var raw = STATE.raw;
  var body = el("raw-body");
  if (!raw || !body) return;
  raw.search = null;
  body.innerHTML = '<div class="loading">Loading events…</div>';
  var join = raw.url.indexOf("?") === -1 ? "?" : "&";
  return api(raw.url + join + "start=" + Math.max(0, start | 0)).then(
    function (payload) {
      if (STATE.raw !== raw || !body.isConnected) return;   // the view is gone
      raw.loaded = true;
      raw.start = payload.start;
      raw.count = payload.count;
      raw.total = payload.total;
      raw.events = payload.events;
      raw.note = null;
      renderRawEvents();
      if (!quiet) el("raw-card").scrollIntoView({ block: "start" });
    },
    function (err) {
      if (isSuperseded(err)) return;
      if (STATE.raw !== raw || !body.isConnected) return;
      body.innerHTML = '<div class="notice err"><div class="notice-body">' +
                       esc(err.message) + "</div></div>";
    });
}

function rawText(event) {
  return JSON.stringify(event.payload === undefined ? event : event.payload);
}

function rawMatches(event, needle) {
  return !needle || (event.type || "").toLowerCase().indexOf(needle) !== -1 ||
         rawText(event).toLowerCase().indexOf(needle) !== -1;
}

function rawFilterValue() {
  var input = el("raw-filter");
  return input ? (input.value || "").toLowerCase() : "";
}

/* Which events a filter result covers, in words: "events 1–500 of 7,512". */
function rawRange(start, count, total) {
  return count ? "events " + (start + 1) + "–" + (start + count) + " of " + total
               : "no events of " + total;
}

/* The filter runs over the loaded window only, and the pane says so: "Nothing matches" over
 * 500 of 7,500 events read as "nothing in this run matches", which was not what it checked.
 * When the window has no match the reader is offered the rest of the log, searched a window
 * at a time. */
function renderRawEvents() {
  var raw = STATE.raw;
  var body = el("raw-body");
  if (!raw || !body) return;
  var needle = rawFilterValue();
  if (!raw.loaded) {
    // Typing a filter before loading anything: there is nothing to search yet, so load the
    // first window rather than answer "nothing matches" about zero events.
    if (needle && !raw.loading) {
      raw.loading = true;
      Promise.resolve(loadRawEvents(raw.start, true)).then(function () { raw.loading = false; });
    }
    return;
  }
  var rows = [];
  for (var i = 0; i < raw.events.length; i++) {
    var event = raw.events[i];
    if (!rawMatches(event, needle)) continue;
    rows.push(
      '<div class="raw-row"><span class="raw-idx">' + esc(raw.start + i) + "</span>" +
      '<span class="raw-type">' + esc(event.type) + "</span>" +
      '<span class="raw-payload mono">' + esc(fmt.truncate(rawText(event), 400)) + "</span></div>"
    );
  }
  var end = raw.start + raw.count;
  var range = rawRange(raw.start, raw.count, raw.total);
  var summary;
  if (!needle) {
    summary = "";
  } else if (rows.length) {
    summary = '<div class="stat-sub" id="raw-summary">' + esc(rows.length) + " of the " +
              esc(raw.count) + " loaded events match (" + esc(range) + ")." +
              (raw.count < raw.total ? " Events outside this window were not searched." : "") +
              "</div>";
  } else {
    summary = '<div class="stat-sub" id="raw-summary">No match in the loaded window (' +
              esc(range) + ")." +
              (raw.count < raw.total ? " The rest of the log was not searched." : "") + "</div>" +
              '<div class="filters" id="raw-search">' +
              (end < raw.total ? '<button type="button" class="btn" data-search="later">' +
                                 "Search later events</button>" : "") +
              (raw.start > 0 ? '<button type="button" class="btn" data-search="earlier">' +
                               "Search earlier events</button>" : "") +
              "</div>";
  }
  if (raw.note) summary += '<div class="stat-sub" id="raw-search-note">' + esc(raw.note) + "</div>";
  var pager =
    '<div class="filters">' +
    (raw.start > 0
      ? '<button type="button" class="btn" data-page="' +
        Math.max(0, raw.start - 500) + '">← previous</button>' : "") +
    (end < raw.total
      ? '<button type="button" class="btn" data-page="' + end + '">next →</button>' : "") +
    '<span class="meta">' + esc(range) + "</span></div>";
  body.innerHTML = summary + rows.join("") +
    (!rows.length && !needle ? '<div class="stat-sub">No events in this window.</div>' : "") + pager;
}

/* Walk the log a window at a time, forwards or backwards from the loaded one, and stop at the
 * first window with a match. Abandoned when the filter changes, another load starts or the
 * view goes away. */
function searchRawEvents(direction) {
  var raw = STATE.raw;
  var body = el("raw-body");
  var needle = rawFilterValue();
  if (!raw || !body || !needle) return;
  var token = {};
  raw.search = token;
  var origin = direction > 0 ? raw.start + raw.count : raw.start;
  var join = raw.url.indexOf("?") === -1 ? "?" : "&";
  var status = function (text) {
    var node = el("raw-search");
    if (node) node.innerHTML = '<span class="meta">' + esc(text) + "</span>";
  };
  var done = function (searchedFrom, searchedTo) {
    raw.search = null;
    raw.note = "Searched events " + (searchedFrom + 1) + "–" + searchedTo + " as well: no match. " +
               "Nothing " + (direction > 0 ? "after" : "before") + " the loaded window matches.";
    renderRawEvents();
  };
  var step = function (start) {
    if (raw.search !== token || STATE.raw !== raw || !body.isConnected ||
        rawFilterValue() !== needle) return null;
    if (direction > 0 && start >= raw.total) return done(origin, raw.total);
    if (direction < 0 && start <= 0) return done(0, origin);
    var from = direction > 0 ? start : Math.max(0, start - 500);
    status("Searching events " + (from + 1) + "–" + Math.min(from + 500, raw.total) + " of " +
           raw.total + "…");
    return api(raw.url + join + "start=" + from).then(function (payload) {
      if (raw.search !== token || STATE.raw !== raw || !body.isConnected ||
          rawFilterValue() !== needle) return null;
      raw.total = payload.total;
      var hit = payload.events.some(function (event) { return rawMatches(event, needle); });
      if (hit) {
        raw.search = null;
        raw.start = payload.start;
        raw.count = payload.count;
        raw.events = payload.events;
        raw.note = null;
        renderRawEvents();
        return null;
      }
      if (!payload.count) return done(direction > 0 ? origin : from, direction > 0 ? raw.total : origin);
      return step(direction > 0 ? from + payload.count : from);
    }, function (err) {
      if (isSuperseded(err)) return;
      if (raw.search !== token) return;
      raw.search = null;
      raw.note = "The search stopped: " + err.message;
      renderRawEvents();
    });
  };
  step(origin);
}
