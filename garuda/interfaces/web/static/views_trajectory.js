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
  render('<div class="loading">Loading run…</div>');
  return api(detailUrl)
    .then(function (payload) {
      STATE.run = payload;
      renderTrajectory(payload, sources);
    })
    .catch(function (err) {
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      render('<div class="notice err"><div class="notice-title">Could not load run</div>' +
             '<div class="notice-body">' + esc(err.message) + "</div></div>");
    });
}

function renderTrajectory(payload, sources) {
  var run = payload.run;
  var traj = payload.trajectory;
  var turns = traj.turns || [];
  var hasGates = turns.some(function (t) { return (t.gates || []).length; }) ||
                 (traj.phases || []).some(function (p) { return p.critic; });

  // Set before rendering, not after: `outputHtml` asks whether buffers exist while building
  // the step bodies, and reading it later would answer with the previous view's source.
  STATE.raw = {
    url: sources.events,
    buffers: sources.buffers || null,
    subagents: sources.subagents || null,
    total: payload.event_count, start: 0, count: 0, events: []
  };

  var live = isRunLive(payload);

  render(
    '<div class="page-head">' +
    '<a href="#/runs">← Runs</a>' +
    "<h1>" + esc(fmt.truncate(run.task, 90)) + "</h1>" +
    statusPill(run.status) +
    (live ? '<span class="pill live" id="live-badge">live</span>' : "") +
    '<span class="meta">' + esc(run.session_id) + "</span>" +
    "</div>" +
    (live ? liveStreamHtml() : "") +
    '<div class="grid cols-4">' + detailTilesHtml(payload) + "</div>" +
    runFlagsHtml(payload) +
    overviewHtml(traj) +
    '<div class="card">' + pressureChartHtml(payload) + "</div>" +
    (hasGates || traj.gate_stack ? '<div class="card">' + gateStackHtml(traj) + "</div>" : "") +
    timelineHeadHtml(turns) +
    '<div class="trajectory' + (hasGates ? " with-lane" : "") + '" id="traj">' +
    timelineHtml(traj, hasGates) +
    "</div>" +
    rawEventsHtml(payload)
  );

  el("traj").addEventListener("click", onTurnClick);
  wireRawEvents();
  wireTimelineHead();
  restoreOpenTurns(turns);
  // The invariant: a poller exists exactly when the rendered view is live. Deciding it here
  // rather than from the tail response means a run that finished between two polls stops
  // being polled as soon as it is drawn as finished.
  if (live && sources.tail) startLive(sources, payload);
  else stopLive();
}

/* `live`, not `status === "running"`: the status is a permanent lie for a killed run, and
 * trusting it started a tail poller on month-old sessions that never stopped. */
