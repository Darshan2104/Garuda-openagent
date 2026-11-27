/* The runs list: every past run, with the stats over them.
 *
 * The browser replacement for `python -m garuda.eval.dashboard`, plus four charts over the
 * same rows. Each row's task links to its trace and its name to its session.
 *
 * The page head and the filter controls are rendered once per visit and never replaced: a
 * filter change re-renders only the tiles, charts and table (#runs-body), so typing in the
 * search box keeps its focus, caret and every keystroke. The filters live in the route's
 * query (#/runs?q=…&status=…&agent=…), so a filtered list survives a reload, can be shared,
 * and back/forward step through it. The server filters the whole store and pages it; the
 * page loads RUNS_PAGE_SIZE rows at a time and says how many it has not loaded. */

"use strict";

var RUNS_PAGE_SIZE = 100;

/* Whether this dashboard can talk to an agent. The UI hides controls off the server's
 * capability report rather than showing buttons that 503. */
function canRun() {
  return !!(STATE.health && STATE.health.capabilities && STATE.health.capabilities.chat);
}

function runFilters() {
  var params = routeParams();
  return { q: params.q || "", status: params.status || "", agent: params.agent || "" };
}

function runsView() {
  var state = { filters: runFilters(), runs: [], total: null, agents: [], seq: 0, unreadable: "" };
  STATE.runsList = state;
  render(
    '<div class="page-head"><h1>Runs</h1>' +
    '<span class="meta" id="runs-count" aria-live="polite"></span>' +
    '<span class="meta" id="runs-live-note"></span>' +
    '<button type="button" class="btn" id="runs-refresh">Refresh</button>' +
    (canRun() ? '<a class="btn btn-primary" href="#/chat">Talk to an agent</a>' : "") +
    "</div>" +
    '<p class="page-lede" id="runs-lede">Traces and metrics of what each agent did: turns, tokens, cost and time. ' +
    'State, queue, approvals and control are on <a href="#/sessions">Sessions</a>.</p>' +
    filtersHtml(state) +
    '<div id="runs-body"><div class="loading">Loading runs…</div></div>'
  );
  wireFilters(state);
  el("runs-refresh").addEventListener("click", function () { loadRuns(state); });
  el("runs-body").addEventListener("click", function (event) {
    // Links (the task, the session) navigate themselves; the rest of the row is a shortcut.
    if (event.target.closest("a, button")) return;
    var row = event.target.closest("tr[data-sid]");
    if (row && !String(window.getSelection ? window.getSelection() : "")) {
      location.hash = "#/runs/" + encodeURIComponent(row.getAttribute("data-sid"));
    }
  });
  return loadRuns(state);
}

/* Fetch the first page for the current filters, or with `more` the next older page. A
 * response for filters that have since changed is dropped. A failed first page is shown in
 * the body; a failed `more` rejects, so the button's guard reports it. */
function loadRuns(state, more) {
  var seq = ++state.seq;
  var params = new URLSearchParams();
  ["q", "status", "agent"].forEach(function (key) {
    if (state.filters[key]) params.set(key, state.filters[key]);
  });
  params.set("limit", String(RUNS_PAGE_SIZE));
  if (more) params.set("offset", String(state.runs.length));
  var body = el("runs-body");
  if (body) body.setAttribute("aria-busy", "true");
  return api("/api/runs?" + params.toString()).then(function (payload) {
    if (STATE.runsList !== state || seq !== state.seq || !el("runs-body")) return;
    var seen = {};
    state.runs = (more ? state.runs : []).concat(payload.runs || []).filter(function (r) {
      if (seen[r.session_id]) return false;
      seen[r.session_id] = true;
      return true;
    });
    state.total = typeof payload.total === "number" ? payload.total : state.runs.length;
    state.agents = payload.agents || state.agents;
    // Sessions the server could not read are counted, never silently missing (core.js).
    state.unreadable = unreadableNote(payload);
    STATE.runs = state.runs;
    var firstNew = more && payload.runs && payload.runs[0] ? payload.runs[0].session_id : null;
    renderRunsBody(state);
    if (firstNew) {
      var link = document.querySelector('tr[data-sid="' + cssEscape(firstNew) + '"] .run-link');
      if (link) link.focus();
    }
  }, function (err) {
    if (isSuperseded(err)) return;
    if (STATE.runsList !== state || seq !== state.seq) return;
    var current = el("runs-body");
    if (current) current.removeAttribute("aria-busy");
    if (err.status === 401 || !current) return;  // api() has shown the token page
    if (more) throw err;
    current.innerHTML = '<div class="notice err"><div class="notice-title">Could not load runs</div>' +
      '<div class="notice-body">' + esc(apiErrorText(err)) + "</div></div>";
  });
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, "\\$&");
}

