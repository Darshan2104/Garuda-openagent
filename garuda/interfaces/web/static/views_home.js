/* Overview: the landing page. What is happening, and does anything need me?
 *
 * Four sections, each fed and refreshed on its own so a slow or failing read never blanks
 * the page:
 *
 *   /api/overview   (fast) needs attention (sessions), running now, recent
 *   /api/setup      (slow) configuration errors, and which harnesses the roles use
 *   /api/providers  (slow) each harness's availability and recorded login reading
 *
 * The session rows are the shared read model's, selected on the server from every session
 * (not the newest page /api/sessions returns), so an old crashed or waiting session is never missed. Nothing is
 * computed here that the read model does not say: unknown stays "unknown", a login reading
 * shows its age, and a review is never shown as verification.
 *
 * Re-rendering patches each list by row key, so a poll leaves scroll and focus alone and an
 * unchanged row is never touched. Both pollers live on HOME and stop on navigation. */

"use strict";

var HOME = null;
// Globals rather than constants so the browser check can shorten them.
var HOME_FAST_MS = 10000;
var HOME_SLOW_MS = 60000;

function stopHomeView() {
  if (!HOME) return;
  HOME.fast.stop();
  HOME.slow.stop();
  HOME = null;
}

function overviewView() {
  stopHomeView();
  var state = { overview: null, overviewError: null, setup: null, setupError: null,
                setupAt: null, providers: null, providersError: null, providersAt: null };
  render(
    '<div id="home-root">' +
    '<div class="page-head"><h1>Overview</h1><span class="meta" id="home-updated"></span></div>' +
    '<div id="home-unreadable"></div>' +
    '<section class="card home-section" id="home-attention" aria-labelledby="home-attention-h">' +
    '<h2 id="home-attention-h">Needs attention</h2><ul class="home-list" id="home-attention-list"></ul>' +
    '<div class="home-status" id="home-attention-status"></div></section>' +
    '<section class="card home-section" id="home-running" aria-labelledby="home-running-h">' +
    '<h2 id="home-running-h">Running now <span class="meta" id="home-running-count"></span></h2>' +
    '<ul class="home-list" id="home-running-list"></ul><div class="home-status" id="home-running-status"></div></section>' +
    '<section class="card home-section" id="home-recent" aria-labelledby="home-recent-h">' +
    '<h2 id="home-recent-h">Recent</h2><ul class="home-list" id="home-recent-list"></ul>' +
    '<div class="home-status" id="home-recent-status"></div>' +
    '<p class="home-foot"><a href="#/sessions">All sessions</a></p></section>' +
    '<section class="card home-section" id="home-harnesses" aria-labelledby="home-harnesses-h">' +
    '<h2 id="home-harnesses-h">Harness health</h2><ul class="home-list" id="home-harness-list"></ul>' +
    '<div class="home-status" id="home-harness-status"></div>' +
    '<p class="home-foot"><a href="#/providers">Providers</a></p></section>' +
    "</div>"
  );
  el("home-root").addEventListener("click", function (event) {
    var button = event.target.closest("button[data-home-retry]");
    if (!button || HOME !== state) return;
    var which = button.getAttribute("data-home-retry");
    button.disabled = true;
    // Let the next render redraw this status even if the retry fails the same way.
    var holder = button.closest(".home-status");
    if (holder) holder.homeHtml = null;
    if (which === "overview") homeLoadOverview(state);
    if (which === "setup" || which === "slow") homeLoadSetup(state);
    if (which === "providers" || which === "slow") homeLoadProviders(state);
  });
  HOME = state;
  homeRenderAll(state);
  state.fast = new Poller(function () { return homeLoadOverview(state); }, HOME_FAST_MS).start();
  state.slow = new Poller(function () {
    return Promise.all([homeLoadSetup(state), homeLoadProviders(state)]);
  }, HOME_SLOW_MS).start();
}

/* --- loading: each read settles into its own slot, then the page re-renders ----------- */