function isRunLive(payload) {
  return !!(payload.run && payload.run.live);
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
      '<a class="flow-row" href="#turn-' + turn.index + '" data-scroll="' + turn.index + '">' +
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

function liveStreamHtml() {
  return (
    '<div class="card live-card">' +
    '<h2>Live <span class="meta" id="live-meta">connecting…</span></h2>' +
    '<div id="live-body" class="live-body"><div class="stat-sub">Waiting for the next event…' +
    "</div></div></div>"
  );
}

function startLive(sources, payload) {
  // Stop whatever was polling first. A structure refresh re-renders, which lands back here —
  // and without this the new poller overwrote STATE.poller while the old one kept ticking,
  // unreachable and therefore unstoppable by stopLive(). One leaked poller per refresh.
  var previous = STATE.live;
  stopLive();
  var state = {
    sources: sources,
    offset: payload.run && typeof payload.run.events_bytes === "number"
      ? payload.run.events_bytes : 0,
    // Carried across the refresh: a stream pane that empties every three seconds is worse
    // than no stream pane.
    rows: previous ? previous.rows : [],
    empty: 0,
    eventCount: payload.event_count,
    sinceStructure: 0
  };
  STATE.live = state;
  STATE.poller = new Poller(function () { return livePoll(state); }, LIVE_INTERVAL_MS);
  STATE.poller.start();
  if (state.rows.length) renderLiveRows(state);
}

function livePoll(state) {
  var join = state.sources.tail.indexOf("?") === -1 ? "?" : "&";
  return api(state.sources.tail + join + "offset=" + state.offset)
    .then(function (payload) {
      if (STATE.live !== state) return;   // the view changed under us
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
      if (err.status === 401) { stopLive(); render(tokenRequiredPanel()); return; }
      setLiveMeta("poll failed: " + err.message);
    });
}

function liveMetaText(state, payload) {
  var behind = payload.size - state.offset;
  return state.rows.length + " events streamed · " +
         (behind > 0 ? fmt.tokens(behind) + " bytes pending" : "up to date") +
         " · polling every " +
         Math.round((STATE.poller.interval || LIVE_INTERVAL_MS) / 100) / 10 + "s";
}

function setLiveMeta(text) {
  var node = el("live-meta");
  if (node) node.textContent = text;
}

function renderLiveRows(state) {
  var node = el("live-body");
  if (!node) return;
  node.innerHTML = state.rows.slice().reverse().map(function (event) {
    var payload = event.payload === undefined ? event : event.payload;
    return '<div class="raw-row"><span class="raw-idx">' +
           esc(String(event.timestamp || "").slice(11, 19)) + "</span>" +
           '<span class="raw-type">' + esc(event.type) + "</span>" +
           '<span class="raw-payload mono">' +
           esc(fmt.truncate(JSON.stringify(payload), 220)) + "</span></div>";
  }).join("");
}

/* Refetch the turn structure and re-render. Turn grouping stays server-side in exactly one
 * place, so this is a full re-render — which is why which turns were expanded is recorded
 * first and reapplied after. A live view that keeps closing the turn you are reading is worse
 * than one that does not update. */
function refreshStructure(state, finished) {
  return api(state.sources.detail)
    .then(function (payload) {
      if (STATE.live !== state) return;
      if (!finished && payload.event_count === state.eventCount) {
        setLiveMeta(state.rows.length + " events streamed · structure unchanged");
        return;
      }
      state.eventCount = payload.event_count;
      rememberOpenTurns();
      var scroll = window.scrollY;
      STATE.run = payload;
      // renderTrajectory owns the poller either way: it starts one if the re-rendered view
      // is live and stops one if it is not, so "a poller exists" and "the view shows a live
      // badge" cannot disagree.
      renderTrajectory(payload, state.sources);
      window.scrollTo(0, scroll);
      if (finished) toast("Run finished — " + (payload.run.status || "done"));
    })
    .catch(function () { /* the tail keeps going; the structure catches up next time */ });
}

function stopLive() {
  if (STATE.poller) { STATE.poller.stop(); STATE.poller = null; }
  STATE.live = null;
}

/* --- expansion state across re-renders ---------------------------------- */

function rememberOpenTurns() {
  var open = {};
  var nodes = document.querySelectorAll(".turn.open");
  for (var i = 0; i < nodes.length; i++) open[nodes[i].getAttribute("data-turn")] = 1;
  STATE.openTurns = open;
}

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

function timelineHeadHtml(turns) {
  return (
    '<div class="page-head sub-head"><h2>Trace</h2>' +
    '<span class="meta">' + turns.length + (turns.length === 1 ? " turn" : " turns") + "</span>" +
    '<button type="button" class="btn" id="expand-all">Expand all</button>' +
    '<button type="button" class="btn" id="collapse-all">Collapse all</button></div>'
  );
}

function wireTimelineHead() {
  var expand = el("expand-all");
  var collapse = el("collapse-all");
  if (expand) expand.addEventListener("click", function () { setAllTurns(true); });
  if (collapse) collapse.addEventListener("click", function () { setAllTurns(false); });
  var flow = el("flow");
  if (flow) {
    flow.addEventListener("click", function (event) {
      var row = event.target.closest("[data-scroll]");
      if (!row) return;
      event.preventDefault();
      var index = row.getAttribute("data-scroll");
      setTurnOpen(index, true);
      var card = document.querySelector('.turn[data-turn="' + index + '"]');
      if (card) card.scrollIntoView({ block: "center" });
    });
  }
}

/* --- tiles and run-level flags -------------------------------------------- */

function detailTilesHtml(payload) {
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
    tile("Cost", fmt.cost(traj.cost_usd), esc(traj.model || run.model || "—")),
    tile("Tool calls", fmt.num(toolCount), errors ? errors + " errored" : "none errored")
  ].join("");
}