function filtersActive(filters) {
  return !!(filters.q || filters.status || filters.agent);
}

function countText(state) {
  var total = state.total, shown = state.runs.length;
  var noun = filtersActive(state.filters) ? (total === 1 ? " matching run" : " matching runs") : (total === 1 ? " run" : " runs");
  if (shown >= total) return total.toLocaleString("en-US") + noun;
  return shown.toLocaleString("en-US") + " of " + total.toLocaleString("en-US") + noun + " loaded";
}

function renderRunsBody(state) {
  var body = el("runs-body");
  if (!body) return;
  body.removeAttribute("aria-busy");
  var runs = state.runs;
  updateAgentOptions(state);
  el("runs-count").textContent = countText(state);
  var filters = el("runs-filters");
  if (!runs.length && !filtersActive(state.filters)) {
    filters.hidden = true;
    body.innerHTML = state.unreadable +
      '<div class="empty">' +
      "<p><strong>No runs yet.</strong></p>" +
      "<p>Start one with <code>garuda run -t &quot;your task&quot;</code> and reload.</p>" +
      "<p class=\"stat-sub\">Reading from " +
      esc((STATE.health && STATE.health.sessions_root) || "the sessions directory") + "</p>" +
      (canRun() ? '<p><a class="btn btn-primary" href="#/chat">Talk to an agent</a></p>' : "") +
      "</div>";
    liveNote([]);
    return;
  }
  filters.hidden = false;
  if (!runs.length) {
    body.innerHTML = state.unreadable + '<div class="filter-empty" id="runs-none">No run matches these filters.</div>';
    liveNote([]);
    return;
  }
  var partial = runs.length < state.total;
  body.innerHTML = state.unreadable +
    (partial
      ? '<p class="stat-sub runs-window" id="runs-window">Tiles and charts cover the ' + runs.length +
        " newest of " + state.total.toLocaleString("en-US") + " runs; load older ones below the table.</p>"
      : "") +
    '<div class="grid cols-4" id="tiles">' + tilesHtml(runs, state.total) + "</div>" +
    '<div class="grid cols-4" id="charts">' + chartsHtml(runs) + "</div>" +
    '<div class="card table-wrap" id="table-card">' + tableHtml(runs) + moreHtml(state) + "</div>";
  var more = el("runs-more");
  if (more) {
    more.addEventListener("click", function () {
      guardAction(more, function () { return loadRuns(state, true); }, { busy: "loading…" });
    });
  }
  liveNote(runs);
}

function moreHtml(state) {
  var left = state.total - state.runs.length;
  if (left <= 0) return "";
  var next = Math.min(left, RUNS_PAGE_SIZE);
  return '<div class="runs-more"><button type="button" class="btn" id="runs-more">Load ' + next +
    " older run" + (next === 1 ? "" : "s") + "</button>" +
    '<span class="stat-sub">' + state.runs.length + " of " + state.total.toLocaleString("en-US") + " shown</span></div>";
}

/* The run list does NOT auto-refresh, deliberately.
 *
 * It used to, every 2.5 seconds, whenever any session's `meta.json` said `running`. Two
 * things made that indefensible. A run killed before it finished writing its meta says
 * `running` forever — four month-old sessions on this machine did — so the condition never
 * cleared and the page re-rendered until the tab was closed. And a full re-render throws
 * away scroll position, collapses anything open and repaints every chart, which is
 * disruptive even when the data genuinely changed.
 *
 * So refreshing the list is a button. Watching something happen live is the *run detail*
 * view's job, where there is one specific run to follow and re-rendering is the point. */