function homeLoad(state, path, key) {
  return api(path).then(function (payload) {
    if (HOME !== state) return;
    state[key] = payload;
    state[key + "Error"] = null;
    state[key + "At"] = Date.now();
    homeRenderAll(state);
  }, function (err) {
    if (isSuperseded(err)) return;
    if (HOME !== state) return;
    if (err.status === 401) { stopHomeView(); render(tokenRequiredPanel()); return; }
    // A failed read clears its slot: showing the last answer under an error would let a
    // stale "nothing is running" pass for the current one.
    state[key] = null;
    state[key + "Error"] = err;
    homeRenderAll(state);
  });
}

function homeLoadOverview(state) { return homeLoad(state, "/api/overview", "overview"); }
function homeLoadSetup(state) { return homeLoad(state, "/api/setup", "setup"); }
function homeLoadProviders(state) { return homeLoad(state, "/api/providers", "providers"); }

/* --- in-place updates -------------------------------------------------------------------- */

/* Make `list` hold exactly `items` ({key, html}) in order, touching only rows whose HTML
 * changed: an unchanged row keeps its node, so focus and hover survive a poll. */
function homePatchList(list, items) {
  if (!list) return;
  var existing = {};
  Array.prototype.forEach.call(list.children, function (node) {
    existing[node.getAttribute("data-key")] = node;
  });
  var previous = null;
  items.forEach(function (item) {
    var node = existing[item.key];
    delete existing[item.key];
    if (!node || node.homeHtml !== item.html) {
      var template = document.createElement("template");
      template.innerHTML = item.html;
      var fresh = template.content.firstElementChild;
      fresh.homeHtml = item.html;
      if (node) list.replaceChild(fresh, node);
      node = fresh;
    }
    var wanted = previous ? previous.nextSibling : list.firstChild;
    if (node !== wanted) list.insertBefore(node, wanted);
    previous = node;
  });
  Object.keys(existing).forEach(function (key) { existing[key].remove(); });
}

/* A fold row rebuilt by a refresh (its rows changed) reopens if the user had it open. */
function homeApplyFolds(list) {
  if (!list) return;
  if (!list.homeFoldWatch) {
    list.homeFoldWatch = true;
    // `toggle` does not bubble: listen in the capture phase.
    list.addEventListener("toggle", function (event) {
      var fold = event.target.getAttribute && event.target.getAttribute("data-fold");
      if (fold) HOME_FOLDS_OPEN[fold] = event.target.open;
    }, true);
  }
  list.querySelectorAll("details[data-fold]").forEach(function (d) {
    var want = !!HOME_FOLDS_OPEN[d.getAttribute("data-fold")];
    if (d.open !== want) d.open = want;
  });
}

function homeSet(node, html) {
  if (node && node.homeHtml !== html) { node.innerHTML = html; node.homeHtml = html; }
}

/* --- formatting ---------------------------------------------------------------------------- */

function homePlural(count, word) { return count + " " + word + (count === 1 ? "" : "s"); }

/* Whole minutes, so a running row's text changes at most once a minute rather than on every
 * poll. Unknown when no start time was recorded. */
function homeElapsed(iso) {
  var then = iso ? new Date(iso).getTime() : NaN;
  if (isNaN(then)) return "elapsed unknown";
  var minutes = Math.max(0, Math.floor((Date.now() - then) / 60000));
  if (minutes < 1) return "for under a minute";
  if (minutes < 60) return "for " + minutes + "m";
  var hours = Math.floor(minutes / 60);
  if (hours < 24) return "for " + hours + "h " + (minutes % 60) + "m";
  return "for " + Math.floor(hours / 24) + "d " + (hours % 24) + "h";
}

function homeEpochWhen(epoch) {
  return epoch ? fmt.when(new Date(epoch * 1000).toISOString()) : "—";
}

function homeSessionLink(row) {
  return '<a class="home-name" href="#/sessions/' + encodeURIComponent(row.session_id) + '">' +
    esc(row.name || String(row.session_id).slice(0, 8)) + "</a>";
}

function homeStatus(kind, html) {
  return '<div class="home-' + kind + '">' + html + "</div>";
}