/* A log with no turn structure at all — the 1.1.0 vintage most archived runs are written in.
 * `finished` is meaningless there, because nothing in the log could ever have set it, so every
 * per-turn completion flag must be suppressed. Raising "TURN NEVER FINISHED" on all of them
 * would be a false alarm on the majority of real data. */
function isLegacyLog(traj) {
  return (traj.warning_codes || []).indexOf("legacy_log_format") !== -1;
}

/* Everything that says "look here first". A killed run, a rejected gate and an unpriceable
 * cost are all things you want to see before scrolling. */
function runFlagsHtml(payload) {
  var traj = payload.trajectory;
  var legacy = isLegacyLog(traj);
  var notes = [];

  if (!legacy && (traj.success === null || traj.success === undefined)) {
    notes.push(["err", "No session_end in the log",
                "The run was killed, or is still in flight. This is not a failure — " +
                "the harness never got to record one."]);
  }
  var unfinished = legacy ? [] : (traj.turns || []).filter(function (t) { return !t.finished; });
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
    if (seen[anomaly.code]) return;
    seen[anomaly.code] = 1;
    var where = anomaly.turn_index !== null && anomaly.turn_index !== undefined
      ? " (turn band " + (anomaly.turn_index + 1) + ")" : "";
    notes.push(["", "Reader note · " + anomaly.code, esc(anomaly.message) + esc(where)]);
  });

  return notes.map(function (note) {
    return '<div class="notice' + (note[0] ? " " + note[0] : "") + '">' +
           '<div class="notice-title">' + esc(note[1]) + "</div>" +
           '<div class="notice-body">' + note[2] + "</div></div>";
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

function timelineHtml(traj, hasGates) {
  var turns = traj.turns || [];
  if (!turns.length) {
    return '<div class="empty span-all"><p>No turns in this log.</p>' +
           "<p>Either the run failed before its first model call, or the log is from a " +
           "build that did not record enough to segment.</p></div>";
  }
  var phases = traj.phases || [];
  var showPhases = phases.length > 1;
  var legacy = isLegacyLog(traj);
  var out = "";
  for (var i = 0; i < turns.length; i++) {
    if (showPhases) {
      out += phases.filter(function (p) { return p.turn_start === i; })
                   .map(phaseHeaderHtml).join("");
    }
    out += turnCardHtml(turns[i], turns[i - 1], traj, legacy);
    if (hasGates) out += gateCellHtml(turns[i]);
    if (showPhases) {
      // `turn_end` is EXCLUSIVE — Phase(0, 3) covers bands 0..2 — so the critic strip belongs
      // after band turn_end-1. Comparing it to `i` directly would render the critic one row
      // into the *next* phase.
      out += phases.filter(function (p) { return p.critic && p.turn_end === i + 1; })
                   .map(phaseCriticHtml).join("");
    }
  }
  return out;
}

function phaseHeaderHtml(phase) {
  var count = phase.turn_end - phase.turn_start;
  return (
    '<div class="phase-head span-all">' +
    '<span class="phase-kind">' + esc(phase.kind) + "</span>" +
    (phase.attempt !== null && phase.attempt !== undefined
      ? '<span class="meta">round ' + esc(phase.attempt) + "</span>" : "") +
    '<span class="meta">' +
    (count === 1 ? "1 band" : "bands " + (phase.turn_start + 1) + "–" + phase.turn_end) +
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

function turnCardHtml(turn, previous, traj, legacy) {
  var steps = turnSteps(turn);
  var fraction = turn.budget && typeof turn.budget.fraction === "number"
    ? Math.round(turn.budget.fraction * 100) + "%" : "—";

  return (
    '<div class="turn" data-turn="' + turn.index + '" id="turn-' + turn.index + '">' +
    '<button type="button" class="turn-head" data-toggle="' + turn.index + '">' +
    '<span class="turn-no">' + esc(turnNumber(turn)) + "</span>" +
    '<span class="turn-flags">' + turnFlagsHtml(turn, steps, legacy) + "</span>" +
    '<span class="turn-stats">' +
    turnStat("model", fmt.duration(turn.model_ms)) +
    turnStat("tools", String(steps.length)) +
    turnStat("tokens", fmt.tokens(turn.prompt_tokens)) +
    turnStat("ctx", fraction) +
    turnStat("cost", fmt.cost(turn.cost_usd)) +
    "</span>" +
    '<span class="turn-caret">▸</span>' +
    "</button>" +
    '<div class="turn-body" id="turn-body-' + turn.index + '" hidden>' +
    turnBodyHtml(turn, previous, traj, steps) +
    "</div></div>"
  );
}

function turnStat(label, value) {
  return '<span class="turn-stat"><span class="turn-stat-k">' + esc(label) + "</span>" +
         '<span class="turn-stat-v">' + esc(value) + "</span></span>";
}

function turnFlagsHtml(turn, steps, legacy) {
  var flags = [];
  if (turn.label) flags.push(["accent", turn.label]);
  if (!turn.finished && !legacy) flags.push(["err", "never finished"]);
  var pending = steps.filter(function (s) { return s.status === "pending"; }).length;
  if (pending) flags.push(["err", pending + " pending"]);
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
function turnBodyHtml(turn, previous, traj, steps) {
  var out = inputHtml(turn, previous, traj);

  (turn.model_calls || []).forEach(function (call, index) {
    if (call.truncated) {
      out += '<div class="notice err"><div class="notice-title">Response truncated</div>' +
             '<div class="notice-body">The provider cut this response at max_tokens. Any ' +
             "tool call it was mid-way through writing is not in the log.</div></div>";
    }
    var suffix = (turn.model_calls.length > 1) ? " · call " + (index + 1) : "";
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
           steps.map(function (step) { return stepHtml(step, turn); }).join("");
  }

  if ((turn.events || []).length) {
    out += '<div class="stat-sub">events ' + turn.events[0] + "–" +
           turn.events[turn.events.length - 1] + " of the log · " +
           '<a href="#" data-jump="' + turn.events[0] + '">show raw</a></div>';
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

function stepHtml(step, turn) {
  var cls = STEP_CLASS[step.status] || "unknown";
  var family = toolFamily(step.name);
  var why = STEP_WHY[step.status];
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
      ? '<span class="meta">' + handoff.turns + " turns inside</span>" : "") +
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
  api(STATE.raw.subagents + encodeURIComponent(subSessionId))
    .then(function (payload) {
      node.innerHTML = subTraceHtml(payload);
    })
    .catch(function (err) {
      node.innerHTML = '<div class="notice"><div class="notice-body">' +
                       esc(err.status === 404
                         ? "No trace was kept for this subagent. Runs from before subagent " +
                           "logs were persisted only recorded the handoff."
                         : err.message) + "</div></div>";
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
    fmt.tokens((traj.usage || {}).prompt_tokens) + " prompt tokens · " +
    fmt.cost(traj.cost_usd) + " · " + esc(traj.model || "?") + "</div>" +
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
  if (!STATE.raw || !STATE.raw.buffers) {
    toast("Buffers are only stored beside a local session.");
    return;
  }
  api(STATE.raw.buffers + encodeURIComponent(bufferId))
    .then(function (payload) {
      var node = el("raw-body");
      node.innerHTML =
        '<div class="block-label">buffer ' + esc(bufferId) + " · " + payload.bytes + " bytes" +
        (payload.truncated ? " (clipped)" : "") + "</div><pre>" + esc(payload.content) + "</pre>";
      el("raw-card").scrollIntoView({ block: "start" });
    })
    .catch(function (err) { toast(err.message); });
}

/* --- the raw-event escape hatch ------------------------------------------ */

function rawEventsHtml(payload) {
  return (
    '<div class="card" id="raw-card">' +
    "<h2>Raw events</h2>" +
    '<div class="stat-sub">' + payload.event_count + " events in the log. Everything above " +
    "is derived from these; this is the check on the derivation.</div>" +
    '<div class="filters">' +
    '<input type="search" id="raw-filter" placeholder="Filter by type or text…">' +
    '<button type="button" class="btn" id="raw-load">Load events</button>' +
    "</div>" +
    '<div id="raw-body"></div></div>'
  );
}

function wireRawEvents() {
  el("raw-load").addEventListener("click", function () { loadRawEvents(STATE.raw.start); });
  el("raw-filter").addEventListener("input", function () { renderRawEvents(); });
}

function loadRawEvents(start) {
  var raw = STATE.raw;
  el("raw-body").innerHTML = '<div class="loading">Loading events…</div>';
  var join = raw.url.indexOf("?") === -1 ? "?" : "&";
  return api(raw.url + join + "start=" + Math.max(0, start | 0))
    .then(function (payload) {
      raw.start = payload.start;
      raw.count = payload.count;
      raw.total = payload.total;
      raw.events = payload.events;
      renderRawEvents();
      el("raw-card").scrollIntoView({ block: "start" });
    })
    .catch(function (err) {
      el("raw-body").innerHTML = '<div class="notice err"><div class="notice-body">' +
                                 esc(err.message) + "</div></div>";
    });
}

function renderRawEvents() {
  var raw = STATE.raw;
  var needle = (el("raw-filter").value || "").toLowerCase();
  var rows = [];
  for (var i = 0; i < raw.events.length; i++) {
    var event = raw.events[i];
    var text = JSON.stringify(event.payload === undefined ? event : event.payload);
    if (needle && (event.type || "").toLowerCase().indexOf(needle) === -1 &&
        text.toLowerCase().indexOf(needle) === -1) {
      continue;
    }
    rows.push(
      '<div class="raw-row"><span class="raw-idx">' + (raw.start + i) + "</span>" +
      '<span class="raw-type">' + esc(event.type) + "</span>" +
      '<span class="raw-payload mono">' + esc(fmt.truncate(text, 400)) + "</span></div>"
    );
  }
  var end = raw.start + raw.count;
  var pager =
    '<div class="filters">' +
    (raw.start > 0
      ? '<button type="button" class="btn" data-page="' +
        Math.max(0, raw.start - 500) + '">← previous</button>' : "") +
    (end < raw.total
      ? '<button type="button" class="btn" data-page="' + end + '">next →</button>' : "") +
    '<span class="meta">' + (raw.start + 1) + "–" + end + " of " + raw.total + "</span></div>";
  var body = el("raw-body");
  body.innerHTML =
    (rows.length ? rows.join("") : '<div class="stat-sub">Nothing matches.</div>') + pager;
  var buttons = body.querySelectorAll("[data-page]");
  for (var b = 0; b < buttons.length; b++) {
    buttons[b].addEventListener("click", function () {
      loadRawEvents(Number(this.getAttribute("data-page")));
    });
  }
}