function liveNote(runs) {
  var live = runs.filter(function (r) { return r.live; });
  var note = el("runs-live-note");
  if (!note) return;
  if (!live.length) { note.textContent = ""; return; }
  // A live run is worth pointing at — but by offering the link, not by hijacking the page.
  note.innerHTML =
    live.length + " run" + (live.length === 1 ? "" : "s") + " in flight · " +
    '<a href="#/runs/' + esc(encodeURIComponent(live[0].session_id)) + '">follow ' +
    esc(fmt.truncate(live[0].task, 40)) + "</a>";
}

/* How a run ended, from the recorded outcome (B.2), never the legacy `status` word: a native
 * run records `success`, an external harness or a flow `completed`, a chat `finished`, for
 * the same outcome. `null` while the work is still active or the run never recorded one. */
function runOutcome(r) {
  return (r.state && r.state.outcome) || null;
}

/* Completed is how a run *ended*. It is not verification, which is a separate fact. */
function outcomeCounts(runs) {
  var finished = runs.filter(function (r) { return runOutcome(r) !== null; });
  return {
    finished: finished.length,
    completed: finished.filter(function (r) { return runOutcome(r) === "completed"; }).length,
    verified: finished.filter(function (r) {
      return r.state && r.state.verification && r.state.verification.status === "passed";
    }).length
  };
}

/* `total` is every run the filters match; the other tiles cover the loaded `runs` and the
 * first one says how many that is. */
function tilesHtml(runs, total) {
  total = typeof total === "number" ? total : runs.length;
  var counts = outcomeCounts(runs);
  var priced = runs.filter(function (r) { return typeof r.cost_usd === "number"; });
  var spend = priced.reduce(function (sum, r) { return sum + r.cost_usd; }, 0);
  // Tokens are summed over the runs that recorded them, and say how many did not: a run
  // with no token count is unknown, never zero.
  var counted = runs.filter(function (r) { return typeof r.total_tokens === "number"; });
  var tokens = counted.reduce(function (sum, r) { return sum + r.total_tokens; }, 0);
  var durations = runs
    .map(function (r) { return r.duration_ms; })
    .filter(function (d) { return typeof d === "number"; });
  var median = durations.length
    ? durations.slice().sort(function (a, b) { return a - b; })[Math.floor(durations.length / 2)]
    : null;
  // Cost is reported over the runs that could be priced, and says so — an
  // unpriceable run must not be silently counted as free.
  var unpriced = runs.length - priced.length;
  var untimed = runs.length - durations.length;

  return [
    tile("Runs", total.toLocaleString("en-US"),
         (runs.length < total ? runs.length + " loaded · " : "") + counts.finished + " finished"),
    tile("Completed", counts.finished ? Math.round((counts.completed / counts.finished) * 100) + "%" : "—",
         counts.completed + " of " + counts.finished + " finished · " + counts.verified + " verified"),
    tile("Spend", priced.length ? fmt.cost(spend) + (unpriced ? " known" : "") : "unknown",
         unpriced ? priced.length + " priced · " + unpriced + " unpriced" : priced.length + " priced"),
    tile("Median duration", fmt.duration(median),
         (untimed ? untimed + " untimed · " : "") +
         (counted.length ? fmt.tokens(tokens) + " tokens over " + counted.length + " run" + (counted.length === 1 ? "" : "s") +
                           (counted.length < runs.length ? " (" + (runs.length - counted.length) + " not recorded)" : "")
                         : "tokens not recorded"))
  ].join("");
}

function tile(label, value, sub) {
  return (
    '<div class="card" data-tile="' + esc(label) + '"><div class="stat-label">' + esc(label) + "</div>" +
    '<div class="stat-value">' + esc(value) + "</div>" +
    '<div class="stat-sub">' + esc(sub) + "</div></div>"
  );
}