function homeError(what, err, retry) {
  return '<div class="home-error" role="alert" data-home-error="' + esc(retry) + '">' +
    '<span class="pill failed">error</span> Could not load ' + esc(what) + ": " +
    esc(err && err.message ? err.message : "unavailable") +
    ' <button type="button" class="btn btn-small" data-home-retry="' + esc(retry) + '">Retry</button></div>';
}

/* A running flow's step in flight, from the shared live view (`in_flight`): its turn count
 * and last activity, as recorded; nothing when the reader has neither. */
function homeLiveText(live) {
  if (!live || live.paused || live.running === false) return "";
  var parts = [];
  if (typeof live.turns === "number") parts.push("turn " + live.turns);
  if (live.last_activity_at) parts.push("last activity " + fmt.when(live.last_activity_at));
  return parts.length ? ' · <span class="home-live" data-live="flow">' + esc(parts.join(" · ")) + "</span>" : "";
}

/* The step sessions a running flow launched, nested under it (linked by their recorded
 * `flow_step`, or the flow's live view naming them), instead of rows of their own. */
function homeChildrenHtml(children) {
  if (!children || !children.length) return "";
  return '<ul class="home-children">' + children.map(function (c) {
    var link = c.flow_step || {};
    return '<li class="home-child" data-session="' + esc(c.session_id) + '">↳ ' + homeSessionLink(c) +
      ' <span class="pill ' + (c.state && c.state.work === "working" ? "running" : "unknown") + '" data-state="' + esc(c.label) + '">' + esc(c.label) + "</span>" +
      ' <span class="stat-sub">' + (link.step ? "step " + esc(link.step) + (link.attempt ? " (attempt " + esc(link.attempt) + ")" : "") + " · " : "") +
      esc(identityText(c)) + "</span></li>";
  }).join("") + "</ul>";
}

/* --- needs attention: older rows fold ------------------------------------------------------
 * Rows older than a day fold into one expandable line per kind, newest first; nothing is
 * hidden or deleted, and a fold the user opened stays open across refreshes. */
var HOME_FOLD_AGE_MS = 24 * 3600 * 1000;
var HOME_FOLDS_OPEN = {};
var HOME_FOLD_WORDS = { crashed: "crashed session", waiting: "waiting session", paused: "paused flow" };

function homeFoldOld(items, rows, kind) {
  var now = Date.now();
  var fresh = [], old = [];
  rows.forEach(function (pair) {
    var at = new Date(pair.row.updated_at).getTime();
    (!isNaN(at) && now - at > HOME_FOLD_AGE_MS ? old : fresh).push(pair.item);
  });
  fresh.forEach(function (item) { items.push(item); });
  if (!old.length) return;
  var inner = old.map(function (item) { return item.html; }).join("");
  items.push({ key: "fold:" + kind, html: '<li class="home-row home-fold" data-key="fold:' + esc(kind) + '" data-kind="' + esc(kind) + '">' +
    '<details class="home-fold-details" data-fold="' + esc(kind) + '"><summary>' +
    esc(homePlural(old.length, HOME_FOLD_WORDS[kind] || kind)) + " older than a day</summary>" +
    '<ul class="home-list home-fold-list">' + inner + "</ul></details></li>" });
}

function homeRow(key, kind, html) {
  return { key: key, html: '<li class="home-row" data-key="' + esc(key) + '" data-kind="' + esc(kind) + '">' + html + "</li>" };
}

/* --- harness health: which harnesses the roles use, joined with the providers source ------ */

/* The recorded login reading of one harness and when it was taken, from the /api/providers
 * card. One function on purpose: if the login source changes, this is the only place to
 * change. `checked_at` is epoch seconds or null; null means no reading was ever recorded. */
function homeLoginReading(card) {
  var login = (card && card.login) || {};
  return { state: login.state || "unknown", checkedAt: login.checked_at || null };
}

function homeLoginText(reading) {
  if (reading.state === "not_checked") return "login not checked";
  var label = reading.state === "authenticated" ? "logged in" : String(reading.state).replace(/_/g, " ");
  return label + " · " + (reading.checkedAt ? "checked " + homeEpochWhen(reading.checkedAt) : "check time unknown");
}

