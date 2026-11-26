/* The runs list: every past run, with the stats over them.
 *
 * The browser replacement for `python -m garuda.eval.dashboard`, plus four charts over the
 * same rows. Clicking a row opens its trace. */

"use strict";

/* Whether this dashboard can talk to an agent. The UI hides controls off the server's
 * capability report rather than showing buttons that 503. */
function canRun() {
  return !!(STATE.health && STATE.health.capabilities && STATE.health.capabilities.chat);
}

function runsView() {
  render('<div class="loading">Loading runs…</div>');
  var params = new URLSearchParams();
  if (STATE.filters.q) params.set("q", STATE.filters.q);
  if (STATE.filters.status) params.set("status", STATE.filters.status);
  if (STATE.filters.agent) params.set("agent", STATE.filters.agent);
  var suffix = params.toString() ? "?" + params.toString() : "";

  return api("/api/runs" + suffix)
    .then(function (payload) {
      STATE.runs = payload.runs;
      renderRuns(payload);
    })
    .catch(function (err) {
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      render('<div class="notice err"><div class="notice-title">Could not load runs</div>' +
             '<div class="notice-body">' + esc(err.message) + "</div></div>");
    });
}

function renderRuns(payload) {
  var runs = payload.runs;
  if (!runs.length && !STATE.filters.q && !STATE.filters.status) {
    render(
      '<div class="empty">' +
      "<p><strong>No runs yet.</strong></p>" +
      "<p>Start one with <code>garuda run -t &quot;your task&quot;</code> and reload.</p>" +
      "<p class=\"stat-sub\">Reading from " +
      esc((STATE.health && STATE.health.sessions_root) || "the sessions directory") + "</p>" +
      (canRun()
        ? '<p><a class="btn btn-primary" href="#/chat">Talk to an agent</a></p>'
        : "") +
      "</div>"
    );
    return;
  }

  render(
    '<div class="page-head"><h1>Runs</h1>' +
    '<span class="meta">' + runs.length + " of " + payload.total + "</span>" +
    '<span class="meta" id="runs-live-note"></span>' +
    '<button type="button" class="btn" id="runs-refresh">Refresh</button>' +
    (canRun() ? '<a class="btn btn-primary" href="#/chat">Talk to an agent</a>' : "") +
    "</div>" +
    filtersHtml() +
    '<div class="grid cols-4" id="tiles"></div>' +
    '<div class="grid cols-4" id="charts"></div>' +
    '<div class="card table-wrap" id="table-card"></div>'
  );

  el("tiles").innerHTML = tilesHtml(runs);
  el("charts").innerHTML = chartsHtml(runs);
  el("table-card").innerHTML = tableHtml(runs);
  wireFilters();

  el("table-card").addEventListener("click", function (event) {
    var row = event.target.closest("tr[data-sid]");
    if (row) location.hash = "#/runs/" + row.getAttribute("data-sid");
  });
  wireRefresh(runs);
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
function wireRefresh(runs) {
  var button = el("runs-refresh");
  if (button) {
    button.addEventListener("click", function () { runsView(); });
  }
  var live = runs.filter(function (r) { return r.live; });
  var note = el("runs-live-note");
  if (!note) return;
  if (!live.length) { note.textContent = ""; return; }
  // A live run is worth pointing at — but by offering the link, not by hijacking the page.
  note.innerHTML =
    live.length + " run" + (live.length === 1 ? "" : "s") + " in flight · " +
    '<a href="#/runs/' + esc(live[0].session_id) + '">follow ' +
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

function tilesHtml(runs) {
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
    tile("Runs", String(runs.length), counts.finished + " finished"),
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

function tableHtml(runs) {
  var head =
    "<thead><tr><th>Status</th><th>Task</th><th>Agent</th>" +
    "<th>Mode</th><th>Model</th>" +
    '<th class="num">Turns</th><th class="num">Tokens</th><th class="num">Cost</th>' +
    '<th class="num">Duration</th><th>When</th></tr></thead>';
  var rows = runs.map(function (r) {
    return (
      '<tr data-sid="' + esc(r.session_id) + '">' +
      "<td>" + runStatusPill(r) + "</td>" +
      '<td class="task" title="' + esc(r.task) + '">' + esc(fmt.truncate(r.task, 90)) + "</td>" +
      '<td class="mono">' + esc(r.agent || "—") + "</td>" +
      '<td class="mono">' + esc(r.mode || "—") + "</td>" +
      '<td class="mono">' + esc(fmt.truncate(r.model, 34)) + "</td>" +
      '<td class="num">' + fmt.num(r.turns) + "</td>" +
      '<td class="num">' + fmt.tokens(r.total_tokens) + "</td>" +
      '<td class="num">' + fmt.cost(r.cost_usd) + "</td>" +
      '<td class="num">' + fmt.duration(r.duration_ms) + "</td>" +
      '<td class="mono">' + esc(fmt.when(r.updated_at)) + "</td>" +
      "</tr>"
    );
  });
  return "<table>" + head + "<tbody>" + rows.join("") + "</tbody></table>";
}

function filtersHtml() {
  var agents = {};
  STATE.runs.forEach(function (r) { if (r.agent) agents[r.agent] = 1; });
  var options = Object.keys(agents).sort().map(function (name) {
    var selected = STATE.filters.agent === name ? " selected" : "";
    return '<option value="' + esc(name) + '"' + selected + ">" + esc(name) + "</option>";
  });
  return (
    '<div class="filters">' +
    '<input type="search" id="f-q" placeholder="Search task or id…" value="' +
    esc(STATE.filters.q || "") + '">' +
    '<select id="f-status">' +
    // Outcome labels: the server matches them against each run's recorded outcome, so
    // "completed" finds native `success` and external/flow `completed` runs alike.
    statusOption("", "Any status") + statusOption("completed", "completed") +
    statusOption("failed", "failed") + statusOption("cancelled", "cancelled") +
    statusOption("refused", "refused") + statusOption("running", "running") +
    "</select>" +
    '<select id="f-agent"><option value="">Any agent</option>' + options.join("") + "</select>" +
    "</div>"
  );
}

function statusOption(value, label) {
  var selected = (STATE.filters.status || "") === value ? " selected" : "";
  return '<option value="' + esc(value) + '"' + selected + ">" + esc(label) + "</option>";
}

function wireFilters() {
  var search = el("f-q");
  var timer = null;
  search.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      STATE.filters.q = search.value.trim();
      runsView();
    }, 220);
  });
  el("f-status").addEventListener("change", function () {
    STATE.filters.status = this.value;
    runsView();
  });
  el("f-agent").addEventListener("change", function () {
    STATE.filters.agent = this.value;
    runsView();
  });
}

/* Run detail lives in views_trajectory.js — it is the inspector, not a table. */


/* `meta.json` says `running` until a run finishes writing it, so a killed run claims to be
 * running forever. Rather than relabel it — the harness never recorded an outcome, and
 * inventing "failed" would be a lie of a different kind — say what is actually known: it
 * started, it stopped talking, nobody wrote down how it ended. */
function runStatusPill(run) {
  if (run.live) return '<span class="pill running">running</span>';
  if (run.stale) {
    return '<span class="pill unknown" title="meta.json still says running, but the log ' +
           'stopped being written ' + esc(fmt.duration((run.written_ago_seconds || 0) * 1000)) +
           ' ago — this run was killed before it could record an outcome">abandoned</span>';
  }
  return statusPill(run.status);
}