function chartsHtml(runs) {
  // Oldest-first so the charts read left-to-right in time; the table stays newest-first.
  var series = runs.slice().reverse();
  // Only runs that recorded the measure are plotted; an unknown is left out, not drawn as 0.
  var costs = series.filter(function (r) { return typeof r.cost_usd === "number"; }).map(function (r) {
    return {
      value: r.cost_usd,
      cls: runOutcome(r) === "failed" ? "bar-err" : "",
      label: fmt.truncate(r.task, 60) + " — " + fmt.cost(r.cost_usd)
    };
  });
  var tokens = series.filter(function (r) {
    return typeof r.prompt_tokens === "number" || typeof r.completion_tokens === "number";
  }).map(function (r) {
    return {
      a: r.prompt_tokens || 0,
      b: r.completion_tokens || 0,
      label: fmt.tokens(r.prompt_tokens) + " in / " + fmt.tokens(r.completion_tokens) + " out"
    };
  });
  var durations = series
    .filter(function (r) { return typeof r.duration_ms === "number"; })
    .map(function (r) { return { value: r.duration_ms }; });
  var counts = outcomeCounts(runs);

  return [
    '<div class="card">' + bars(costs, { title: "Cost per run", emptyNote: "No run could be priced." }) + "</div>",
    '<div class="card">' + stack(tokens, { title: "Tokens", aLabel: "prompt", bLabel: "completion", emptyNote: "No token counts recorded." }) + "</div>",
    '<div class="card">' + line(durations, { title: "Duration", emptyNote: "Need at least two timed runs." }) + "</div>",
    '<div class="card">' + meter(counts.finished ? counts.completed / counts.finished : 0, {
      title: "Completed",
      caption: counts.completed + " / " + counts.finished + " finished runs completed (not verification)"
    }) + "</div>"
  ].join("");
}

/* The task cell carries the real links: the task to the trace (keyboard-reachable, opens in
 * a new tab, shows its URL) and, on a second line, the session's name to its session page.
 * At phone width the row stacks into labelled cells (data-label). */
function tableHtml(runs) {
  var head =
    '<thead><tr><th class="col-status">Status</th><th class="col-task">Task</th><th>Agent</th>' +
    "<th>Mode</th><th>Model</th>" +
    '<th class="num">Turns</th><th class="num">Tokens</th><th class="num">Cost</th>' +
    '<th class="num">Duration</th><th class="col-when">When</th></tr></thead>';
  var rows = runs.map(function (r) {
    var sid = encodeURIComponent(r.session_id);
    return (
      '<tr class="run-row" data-sid="' + esc(r.session_id) + '">' +
      '<td class="col-status" data-label="Status">' + runStatusPill(r) + "</td>" +
      '<td class="task" data-label="Task"><a class="run-link" href="#/runs/' + esc(sid) + '" title="' + esc(r.task) + '">' +
      esc(r.task ? fmt.truncate(r.task, 300) : "no task recorded") + "</a>" +
      '<div class="run-sub"><a class="run-session" href="#/sessions/' + esc(sid) + '" title="Session state, queue and control">' +
      esc(r.name || r.session_id.slice(0, 8)) + "</a></div></td>" +
      '<td class="mono" data-label="Agent">' + esc(r.agent || "—") + "</td>" +
      '<td class="mono" data-label="Mode">' + esc(r.mode || "—") + "</td>" +
      '<td class="mono col-model" data-label="Model" title="' + esc(r.model || "") + '">' + esc(fmt.truncate(r.model, 34) || "—") + "</td>" +
      '<td class="num" data-label="Turns">' + fmt.num(r.turns) + "</td>" +
      '<td class="num" data-label="Tokens">' + fmt.tokens(r.total_tokens) + "</td>" +
      '<td class="num" data-label="Cost">' + fmt.cost(r.cost_usd) + "</td>" +
      '<td class="num" data-label="Duration">' + fmt.duration(r.duration_ms) + "</td>" +
      '<td class="mono col-when" data-label="When">' + esc(fmt.when(r.updated_at)) + "</td>" +
      "</tr>"
    );
  });
  return '<table class="runs-table" id="runs-table">' + head + "<tbody>" + rows.join("") + "</tbody></table>";
}