/* `null` until both slow reads answered. Harnesses come from the role table: each role's
 * harness and its fallback chain. The built-in native runtime has no login to show. */
function homeHarnesses(state) {
  if (!state.setup || !state.providers) return null;
  var used = {}, order = [];
  (state.setup.roles || []).forEach(function (role) {
    var chain = [{ harness: role.harness, fallback: false }].concat((role.fallback || []).map(function (f) {
      return { harness: f.harness, fallback: true };
    }));
    chain.forEach(function (entry) {
      if (!entry.harness || entry.harness === "native") return;
      if (!used[entry.harness]) { used[entry.harness] = []; order.push(entry.harness); }
      used[entry.harness].push(role.role + (entry.fallback ? " (fallback)" : ""));
    });
  });
  var cards = {};
  (state.providers.harnesses || []).forEach(function (card) { cards[card.id] = card; });
  return order.sort().map(function (id) {
    var card = cards[id] || null;
    var reading = homeLoginReading(card);
    return { id: id, roles: used[id], card: card, reading: reading,
             unavailable: card ? card.available === false : true,
             loggedOut: reading.state === "logged_out" };
  });
}

function homeAvailabilityPill(h) {
  if (!h.card) return '<span class="pill failed">not in catalog</span>';
  if (h.card.available === true) return '<span class="pill success">available</span>';
  if (h.card.available === false) return '<span class="pill failed">unavailable</span>';
  return '<span class="pill unknown">availability unknown</span>';
}

function homeHarnessItems(harnesses) {
  return harnesses.map(function (h) {
    var login = h.card ? homeLoginText(h.reading) : "login unknown";
    return homeRow("harness:" + h.id, "harness",
      '<a class="home-name" href="#/providers">' + esc(h.id) + "</a> " + homeAvailabilityPill(h) +
      ' <span class="pill ' + (h.reading.state === "authenticated" ? "success" : (h.loggedOut ? "failed" : "unknown")) +
      '" data-login="' + esc(h.reading.state) + '">' + esc(login) + "</span>" +
      '<span class="home-what stat-sub">used by ' + esc(h.roles.join(", ")) + "</span>");
  });
}

/* --- needs attention --------------------------------------------------------------------- */

function homeSessionAttention(o) {
  var items = [];
  var a = o.approvals;
  if (a.waiting) {
    var waitingRows = o.attention.waiting.items.filter(function (r) { return r.approvals_live; });
    var oldest = waitingRows.map(function (r) { return r.updated_at; }).sort()[0];
    items.push(homeRow("approvals", "approvals",
      '<span class="pill running">approvals</span> <a class="home-name" href="#/inbox">' +
      esc(homePlural(a.waiting, "approval")) + " waiting</a>" +
      '<span class="home-what">in ' + esc(homePlural(a.sessions, "session")) +
      (a.expired ? " · " + esc(a.expired) + " expired (they will be denied)" : "") + "</span>" +
      '<span class="home-age">' + (oldest ? "session updated " + esc(fmt.when(oldest)) : "") + "</span>"));
  }
  var crashedRows = [], waitingRows2 = [], pausedRows = [];
  o.attention.crashed.items.forEach(function (r) {
    // A dashboard flow whose dashboard died mid-step: what the crash proof found, and Resume
    // when it holds (the read model's facts; nothing judged here).
    var died = dashboardCrashPill(r);
    if (died) {
      var crash = dashboardCrash(r);
      var check = crash.resume || {};
      crashedRows.push({ row: r, item: homeRow("crashed:" + r.session_id, "crashed",
        '<span class="pill ' + died.cls + '" data-state="dashboard-died">' + esc(died.label) + "</span> " + homeSessionLink(r) +
        '<span class="home-what">' + (crash.step ? "step " + esc(crash.step) + " never settled · " : "") +
        (check.available === true ? "its processes were proved gone ·" + resumeLinkHtml(r)
          : check.available === false ? "cannot resume yet: <code>" + esc(check.code) + '</code> · <a href="#/sessions/' +
            encodeURIComponent(r.session_id) + '?focus=resume">what to do</a>'
          : "the dashboard that ran it is not proved gone") + "</span>" +
        '<span class="home-age">updated ' + esc(fmt.when(r.updated_at)) + "</span>") });
      return;
    }
    crashedRows.push({ row: r, item: homeRow("crashed:" + r.session_id, "crashed",
      '<span class="pill failed">crashed</span> ' + homeSessionLink(r) +
      '<span class="home-what">its worker is gone and the work never finished · ' + esc(identityText(r)) + "</span>" +
      '<span class="home-age">updated ' + esc(fmt.when(r.updated_at)) + "</span>") });
  });
  homeFoldOld(items, crashedRows, "crashed");
  o.attention.waiting.items.forEach(function (r) {
    waitingRows2.push({ row: r, item: homeRow("waiting:" + r.session_id, "waiting",
      '<span class="pill running">waiting</span> ' + homeSessionLink(r) +
      '<span class="home-what">' + (r.approvals_live
        ? 'waiting on ' + esc(homePlural(r.approvals_live, "approval")) + ' · <a href="#/inbox">answer in Approvals</a>'
        : "waiting; no approval is pending for it") + "</span>" +
      '<span class="home-age">updated ' + esc(fmt.when(r.updated_at)) + "</span>") });
  });
  homeFoldOld(items, waitingRows2, "waiting");
  o.attention.paused.items.forEach(function (r) {
    var stop = (r.flow && r.flow.stop) || {};
    var q = r.quota_recovery || {};
    var what = r.pause.kind === "quota"
      ? (typeof r.pause.reset_at === "number"
          // A usage-limit pause whose reset was recorded: when it resets, or that it passed.
          ? "paused: usage limit, " + esc(pauseResetText(r.pause.reset_at))
          : "waiting for quota" + (q.reason ? ": " + esc(q.reason) : "")) +
        (q.next_check ? " · next check " + esc(new Date(q.next_check * 1000).toLocaleString()) : "")
      : (stop.step ? "step " + esc(stop.step) + " " : "a step ") +
        (PAUSE_KINDS[r.pause.kind] || "paused") +
        (stop.code ? " (<code>" + esc(stop.code) + "</code>)" : "");
    // How it ends: automatically (quota recovery) or by the command, after the reset.
    var how = pauseResume(r.pause, r.session_id);
    // A dashboard-started flow resumes from its session page, never by the CLI command.
    what += r.dashboard_operation ? " ·" + resumeLinkHtml(r)
      : ' · <span class="home-resume" data-resume="' + esc(how.resume) + '">' + esc(how.text) +
        (how.command ? ': <code>' + esc(how.command) + "</code>" : "") + "</span>";
    pausedRows.push({ row: r, item: homeRow("paused:" + r.session_id, "paused",
      '<span class="pill paused">paused</span> ' + homeSessionLink(r) +
      '<span class="home-what">' + what + "</span>" +
      '<span class="home-age">updated ' + esc(fmt.when(r.updated_at)) + "</span>") });
  });
  homeFoldOld(items, pausedRows, "paused");
  // A running flow step the live view judges near or past its silence limit (#867): the
  // reader's own silence_state, never a judgement made here.
  o.running.items.forEach(function (r) {
    var live = r.in_flight;
    if (!live || (live.silence_state !== "warn" && live.silence_state !== "over")) return;
    var over = live.silence_state === "over";
    var limit = live.tool_in_flight ? live.tool_silence_limit_sec : live.silence_limit_sec;
    var quiet = typeof live.idle_sec === "number" ? Math.floor(live.idle_sec / 60) : null;
    var limitMin = typeof limit === "number" ? Math.round(limit / 60) : null;
    var what = (live.step ? "step " + esc(live.step) + " · " : "") + (over
      ? "past its silence limit" + (quiet !== null ? " (quiet for " + esc(quiet) + " min" + (limitMin !== null ? ", limit " + esc(limitMin) + " min" : "") + ")" : "")
      : "quiet for " + (quiet !== null ? esc(quiet) : "?") + " min" + (limitMin !== null ? ", limit " + esc(limitMin) : "") +
        " — the watchdog will stop the step");
    items.push(homeRow("quiet:" + r.session_id, over ? "quiet-over" : "quiet",
      '<span class="pill' + (over ? " failed" : "") + '" data-state="' + (over ? "failed" : "waiting") + '">' +
      (over ? "silent" : "quiet") + "</span> " + homeSessionLink(r) +
      '<span class="home-what">' + what + "</span>" +
      '<span class="home-age">' + (live.last_activity_at ? "last activity " + esc(fmt.when(live.last_activity_at)) : "") + "</span>"));
  });
  [["crashed", "crashed"], ["waiting", "waiting"], ["paused", "paused"]].forEach(function (pair) {
    var list = o.attention[pair[0]];
    if (list.total > list.items.length) {
      items.push(homeRow("more:" + pair[0], "more", '<span class="stat-sub">and ' +
        esc(list.total - list.items.length) + " more " + esc(pair[1]) + ' · <a href="#/sessions">Sessions</a></span>'));
    }
  });
  return items;
}