/* `data-route-param` marks a control whose value lives in the URL: the router does not count
 * it as an unsubmitted edit, so `r` and a reconnect still refresh the page. */
function filtersHtml(state) {
  return (
    '<div class="filters" id="runs-filters">' +
    '<input type="search" id="f-q" data-route-param aria-label="Search runs by task or id" placeholder="Search task or id…" value="' +
    esc(state.filters.q) + '">' +
    '<select id="f-status" data-route-param aria-label="Filter runs by outcome">' +
    // Outcome labels: the server matches them against each run's recorded outcome, so
    // "completed" finds native `success` and external/flow `completed` runs alike.
    statusOption("", "Any status", state) + statusOption("completed", "completed", state) +
    statusOption("failed", "failed", state) + statusOption("cancelled", "cancelled", state) +
    statusOption("refused", "refused", state) + statusOption("running", "running", state) +
    "</select>" +
    '<select id="f-agent" data-route-param aria-label="Filter runs by agent">' + agentOptions(state) + "</select>" +
    "</div>"
  );
}

function agentNames(state) {
  var names = state.agents.slice();
  if (state.filters.agent && names.indexOf(state.filters.agent) === -1) names.push(state.filters.agent);
  return names;
}

function agentOptions(state) {
  return '<option value="">Any agent</option>' + agentNames(state).map(function (name) {
    var selected = state.filters.agent === name ? " selected" : "";
    return '<option value="' + esc(name) + '"' + selected + ">" + esc(name) + "</option>";
  }).join("");
}

/* The agent list comes with each response; the <select> keeps its node (and focus) and only
 * its options change, and only when the list did. */
function updateAgentOptions(state) {
  var select = el("f-agent");
  if (!select) return;
  var key = agentNames(state).join("\n");
  if (select.getAttribute("data-agents") === key) return;
  select.setAttribute("data-agents", key);
  select.innerHTML = agentOptions(state);
}

function statusOption(value, label, state) {
  var selected = (state.filters.status || "") === value ? " selected" : "";
  return '<option value="' + esc(value) + '"' + selected + ">" + esc(label) + "</option>";
}

/* Typing replaces the history entry (one per search, not per keystroke); choosing a status
 * or agent adds one, so Back undoes it. */
function wireFilters(state) {
  var search = el("f-q");
  var timer = null;
  search.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      if (!search.isConnected || STATE.runsList !== state) return;
      var q = search.value.trim();
      if (q === state.filters.q) return;
      state.filters.q = q;
      setRouteParams(state.filters);
      loadRuns(state);
    }, 220);
  });
  ["status", "agent"].forEach(function (key) {
    el("f-" + key).addEventListener("change", function () {
      state.filters[key] = this.value;
      setRouteParams(state.filters, { push: true });
      loadRuns(state);
    });
  });
}

/* Run detail lives in views_trajectory.js — it is the inspector, not a table. */


/* `meta.json` says `running` until a run finishes writing it, so a killed run claims to be
 * running forever. Rather than relabel it — the harness never recorded an outcome, and
 * inventing "failed" would be a lie of a different kind — say what is actually known: it
 * started, it stopped talking, nobody wrote down how it ended. */
function runStatusPill(run) {
  if (run.live) return '<span class="pill running">running</span>';
  // A flow paused on a resumable stop (the read model's `pause`), drawn as the Sessions list
  // draws it; its recorded outcome (failed) still answers the status filter and counts.
  if (run.pause) {
    return '<span class="pill paused" data-state="paused">paused</span>' + pauseLineHtml(run.pause, run.session_id);
  }
  if (run.stale) {
    return '<span class="pill unknown" title="meta.json still says running, but the log ' +
           'stopped being written ' + esc(fmt.duration((run.written_ago_seconds || 0) * 1000)) +
           ' ago — this run was killed before it could record an outcome">abandoned</span>';
  }
  return statusPill(run.status);
}