function homeSlowAttention(state, harnesses) {
  var items = [];
  if (state.setup) {
    // Harness errors are listed per harness below, from the same records as Harness health.
    var errors = (state.setup.diagnostics || []).filter(function (d) {
      return d.level === "error" && String(d.code || "").indexOf("harness.") !== 0;
    });
    if (errors.length) {
      items.push(homeRow("config", "config",
        '<span class="pill failed">configuration</span> <a class="home-name" href="#/setup">' +
        esc(homePlural(errors.length, "configuration error")) + "</a>" +
        '<span class="home-what"><code>' + esc(errors[0].code) + "</code> " + esc(fmt.truncate(errors[0].message, 140)) +
        (errors.length > 1 ? " · and " + esc(errors.length - 1) + " more" : "") + "</span>" +
        '<span class="home-age">checked ' + esc(fmt.when(new Date(state.setupAt).toISOString())) + "</span>"));
    }
  }
  (harnesses || []).forEach(function (h) {
    if (!h.unavailable && !h.loggedOut) return;
    var problem = !h.card ? "is not in the runtime catalog" : (h.unavailable ? "is unavailable" : "is logged out");
    items.push(homeRow("harness:" + h.id, "harness",
      '<span class="pill failed">harness</span> <a class="home-name" href="#/providers">' + esc(h.id) + "</a>" +
      '<span class="home-what">' + esc(problem) + " · used by " + esc(h.roles.join(", ")) + "</span>" +
      '<span class="home-age">' + esc(h.loggedOut && !h.unavailable ? homeLoginText(h.reading)
        : "checked " + fmt.when(new Date(state.providersAt).toISOString())) + "</span>"));
  });
  return items;
}

/* --- rendering ------------------------------------------------------------------------- */

function homeRenderAll(state) {
  var o = state.overview;
  var harnesses = homeHarnesses(state);
  var slowPending = (!state.setup && !state.setupError) || (!state.providers && !state.providersError);

  // Needs attention: session rows first (fast), then configuration and harness rows (slow).
  var attention = (o ? homeSessionAttention(o) : []).concat(homeSlowAttention(state, harnesses));
  // Sessions whose records could not be read are left out of every list here; say so.
  homeSet(el("home-unreadable"), unreadableNote(o));
  homePatchList(el("home-attention-list"), attention);
  homeApplyFolds(el("home-attention-list"));
  var status = [];
  if (state.overviewError) status.push(homeError("sessions", state.overviewError, "overview"));
  else if (!o) status.push(homeStatus("pending", "Loading sessions…"));
  if (state.setupError) status.push(homeError("configuration", state.setupError, "setup"));
  if (state.providersError) status.push(homeError("harness state", state.providersError, "providers"));
  if (slowPending) status.push(homeStatus("pending", "Checking configuration and harnesses…"));
  if (!attention.length && o && !status.length) {
    status.push(homeStatus("calm", "Nothing needs you right now"));
  } else if (!attention.length && o && !state.overviewError) {
    status.unshift(homeStatus("calm", "No session needs you."));
  }
  homeSet(el("home-attention-status"), status.join(""));

  // Running now.
  var running = o ? o.running.items.map(function (r) {
    var p = r.progress;
    var step = p ? ' · <span class="home-step">step ' + esc(p.index) + " of " + esc(p.total) + " · " + esc(p.step) +
      (p.attempt > 1 ? " (attempt " + esc(p.attempt) + ")" : "") + "</span>" : "";
    var label = r.state && r.state.work === "queued" && r.queue ? queueText(r.queue) : r.label;
    return homeRow("run:" + r.session_id, "running",
      homeSessionLink(r) + (r.kind === "flow" ? ' <span class="pill unknown">flow</span>' : "") +
      ' <span class="pill ' + (r.state && r.state.work === "working" ? "running" : "unknown") + '" data-state="' + esc(r.label) + '">' +
      esc(label) + "</span>" +
      '<span class="home-what stat-sub">' + esc(identityText(r)) + step + homeLiveText(r.in_flight) + "</span>" +
      '<span class="home-age">' + esc(homeElapsed(r.created_at)) + "</span>" + homeChildrenHtml(r.children));
  }) : [];
  homePatchList(el("home-running-list"), running);
  el("home-running-count").textContent = o ? String(o.running.total) : "";
  var runStatus = "";
  if (state.overviewError) runStatus = homeError("running sessions", state.overviewError, "overview");
  else if (!o) runStatus = homeStatus("pending", "Loading…");
  else if (!running.length) {
    runStatus = homeStatus("calm", "Nothing is running" +
      (canWrite() ? ' · <a href="#/starters">Starters</a>' +
        (STATE.health && STATE.health.capabilities && STATE.health.capabilities.chat ? ' · <a href="#/chat">Chat</a>' : "") : ""));
  } else if (o.running.total > running.length) {
    runStatus = homeStatus("more", "and " + esc(o.running.total - running.length) + ' more · <a href="#/sessions">Sessions</a>');
  }
  homeSet(el("home-running-status"), runStatus);

  // Recent.
  var recent = o ? o.recent.map(function (r) {
    var outcome = (r.state && r.state.outcome) || "unknown";
    return homeRow("recent:" + r.session_id, "recent",
      homeSessionLink(r) + (r.kind === "flow" ? ' <span class="pill unknown">flow</span>' : "") +
      '<span class="home-chips">' + chip("outcome", outcome, r.pause ? "chip-pause" : outcomeClass(outcome)) + " " + verificationChip(r.verification) + "</span>" +
      '<span class="home-what stat-sub">' + esc(identityText(r)) + (r.pause ? "" : resumeLinkHtml(r)) + "</span>" +
      '<span class="home-age">' + esc(fmt.when(r.updated_at)) + "</span>");
  }) : [];
  homePatchList(el("home-recent-list"), recent);
  homeSet(el("home-recent-status"), state.overviewError ? homeError("recent sessions", state.overviewError, "overview")
    : (!o ? homeStatus("pending", "Loading…") : (!recent.length ? homeStatus("calm", "No finished sessions yet.") : "")));

  // Harness health.
  homePatchList(el("home-harness-list"), harnesses ? homeHarnessItems(harnesses) : []);
  var hStatus = "";
  if (state.setupError) hStatus += homeError("the role table", state.setupError, "setup");
  if (state.providersError) hStatus += homeError("harness state", state.providersError, "providers");
  if (!hStatus && !harnesses) hStatus = homeStatus("pending", "Checking harnesses…");
  else if (harnesses && !harnesses.length) {
    hStatus = homeStatus("calm", (state.setup.roles || []).length
      ? "Your roles use no external harness." : 'No roles configured · <a href="#/setup">Setup</a>');
  }
  homeSet(el("home-harness-status"), hStatus);

  if (o) el("home-updated").textContent = "updated " + new Date(state.overviewAt).toLocaleTimeString();
}
