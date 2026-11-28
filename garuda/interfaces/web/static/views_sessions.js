/* Sessions and flows: the shared read model (D.3), rendered (D.4).
 *
 * Everything shown here comes from /api/sessions*, the same rows `garuda sessions --json`
 * prints; nothing is computed in the browser beyond formatting a time. Unknown stays visibly
 * unknown: usage and cost read "unknown" until the usage ledger exists, a queue that could not
 * be read says so, and a review outcome is its own badge — never the verification one.
 *
 * The three pages here (the list, a session, the approvals) each keep their state in one
 * object on STATE and check it by identity before touching the DOM, so a response that lands
 * after the user navigated away is dropped. Polls update in place: a row, a card or a section
 * is replaced only when its HTML changed, and what the user opened stays open. */

"use strict";

function chip(label, value, cls) {
  return '<span class="chip ' + (cls || "chip-off") + '" data-chip="' + esc(label) + '">' +
         '<span class="chip-k">' + esc(label) + ":</span> " +
         esc(value === null || value === undefined ? "unknown" : value) +
         "</span>";
}

/* A flow stopped on a resumable pause (the read model's `pause`), in a few words. Its recorded
 * outcome stays as stored; this says why it stopped and that `flow resume` continues it. */
var PAUSE_KINDS = {
  deadline: "ran past its deadline", silence: "went silent past its limit",
  no_progress: "made no measurable progress", max_turns: "used its whole turn budget",
  refused: "was refused by its harness"
};

function pauseText(pause, stop) {
  if (!pause) return "";
  if (pause.kind === "quota") {
    return typeof pause.reset_at === "number" ? "usage limit, " + pauseResetText(pause.reset_at)
      : "waiting for its harness's usage limit";
  }
  return (stop && stop.step ? "step " + stop.step + " " : "a step ") + (PAUSE_KINDS[pause.kind] || "paused");
}

/* A list row's pause line (Sessions, Runs): why, and how it resumes (automatic | manual). */
function pauseLineHtml(pause, id) {
  if (!pause) return "";
  var how = pauseResume(pause, id);
  return '<div class="s-sub s-pause" data-resume="' + esc(how.resume) + '">' +
    esc(pauseText(pause, null) + " · resume: " + how.resume) + "</div>";
}

/* The pause in a few words, for a marker that points to the Result. */
function pauseShortText(pause) {
  return pause.kind === "quota" ? "usage limit" : (PAUSE_KINDS[pause.kind] || "paused");
}

/* "resets 13:24 (in 12 min)", or "reset passed at 13:24" once the time is behind us: never a
 * negative countdown. */
function pauseResetText(resetAt) {
  var at = new Date(resetAt * 1000);
  var clock = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (resetAt * 1000 <= Date.now()) return "reset passed at " + clock;
  var seconds = resetAt - Date.now() / 1000;
  return "resets " + clock + " (" + (seconds < 3600 * 24 ? untilEpochText(resetAt) : at.toLocaleString()) + ")";
}

/* A recorded sentence may embed an ISO timestamp ("after 2026-11-27T16:31:24+00:00"): the
 * page shows local time instead, the way it shows every reset ("13:24 (in 12 min)"). */
var ISO_STAMP = /\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/g;

function localStampText(epoch) {
  var at = new Date(epoch * 1000);
  var clock = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  var ahead = epoch - Date.now() / 1000;
  if (ahead > 0 && ahead < 24 * 3600) return clock + " (" + untilEpochText(epoch) + ")";
  return Math.abs(ahead) < 24 * 3600 ? clock : at.toLocaleString();
}

function humanStamps(text) {
  return String(text).replace(ISO_STAMP, function (stamp) {
    var ms = new Date(stamp).getTime();
    return isNaN(ms) ? stamp : localStampText(ms / 1000);
  });
}

/* How a pause ends, from the read model's `pause.resume` (automatic | manual) and `action`.
 * Manual: the command to run (copyable), after the reset when there is one; once the reset has
 * passed and nothing resumed it, that is said plainly. Automatic: quota recovery resumes it.
 * A pause kind without `resume` (a deadline, silence, no progress…) is resumed by hand. */
function pauseResume(pause, flowId) {
  if (!pause) return null;
  var resume = pause.resume || "manual";
  var command = "garuda flow resume " + flowId;
  var passed = typeof pause.reset_at === "number" && pause.reset_at * 1000 <= Date.now();
  var text;
  if (resume === "automatic") {
    text = passed ? "reset passed; quota recovery resumes it at its next check"
                  : "resumes automatically after the reset";
  } else if (pause.kind === "quota") {
    text = passed ? "reset passed; run the command to continue"
                  : (typeof pause.reset_at === "number" ? "run the command after the reset" : "run the command after the limit resets");
  } else {
    text = "run the command to continue";
  }
  // The structured fields say it all when the reset is known; the recorded sentence is the
  // fallback, with any timestamp in it shown as local time.
  return { resume: resume, text: text, command: resume === "manual" ? command : null,
           action: typeof pause.action === "string" && typeof pause.reset_at !== "number" ? humanStamps(pause.action) : null };
}

if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("click", function (event) {
    var button = event.target && event.target.closest && event.target.closest(".pause-block button.copy-fix[data-copy]");
    if (!button) return;
    var text = button.getAttribute("data-copy");
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(function () { toast("Copied"); },
      function () { toast("Copy is not available here; select the text"); });
  });
}

/* An in-page link ("see Result"): scroll to it, never a route change (the router owns the
 * hash, so a bare "#id" would open "No such view"). */
if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("click", function (event) {
    var link = event.target && event.target.closest && event.target.closest("a[data-scroll-to]");
    if (!link) return;
    event.preventDefault();
    var target = document.getElementById(link.getAttribute("data-scroll-to"));
    if (target && target.scrollIntoView) target.scrollIntoView({ block: "center" });
  });
}

/* The pause as one block: why, when (for a usage limit), how it ends, the command to copy. */
function pauseBlockHtml(pause, stop, flowId, id) {
  var how = pauseResume(pause, flowId);
  if (!how) return "";
  return '<div class="pause-block" data-state="paused" data-resume="' + esc(how.resume) + '"' + (id ? ' id="' + esc(id) + '"' : "") + ">" +
    '<p class="pause-text"><span class="pill paused" data-state="paused">paused</span> ' + esc(pauseText(pause, stop)) + "</p>" +
    '<p class="pause-how">' + chip("resume", how.resume, "chip-pause") + " " + esc(how.text) +
    (how.command ? ' · <code class="pause-command">' + esc(how.command) + "</code> " +
      '<button type="button" class="btn btn-small copy-fix" data-copy="' + esc(how.command) + '">Copy</button>' : "") + "</p>" +
    (how.action ? '<p class="stat-sub pause-action">recorded next step: ' + esc(how.action) + "</p>" : "") + "</div>";
}

function outcomeClass(value) {
  if (value === "completed") return "chip-on";
  if (value === "failed" || value === "cancelled" || value === "refused") return "chip-bad";
  return "chip-off";
}

/* The engine records `status` (review_approved, review_changes_requested, review_not_independent)
 * and how many rounds it took. */
function reviewLabel(review) {
  if (review.verdict || review.outcome) return review.verdict || review.outcome;
  if (!review.status) return "recorded";
  var label = review.status.replace(/^review_/, "").replace(/_/g, " ");
  return review.rounds ? label + " after " + review.rounds + " round" + (review.rounds === 1 ? "" : "s") : label;
}

function verificationChip(v) {
  v = v || {};
  var text = v.status || "unknown";
  if (v.authority && (v.status === "passed" || v.status === "failed")) text += " (" + v.authority + ")";
  return chip("verification", text, v.status === "passed" ? "chip-on" : (v.status === "failed" ? "chip-bad" : "chip-off"));
}

function selfCheckChip(check) {
  if (!check) return "";
  return chip("self-check", check.status, check.status === "passed" ? "chip-on" :
    (check.status === "failed" ? "chip-bad" : "chip-off"));
}

function identityText(row) {
  var parts = [row.runtime || "native"];
  if (row.role && row.role.name) parts.push("role " + row.role.name);
  if (row.model) parts.push(row.model);
  return parts.join(" · ");
}

function queueText(queue) {
  if (!queue) return "—";
  if (queue.state === "queued") return "queued #" + queue.position;
  if (queue.state === "running") return "has its slot";
  return queue.state || "unknown";
}

function isActive(state) { return ["queued", "working", "waiting"].indexOf((state || {}).work) >= 0; }

/* Every spend-gate field as its own chip: the session page and the Usage page show these. */
function spendHtml(spend) {
  if (!spend) return chip("spend", "not recorded");
  if (spend.state !== "recorded") return chip("spend", "unreadable (unknown)");
  var html = chip("scope", spend.scope) +
    chip("token cap", spend.max_tokens === null ? "none" : spend.max_tokens) +
    chip("USD cap", spend.max_cost_usd === null ? "none" : spend.max_cost_usd) +
    chip("settled tokens", spend.settled_tokens) + chip("reserved tokens", spend.reserved_tokens) +
    chip("cost", spend.cost_status === "unknown" ? "unknown" : "reported") +
    chip(spend.cost_status === "unknown" ? "known settled USD (lower bound)" : "settled USD", spend.settled_cost_usd);
  ["cost_unknown_calls", "prior_spend_unknown", "interrupted_calls", "unknown_outcome_calls",
    "unreported_failed_attempts", "estimated_token_calls", "refused"].forEach(function (key) {
    html += chip(key, spend[key]);
  });
  if (spend.inherited) {
    html += chip("inherited source", spend.inherited.source) + chip("inherited tokens", spend.inherited.inherited_tokens);
  }
  return html;
}

function knownText(value, format) {
  return value === null || value === undefined ? "unknown" : (format ? format(value) : String(value));
}

function usdText(value) {
  return typeof value === "number" ? fmt.cost(value) : knownText(value);
}

/* The spend gate in one line for the list: what settled against which caps. A cost Garuda
 * could not price stays a lower bound, never a total; the chips are on the session page. */
function spendSummaryText(spend) {
  if (!spend) return "";
  if (spend.state !== "recorded") return "spend unreadable (unknown)";
  var tokens = knownText(spend.settled_tokens, fmt.tokens) + " / " +
    (spend.max_tokens === null ? "no cap" : knownText(spend.max_tokens, fmt.tokens)) + " tokens";
  var usd = spend.cost_status === "unknown" ? "≥ " + usdText(spend.settled_cost_usd) + " (lower bound)"
                                            : usdText(spend.settled_cost_usd);
  return "spend " + tokens + " · " + usd + " of " +
    (spend.max_cost_usd === null ? "no USD cap" : usdText(spend.max_cost_usd) + " cap");
}

/* A recorded time as "3h ago", with the absolute time beside it or in its title. */
function absoluteTime(iso) {
  if (!iso) return "unknown";
  var then = new Date(iso);
  return isNaN(then.getTime()) ? "unknown" : then.toLocaleString();
}

function relativeTimeHtml(iso) {
  if (!iso) return '<span class="stat-sub">unknown</span>';
  return '<time datetime="' + esc(iso) + '" title="' + esc(absoluteTime(iso)) + '">' + esc(fmt.when(iso)) + "</time>";
}

function firstLine(text) {
  return String(text || "").split("\n").map(function (l) { return l.trim(); }).filter(Boolean)[0] || "";
}

/* --- the Sessions list (#/sessions?q=…&state=…) ------------------------------------------
 * A page of rows from the server, which searches and filters across every session; "Load
 * older" asks for the next page. The filter lives in the hash, so a reload or a shared link
 * keeps it and back/forward walks through filter changes. */

var SESSIONS_PAGE = 50;
var SESSIONS_POLL_MS = 15000;

/* The filter from the route's query (router.js routeParams): q, and the state chip as `state`. */
function parseSessionQuery() {
  var params = routeParams();
  return { q: params.q || "", label: params.state || "" };
}

function sessionRouteParams(filter) {
  return { q: (filter && filter.q) || "", state: (filter && filter.label) || "" };
}

function sessionsHash(filter) {
  var params = new URLSearchParams();
  if (filter && filter.q) params.set("q", filter.q);
  if (filter && filter.label) params.set("state", filter.label);
  var query = params.toString();
  return "#/sessions" + (query ? "?" + query : "");
}

function sessionsUrl(filter, offset, limit) {
  var params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filter.q.trim()) params.set("q", filter.q.trim());
  if (filter.label) params.set("state", filter.label);
  return "/api/sessions?" + params.toString();
}

function approvalsCellHtml(row) {
  var pending = row.approvals_pending || 0;
  var live = typeof row.approvals_live === "number" ? row.approvals_live : pending;
  if (!pending) return "—";
  return (live ? '<span class="pill running">' + esc(live) + " waiting</span>" : "") +
    (pending > live ? '<div class="s-sub">' + esc(pending - live) + " expired</div>" : "");
}

/* A flow step's own session names the flow, step and attempt it ran for (`flow_step`, as
 * recorded at creation); the list and the Overview link it back to its flow. */
function flowStepLinkHtml(link) {
  if (!link || !link.flow_session) return "";
  return '<div class="s-sub flow-step-link">step ' + esc(link.step || "unknown") +
    (link.attempt ? " (attempt " + esc(link.attempt) + ")" : "") + ' of flow <a href="#/sessions/' +
    encodeURIComponent(link.flow_session) + '"><code>' + esc(String(link.flow_session).slice(0, 8)) + "</code></a></div>";
}

function sessionRowHtml(row) {
  var state = row.state || {};
  var id = String(row.session_id || "");
  var task = fmt.truncate(firstLine(row.task), 64);
  var where = row.workspace ? fmt.tail(row.workspace, 30) : "—";
  var summary = spendSummaryText(row.spend);
  return (
    '<tr class="session-row" data-session="' + esc(id) + '" data-label="' + esc(row.label) + '"' +
    (row.pause ? ' data-pause="' + esc(row.pause.kind) + '"' : "") + ">" +
    '<td class="s-name" data-th="Session"><a href="#/sessions/' + encodeURIComponent(id) + '">' +
    esc(row.name || id.slice(0, 8)) + "</a>" +
    (row.kind === "flow" ? ' <span class="pill unknown">flow</span>' : "") +
    (row.origin === "consult" ? ' <span class="pill unknown">consult</span>' : "") +
    ' <a class="session-trace stat-sub" href="#/runs/' + encodeURIComponent(id) + '" title="Trace and metrics on Runs">trace</a>' +
    // Two runs of one task have the same name; the id and the task's first words tell them apart.
    '<div class="s-sub"><code>' + esc(id.slice(0, 8)) + "</code>" + (task ? " · " + esc(task) : "") + "</div>" +
    flowStepLinkHtml(row.flow_step) + "</td>" +
    '<td data-th="State"><span class="pill ' + (row.pause ? "paused" : (row.crashed ? "failed" : (state.work === "working" ? "running" : "unknown"))) +
    '" data-state="' + esc(row.pause ? "paused" : row.label) + '">' +
    esc(row.pause ? "paused" : row.label) + "</span>" + (row.queue ? '<div class="s-sub">' + esc(queueText(row.queue)) + "</div>" : "") +
    pauseLineHtml(row.pause, id) + (typeof resumeLinkHtml === "function" ? resumeLinkHtml(row) : "") + "</td>" +
    '<td class="s-clip" data-th="Runtime · role · model" title="' + esc(identityText(row)) + '">' + esc(identityText(row)) + "</td>" +
    '<td class="s-facts" data-th="Outcome / verification">' + chip("outcome", state.outcome || "—", row.pause ? "chip-pause" : outcomeClass(state.outcome)) + " " +
    verificationChip(row.verification) + selfCheckChip(row.self_check) + "</td>" +
    '<td class="s-clip" data-th="Workspace" title="' + esc((row.workspace || "") + (row.branch ? " @ " + row.branch : "")) + '">' +
    esc(where) + (row.branch ? '<div class="s-sub">@ ' + esc(row.branch) + "</div>" : "") + "</td>" +
    '<td class="s-usage" data-th="Usage / cost"><span class="mono">' + esc(row.usage) + " / " + esc(row.cost) + "</span>" +
    (summary ? '<div class="s-sub s-spend" title="' + esc(summary) + '">' + esc(summary) + "</div>" : "") + "</td>" +
    '<td class="num s-approvals" data-th="Approvals"' + (row.approvals_pending ? " data-waiting" : "") + ">" +
      approvalsCellHtml(row) + "</td>" +
    '<td class="s-when" data-th="Updated">' + relativeTimeHtml(row.updated_at) + "</td>" +
    '<td class="s-when" data-th="Started">' + relativeTimeHtml(row.created_at) + "</td>" +
    sessionRowMoreHtml(row, where, summary) +
    "</tr>"
  );
}

/* The phone card's "details": what the desktop table shows as columns (workspace, started,
 * usage / cost and the spend line), one tap away. Hidden at desktop width (app.css), where
 * those columns are shown; usage / cost is left out when neither is known. */
function sessionRowMoreHtml(row, where, summary) {
  var usage = row.usage === "unknown" && row.cost === "unknown" ? "" : row.usage + " / " + row.cost;
  return '<td class="s-more"><details class="s-details" data-key="s-more:' + esc(row.session_id) + '">' +
    "<summary>details</summary><dl>" +
    "<dt>Workspace</dt><dd>" + esc(where) + (row.branch ? " @ " + esc(row.branch) : "") + "</dd>" +
    "<dt>Started</dt><dd>" + relativeTimeHtml(row.created_at) + "</dd>" +
    (usage ? '<dt>Usage / cost</dt><dd class="mono">' + esc(usage) + "</dd>" : "") +
    (summary ? "<dt>Spend</dt><dd>" + esc(summary) + "</dd>" : "") + "</dl></details></td>";
}

function sessionsView() {
  stopSessionPages();
  reconnectInPlace();
  // The hash is the filter: plain #/sessions is unfiltered, so Back to it shows everything.
  // A session page's "All sessions" link returns to the last filter (STATE.filters.sessions).
  var filter = parseSessionQuery();
  STATE.filters.sessions = filter;
  var list = STATE.sessionList = { filter: filter, rows: [], counts: {}, total: null, matched: null,
                                   nextOffset: null, seq: 0, inflight: 0, loadingOlder: false,
                                   painted: false, error: null };
  render(
    '<div class="page-head"><h1>Sessions</h1><span class="meta" id="sessions-count">loading…</span>' +
    '<button type="button" class="btn" id="sessions-refresh">Refresh</button></div>' +
    '<p class="page-lede" id="sessions-lede">State, queue, approvals and control for each session. ' +
    'Traces and metrics of what each agent did are on <a href="#/runs">Runs</a>.</p>' +
    '<div class="filters"><input type="search" id="sessions-search" data-route-param aria-label="Search sessions" placeholder="Search name, id, task, model, workspace…  ( / )" value="' + esc(filter.q) + '">' +
    '<div class="state-chips" id="sessions-states"></div></div>' +
    '<div class="notice err" id="sessions-error" hidden></div><div id="sessions-unreadable"></div>' +
    '<div class="table-wrap sessions-wrap"><table class="table sessions-table" id="sessions-table"><thead><tr>' +
    "<th>Session</th><th>State</th><th>Runtime · role · model</th><th>Outcome / verification</th>" +
    "<th>Workspace</th><th>Usage / cost</th><th class=\"num\">Approvals</th><th>Updated</th><th>Started</th>" +
    '</tr></thead><tbody id="sessions-tbody"></tbody></table>' +
    '<div class="filter-empty" id="sessions-none" hidden></div>' +
    '<div class="sessions-more"><button type="button" class="btn" id="sessions-more" hidden>Load older</button></div></div>'
  );
  el("sessions-refresh").addEventListener("click", function () { reloadSessionList(list); });
  var typing = null;
  el("sessions-search").addEventListener("input", function (event) {
    clearTimeout(typing);
    var value = event.target.value;
    typing = setTimeout(function () {
      if (STATE.sessionList !== list) return;
      filter.q = value;
      // Typing replaces the history entry (no hashchange, so the field keeps focus).
      setRouteParams(sessionRouteParams(filter));
      reloadSessionList(list);
    }, 250);
  });
  el("sessions-states").addEventListener("click", function (event) {
    var button = event.target.closest(".state-chip");
    if (!button) return;
    var next = { q: el("sessions-search").value, label: button.getAttribute("data-label") };
    // A state chip is a new history entry, so Back returns to the previous filter; the list
    // reloads in place (Back and Forward re-mount it from the URL).
    filter.q = next.q;
    filter.label = next.label;
    setRouteParams(sessionRouteParams(filter), { push: true });
    reloadSessionList(list);
  });
  el("sessions-more").addEventListener("click", function () { loadOlderSessions(list); });
  list.poller = new Poller(function () { return refreshSessionList(list); }, SESSIONS_POLL_MS).start();
  return Promise.resolve();
}

/* One request at a time per list. `seq` names the newest; a newer request (another filter,
 * "Load older") supersedes one in flight, whose answer is then dropped. */
function sessionListRequest(list, url, older) {
  var seq = ++list.seq;
  list.inflight = seq;
  list.loadingOlder = older;
  paintSessionMore(list);
  function settled() {
    if (list.inflight === seq) { list.inflight = 0; list.loadingOlder = false; }
    return STATE.sessionList === list && seq === list.seq;
  }
  return api(url).then(function (payload) {
    if (!settled()) return null;
    return payload;
  }, function (err) {
    if (isSuperseded(err)) { settled(); return null; }
    if (settled()) sessionListFailed(list, err);
    return null;
  });
}

/* The loaded window again from the top: new sessions appear, changed rows are replaced and
 * the rows loaded through "Load older" stay. */
function refreshSessionList(list) {
  // While unreachable the app's one probe asks (core.js); onReconnect polls again.
  if (STATE.sessionList !== list || list.inflight || CONNECTION.down) return;
  var limit = Math.min(500, Math.max(SESSIONS_PAGE, list.rows.length));
  return sessionListRequest(list, sessionsUrl(list.filter, 0, limit), false).then(function (payload) {
    if (!payload) return;
    var fresh = payload.sessions || [];
    var seen = {};
    fresh.forEach(function (r) { seen[r.session_id] = true; });
    var older = list.rows.slice(fresh.length).filter(function (r) { return !seen[r.session_id]; });
    list.rows = fresh.concat(older);
    list.nextOffset = older.length ? list.nextOffset : payload.next_offset;
    acceptSessionPage(list, payload);
  });
}

function reloadSessionList(list) {
  if (STATE.sessionList !== list) return;
  list.rows = [];
  list.inflight = 0;  // supersede whatever is in flight: its answer is for the old filter
  return refreshSessionList(list);
}

function loadOlderSessions(list) {
  if (STATE.sessionList !== list || list.loadingOlder || list.nextOffset === null) return;
  var offset = list.nextOffset;
  return sessionListRequest(list, sessionsUrl(list.filter, offset, SESSIONS_PAGE), true).then(function (payload) {
    if (!payload) return;
    var seen = {};
    list.rows.forEach(function (r) { seen[r.session_id] = true; });
    list.rows = list.rows.concat((payload.sessions || []).filter(function (r) { return !seen[r.session_id]; }));
    list.nextOffset = payload.next_offset;
    acceptSessionPage(list, payload);
  });
}

function acceptSessionPage(list, payload) {
  list.total = payload.total;
  list.matched = payload.matched;
  list.counts = payload.counts || {};
  list.error = null;
  // Sessions whose record could not be read are left out and counted (the server says which).
  paintHost("sessions-unreadable", unreadableNote(payload));
  list.painted = true;
  paintSessionList(list);
}

function sessionListFailed(list, err) {
  if (err.status === 401) { stopSessionPages(); render(tokenRequiredPanel()); return; }
  list.error = err;
  var box = el("sessions-error");
  if (!box) return;
  box.hidden = false;
  box.innerHTML = '<div class="notice-title">' + (list.painted ? "Could not refresh sessions" : "Could not load sessions") +
    '</div><div class="notice-body">' + esc(err.message) + (err.code ? " (" + esc(err.code) + ")" : "") +
    (list.painted ? " — showing what was loaded; it retries on its own." : "") + "</div>";
  paintSessionMore(list);
}

/* Rows keyed by session id: a row is replaced only when its HTML changed, and moved only
 * when the order did, so a poll never rebuilds the table under the pointer. */
function paintSessionList(list) {
  var tbody = el("sessions-tbody");
  if (!tbody) return;
  var existing = {};
  Array.prototype.forEach.call(tbody.children, function (tr) { existing[tr.getAttribute("data-session")] = tr; });
  var previous = null;
  list.rows.forEach(function (row) {
    var html = sessionRowHtml(row);
    var node = existing[row.session_id];
    if (!node || node.__html !== html) {
      var holder = document.createElement("tbody");
      holder.innerHTML = html;
      var fresh = holder.firstElementChild;
      fresh.__html = html;
      if (node) tbody.replaceChild(fresh, node);
      node = fresh;
    }
    delete existing[row.session_id];
    var slot = previous ? previous.nextElementSibling : tbody.firstElementChild;
    if (slot !== node) tbody.insertBefore(node, slot);
    previous = node;
  });
  Object.keys(existing).forEach(function (key) { existing[key].remove(); });

  var filter = list.filter;
  var all = Object.keys(list.counts).reduce(function (sum, k) { return sum + list.counts[k]; }, 0);
  var labels = Object.keys(list.counts).sort();
  if (filter.label && labels.indexOf(filter.label) < 0) labels.push(filter.label);
  var states = el("sessions-states");
  var chipsHtml = '<button type="button" class="state-chip' + (filter.label ? "" : " active") + '" data-label="">all ' + esc(all) + "</button>" +
    labels.map(function (label) {
      return '<button type="button" class="state-chip' + (label === filter.label ? " active" : "") + '" data-label="' + esc(label) + '">' +
        esc(label) + " " + esc(list.counts[label] || 0) + "</button>";
    }).join("");
  if (states && states.__html !== chipsHtml) { states.innerHTML = chipsHtml; states.__html = chipsHtml; }

  var filtered = !!(filter.q.trim() || filter.label);
  var shown = list.rows.length;
  var count = el("sessions-count");
  if (count) {
    count.textContent = (shown === list.matched ? String(shown) : shown + " of " + list.matched) +
      (filtered ? " matching · " + list.total + " in total" : (shown === list.matched ? "" : " sessions"));
  }
  var none = el("sessions-none");
  if (none) {
    none.hidden = shown !== 0;
    none.innerHTML = list.total === 0
      ? "<p><strong>No sessions yet.</strong></p><p>Start one with <code>garuda run --bg -t &quot;your task&quot;</code>.</p>"
      : "No session matches this filter.";
  }
  var box = el("sessions-error");
  if (box && !list.error) box.hidden = true;
  paintSessionMore(list);
}

function paintSessionMore(list) {
  var more = el("sessions-more");
  if (!more) return;
  var remaining = list.matched === null ? 0 : list.matched - list.rows.length;
  more.hidden = list.nextOffset === null || list.nextOffset === undefined;
  more.disabled = !!list.loadingOlder;
  more.textContent = list.loadingOlder ? "Loading…" : "Load older (" + remaining + " more)";
}

/* --- one session (#/sessions/<id>) ---------------------------------------------------------
 * Leads with what was asked and what came of it; the forensic records follow under "Details
 * and evidence". While the session is active the page polls its log tail and refreshes its row,
 * and it updates each section in place. */

var SESSION_ROW_MS = 10000;
var SESSION_TAIL_MS = 2000;
var SESSION_RETRY_MAX_MS = 30000;
var TASK_PREVIEW_LINES = 3;

function artifactText(ref) {
  return ref.type + " from " + ref.producer_step + " (" + String(ref.digest || "").slice(0, 8) + ")";
}

function ranText(attempt) {
  var who = attempt.runtime ? attempt.runtime + (attempt.model ? "/" + attempt.model : "") : "harness not recorded";
  return (attempt.role ? attempt.role + " → " : "") + who;
}

/* A long step's turn loop as its receipt recorded it (#842); nothing for a one-turn step.
 * A count the receipt did not record reads "unknown", never 0. */
function turnLoopHtml(loop) {
  if (!loop) return "";
  var parts = [["turn", loop.turns], ["rotation", loop.rotations], ["continuation", loop.continuations],
               ["repair", loop.repairs]].map(function (pair) {
    return knownText(pair[1]) + " " + pair[0] + (pair[1] === 1 ? "" : "s");
  });
  var outcomes = (loop.repair_outcomes || []).filter(Boolean);
  // An interrupted attempt's counts were recovered from its progress file (#873): the last
  // the loop recorded before it stopped, said so, with that last turn's decision and time.
  var recovered = loop.source === "progress";
  var last = recovered ? [loop.last_decision ? "last turn: " + (liveWords(LIVE_DECISIONS, loop.last_decision) || loop.last_decision) : null,
                          loop.last_turn_at ? fmt.when(loop.last_turn_at) : null].filter(Boolean).join(" · ") : "";
  return '<div class="stat-sub turn-loop" data-source="' + esc(loop.source || "loop") + '">' +
    (recovered ? '<span class="turn-loop-label">last known, from the progress file:</span> ' : "") +
    esc(parts.join(" · ")) + (outcomes.length ? " (" + esc(outcomes.join(", ")) + ")" : "") +
    (last ? " · " + esc(last) : "") + "</div>";
}

/* How much of the change set the approving review saw (#851): "partial" is worth a look. */
function reviewCoverageHtml(coverage) {
  var steps = Object.keys(coverage || {});
  if (!steps.length) return "";
  return steps.map(function (step) {
    var value = coverage[step];
    return " " + chip("coverage" + (steps.length > 1 ? " (" + step + ")" : ""), value,
      value === "partial" ? "chip-bad" : (value === "complete" ? "chip-on" : "chip-off"));
  }).join("") + (steps.some(function (s) { return coverage[s] === "partial"; })
    ? ' <span class="stat-sub">the reviewer saw only part of the change set ·</span>' : "");
}

/* An attempt's stop. A paused attempt (its receipt's own pause, or the attempt its flow's
 * pause stopped on) reads as a pause, pointing to where the pause is said; its raw stop code
 * stays in the tooltip and in Details and evidence. The red callout is for failures only. */
function attemptStopHtml(attempt, pauseKind, link, replacedBy) {
  var stop = attempt.stop;
  if (!stop) return "";
  var known = FLOW_STOPS[stop.code];
  if (replacedBy !== null && replacedBy !== undefined) {
    // History, not a standing failure: a later attempt of this step succeeded.
    return '<div class="stat-sub attempt-stop" data-state="replaced" data-code="' + esc(stop.code) + '" title="' +
      esc("stop code: " + stop.code) + '">' + esc((REPLACED_STOPS[stop.code] || "stopped: " + stop.code) +
      " · replaced by attempt " + replacedBy) + (known ? " — " + esc(known.text) : "") + "</div>";
  }
  if (!pauseKind) {
    return '<div class="stat-sub attempt-stop">stopped: ' + esc(stop.code) + (known ? " — " + esc(known.text) : "") + "</div>";
  }
  return '<div class="stat-sub attempt-stop" data-state="paused" data-code="' + esc(stop.code) + '" title="' +
    esc("stop code: " + stop.code + (known ? " — " + known.text : "")) + '">' +
    esc(pauseKind === "quota" ? "stopped on a usage limit" : "stopped: " + (PAUSE_KINDS[pauseKind] || "paused")) +
    (link ? ' — see <a href="#' + esc(link.id) + '" data-scroll-to="' + esc(link.id) + '">' + esc(link.label) + "</a>" : "") + "</div>";
}

/* What a replaced attempt's stop was, in a word (the full sentence follows). */
var REPLACED_STOPS = {
  "flow.interrupted": "interrupted", "flow.step_deadline": "ran past its deadline",
  "flow.step_silent": "went silent"
};

/* `replacedBy`: the number of a later attempt of the same step that succeeded, when there is
 * one. Its stop is then history, drawn neutral; red is for a failure that stands. */
function attemptHtml(attempt, flowPause, link, replacedBy, resumed) {
  var delta = attempt.delta === null ? "unknown" : (attempt.delta ? "changed" : "unchanged");
  var replaced = attempt.status !== "done" && replacedBy !== null && replacedBy !== undefined;
  var pauseKind = replaced ? null : (attempt.pause || (flowPause && flowPause.kind) || null);
  return (
    '<li class="attempt" data-attempt="' + esc(attempt.attempt) + '" data-status="' + esc(attempt.status) + '"' +
    (pauseKind ? ' data-pause="' + esc(pauseKind) + '"' : "") +
    (replaced ? ' data-replaced="' + esc(replacedBy) + '"' : "") + ">" +
    '<div class="attempt-head">attempt ' + esc(attempt.attempt) + " · " + esc(ranText(attempt)) +
    " " + chip("status", attempt.status, attempt.status === "done" ? "chip-on"
      : (replaced ? "chip-off" : (pauseKind ? "chip-pause" : "chip-bad"))) +
    " " + chip("workspace", delta, "chip-off") +
    (resumed ? ' <span class="pill unknown resume-attempt" title="This attempt ran after the dashboard resumed the flow">resumed</span>' : "") +
    (attempt.no_edits ? " " + chip("no-edits", attempt.no_edits, attempt.no_edits === "unchanged" ? "chip-on" : "chip-bad") : "") +
    ' <span class="stat-sub">' + esc(attempt.duration_ms === null || attempt.duration_ms === undefined ? "duration unknown" : fmt.duration(attempt.duration_ms)) + "</span>" +
    (attempt.session_id ? ' <a class="btn btn-small attempt-trace" href="#/runs/' + encodeURIComponent(attempt.session_id) + '">Open trace</a>' +
      ' · <a href="#/sessions/' + encodeURIComponent(attempt.session_id) + '">session</a>' : "") + "</div>" +
    turnLoopHtml(attempt.turn_loop) +
    attemptStopHtml(attempt, pauseKind, link, replaced ? replacedBy : null) +
    ((attempt.inputs || []).length ? '<div class="stat-sub">in: ' + attempt.inputs.map(function (r) { return esc(artifactText(r)); }).join(", ") + "</div>" : "") +
    ((attempt.outputs || []).length ? '<div class="stat-sub">out: ' + attempt.outputs.map(function (r) { return esc(artifactText(r)); }).join(", ") + "</div>" : "") +
    ((attempt.members || []).length ? '<div class="stat-sub">reviewers: ' + attempt.members.map(function (m) { return esc(m.role) + (m.success ? " ✓" : " ✗"); }).join(", ") + "</div>" : "") +
    "</li>"
  );
}

function stepStatus(step, current, paused, stopped) {
  var attempts = step.attempts || [];
  if (current && current.step === step.id) return { text: "running · attempt " + current.attempt, cls: "running", key: "current" };
  if (paused && paused.step === step.id) return { text: "paused", cls: "paused", key: "paused" };
  if (stoppedAttemptOf(step, stopped)) return { text: "interrupted · attempt " + stopped.attempt, cls: "unknown", key: "interrupted" };
  if (!attempts.length) return { text: "not run yet", cls: "unknown", key: "pending" };
  var last = attempts[attempts.length - 1];
  if (last.status === "done") return { text: "done", cls: "success", key: "done" };
  if (last.pause) return { text: last.status, cls: "paused", key: "failed" };
  return { text: last.status || "unknown", cls: last.status ? "failed" : "unknown", key: "failed" };
}

/* The live view's open attempt whose process is gone (`running: false`, not paused) when
 * `step` has no receipt for it: launched, then stopped (a Stop, a crash) before it recorded an
 * outcome. The step did run, so it is never "not run yet". */
function stoppedAttempt(flow) {
  var live = flow && flow.in_flight;
  return live && live.step && !live.paused && live.running === false ? live : null;
}

function stoppedAttemptOf(step, stopped) {
  if (!stopped || stopped.step !== step.id) return false;
  return !(step.attempts || []).some(function (a) { return a.attempt === stopped.attempt; });
}

function stoppedAttemptHtml(stopped) {
  var sid = stopped.session_id;
  return '<div class="stat-sub pipe-interrupted" data-state="interrupted">attempt ' + esc(stopped.attempt) +
    (stopped.started_at ? " started " + esc(fmt.when(stopped.started_at)) : "") +
    "; its process exited before it recorded an outcome" +
    (sid ? ' · <a href="#/sessions/' + encodeURIComponent(sid) + '">step session</a> · <a href="#/runs/' +
      encodeURIComponent(sid) + '">trace</a>' : "") + "</div>";
}

function edgeAttempt(attempt) {
  return attempt === null || attempt === undefined ? "" : " #" + esc(attempt);
}

/* A flow as a vertical pipeline: each step, who ran it, its status, attempts, durations and
 * what it passed on; the step in flight is highlighted. The review is its own badge. */
function flowHtml(flow, progress, options) {
  if (!flow) return "";
  options = options || {};
  // Where the pause is said on this page (the session's Result, the trace's "How it ended").
  var link = options.pause && options.result ? { id: options.result, label: options.resultLabel || "Result" } : null;
  var review = flow.review;
  var edges = flow.edges || [];
  return (
    '<section class="card flow" id="flow-detail"><h2>Flow ' + esc(flow.name) + " " + chip("flow", flow.state, "chip-off") + "</h2>" +
    (flow.quota_recovery ? '<p id="flow-quota-recovery">Quota recovery: ' +
      esc(flow.quota_recovery.state) + " · " + esc(flow.quota_recovery.reason || "") +
      (flow.quota_recovery.next_check ? " · next check " +
        esc(new Date(flow.quota_recovery.next_check * 1000).toLocaleString()) : "") + "</p>" : "") +
    // A review is advice about the work. It has its own badge and never says "verified".
    (review ? '<p id="flow-review">' + chip("review", reviewLabel(review), "chip-off") +
              reviewCoverageHtml(flow.review_coverage) +
              ' <span class="stat-sub">a review is not verification</span></p>' : "") +
    '<ol class="pipeline steps">' + (flow.steps || []).map(function (step, index) {
      var live = flowInFlight(flow);
      var current = progress || (live ? { step: live.step, attempt: live.attempt } : null);
      var stopped = stoppedAttempt(flow);
      // The journal's progress names the open attempt, but the live view says its process is
      // gone: it is not in flight (there is no step block to point at).
      if (stopped && current && current.step === stopped.step && String(current.attempt) === String(stopped.attempt)) current = null;
      var status = stepStatus(step, current, flowPaused(flow), stopped);
      var attempts = step.attempts || [];
      var last = attempts[attempts.length - 1];
      var durations = attempts.map(function (a) { return a.duration_ms; });
      var total = durations.length && durations.every(function (d) { return typeof d === "number"; })
        ? durations.reduce(function (s, d) { return s + d; }, 0) : null;
      var passes = edges.filter(function (e) { return e.from_step === step.id; });
      return '<li class="pipe-step pipe-' + status.key + '" data-step="' + esc(step.id) + '"' +
        (last && status.key !== "current" ? ' data-status="' + esc(last.status) + '"' : "") +
        (last && last.pause && status.key !== "current" ? ' data-pause="' + esc(last.pause) + '"' : "") +
        // A step the live view says is paused, before (or beyond) a receipt that says so.
        (status.key === "paused" && !(last && last.pause)
          ? ' data-pause="' + esc(String((flowPaused(flow) || {}).code || "paused").replace(/^flow\.(step_)?/, "")) + '"' : "") +
        (status.key === "current" ? ' aria-current="step"' : "") + ">" +
        '<div class="pipe-head"><span class="pipe-no">' + (index + 1) + "</span><strong>" + esc(step.id) + "</strong> " +
        '<span class="pill ' + status.cls + '">' + esc(status.text) + "</span>" +
        (last ? ' <span class="stat-sub">' + esc(ranText(last)) + " · " + attempts.length + " attempt" + (attempts.length === 1 ? "" : "s") +
          " · " + esc(total === null ? "duration unknown" : fmt.duration(total)) + "</span>" : "") + "</div>" +
        (attempts.length ? '<ul class="attempts">' + attempts.map(function (a, i) {
          // The flow's pause names its stopped step: its last attempt is the paused one.
          var paused = options.pause && flow.stop && flow.stop.step === step.id && i === attempts.length - 1;
          var later = attempts.slice(i + 1).filter(function (b) { return b.status === "done"; })[0];
          // On a flow a dashboard resumed: the attempt that ran again after a stopped one.
          var resumed = !!options.resumed && i > 0 && attempts[i - 1].status !== "done";
          return attemptHtml(a, paused ? options.pause : null, link, later ? later.attempt : null, resumed);
        }).join("") + "</ul>" : "") +
        // The step in flight is described in its own block, a paused one in the Result (said
        // once, with its command); never "not run yet".
        ((status.key === "paused" || (last && last.pause)) && options.pause
          ? '<div class="stat-sub pipe-pause" data-state="paused" data-resume="' + esc(pauseResume(options.pause, options.id).resume) + '">' +
            "paused — " + esc(pauseShortText(options.pause)) +
            (link ? ', see <a href="#' + esc(link.id) + '" data-scroll-to="' + esc(link.id) + '">' + esc(link.label) + "</a>" : "") + "</div>"
          : (status.key === "current" || status.key === "paused"
            ? '<div class="stat-sub pipe-live-note">' + (status.key === "current" ? "in flight" : "paused") +
              (live && live.step === step.id && typeof live.turns === "number" ? " · turn " + esc(live.turns) : "") +
              (status.key === "current" ? " · details in the step block above" : "") + "</div>"
            : status.key === "interrupted" ? stoppedAttemptHtml(stopped)
            : (attempts.length ? "" : ' <span class="stat-sub">not run yet</span>'))) +
        (passes.length ? '<div class="pipe-pass stat-sub">passes on: ' + passes.map(function (e) {
          return esc(e.type) + " → " + esc(e.to_step);
        }).join(", ") + "</div>" : "") + "</li>";
    }).join("") + "</ol>" +
    (edges.length ? '<h3>Artifact edges</h3><ul id="flow-edges">' + edges.map(function (e) {
      // Each round passes its own artifact, so the attempts and digest tell two apart.
      return "<li>" + esc(e.from_step) + edgeAttempt(e.from_attempt) + " → " + esc(e.to_step) +
        edgeAttempt(e.to_attempt) + " · " + esc(e.type) +
        (e.digest ? ' <code class="stat-sub">' + esc(String(e.digest).slice(0, 8)) + "</code>" : "") + "</li>";
    }).join("") + "</ul>" : "") + "</section>"
  );
}

/* --- a flow step in flight -------------------------------------------------------------
 * Everything here is the shared live view, `flow.in_flight` (read_model.flow_in_flight,
 * also `garuda flow show --json` `live`), as it is: this file reads no journal or progress
 * file of its own. A value the reader returns as null reads "not recorded", never 0. The
 * agent's own progress note is its words, shown as escaped text and labelled as such. */

/* The step attempt in flight, or null (no live view, or the flow is paused). */
function flowInFlight(flow) {
  var live = flow && flow.in_flight;
  if (!live || live.paused || !live.step) return null;
  // The reader says whether the attempt runs now (#873); an open attempt whose process died
  // is not "in flight". A reader without the field keeps the old inference.
  return live.running === false ? null : live;
}

function flowPaused(flow) {
  var live = flow && flow.in_flight;
  return live && live.paused ? live.paused : null;
}

var LIVE_DECISIONS = {
  next: "continued with another turn", rotate: "rotated to a fresh harness session",
  done: "reported the step done", stop: "stopped the loop", repair: "ran a repair turn"
};
var LIVE_STOP_REASONS = {
  end_turn: "the model ended its turn", max_tokens: "the turn hit its output-token limit",
  max_turn_requests: "the turn hit its request limit", refusal: "the model refused",
  cancelled: "the turn was cancelled"
};
var LIVE_NOTE_PREVIEW = 160;

/* Step sessions' identity (role → harness/model), read once per session from the sessions
 * list's own row; the page asks for it when a new step session appears. */
var LIVE_IDENTITY = {};

function liveWords(map, value) {
  if (value === null || value === undefined || value === "") return null;
  return map[value] || String(value);
}

function liveWhenHtml(iso) {
  return iso ? relativeTimeHtml(iso) + ' <span class="stat-sub">(' + esc(absoluteTime(iso)) + ")</span>"
             : '<span class="stat-sub">not recorded</span>';
}

function liveMinutes(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds)) return "unknown";
  if (seconds < 60) return "under a minute";
  var minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + " min";
  var hours = Math.floor(minutes / 60);
  return hours + "h " + (minutes % 60) + "m";
}

function liveContextHtml(used, size) {
  if (typeof used !== "number") return '<span class="stat-sub">not reported</span>';
  if (typeof size !== "number" || size <= 0) {
    return esc(fmt.tokens(used)) + ' <span class="stat-sub">of an unreported window</span>';
  }
  var pct = Math.round(100 * used / size);
  return '<meter class="live-meter" min="0" max="' + esc(size) + '" value="' + esc(Math.min(used, size)) + '"' +
    ' aria-label="context occupancy">' + esc(pct) + "%</meter> " +
    esc(fmt.tokens(used) + " of " + fmt.tokens(size) + " (" + pct + "%)");
}

function liveDeadlineHtml(iso) {
  if (!iso) return '<span class="stat-sub">no deadline set</span>';
  var at = new Date(iso).getTime();
  if (isNaN(at)) return '<span class="stat-sub">not recorded</span>';
  var left = (at - Date.now()) / 1000;
  return esc(left > 0 ? liveMinutes(left) + " left" : "passed " + liveMinutes(-left) + " ago") +
    ' <span class="stat-sub">(' + esc(absoluteTime(iso)) + ")</span>";
}

function liveCheckpointText(cp) {
  if (!cp) return null;
  return "#" + knownText(cp.n) + (cp.label ? " " + cp.label : "") +
    (cp.step ? " · step " + cp.step + (cp.attempt ? " attempt " + cp.attempt : "") : "") +
    (cp.at ? " · " + fmt.when(cp.at) : "") + (cp.pruned ? " · pruned" : "");
}

function liveFact(key, label, html) {
  return '<div class="live-fact" data-live="' + esc(key) + '"><dt>' + esc(label) + "</dt><dd>" + html + "</dd></div>";
}

/* --- silence and recent turns (#867): `silence_state`, the limits, `recent_turns` ------------
 * The reader's own judgement, shown as a state; every field may be absent (only ACP flow
 * steps with limits or long_run report tool and approval state), and then nothing is said. */
var QUIET_STATES = {
  ok: { pill: "running", state: "running", word: "within its limit" },
  tool_running: { pill: "running", state: "running", word: "a tool call is running; the longer tool limit applies" },
  approval_parked: { pill: "", state: "waiting", word: "waiting on an approval; silence is not counted" },
  warn: { pill: "", state: "waiting", word: "nearing its silence limit; the watchdog stops the step at the limit" },
  over: { pill: "failed", state: "failed", word: "past its silence limit" }
};

/* The silence limit that applies now: the tool limit while a tool call runs. */
function quietLimit(live) {
  var limit = live.tool_in_flight ? live.tool_silence_limit_sec : live.silence_limit_sec;
  return typeof limit === "number" && limit > 0 ? limit : null;
}

function quietText(live) {
  var limit = quietLimit(live);
  if (typeof live.idle_sec !== "number") return limit ? "limit " + liveMinutes(limit) : "";
  return "quiet for " + liveMinutes(live.idle_sec) + (limit ? " of a " + liveMinutes(limit) + " limit" : "");
}

function liveQuietFact(live) {
  var state = live.silence_state;
  var known = state ? QUIET_STATES[state] : null;
  if (!state) return "";  // the reader did not judge it: say nothing rather than guess
  var text = quietText(live);
  var word = known ? known.word : "silence state not recognised";
  // Without a silence limit (a step with no limits.silence_min) there is no limit to be
  // within: say so rather than imply one.
  if (!quietLimit(live) && (state === "ok" || state === "tool_running")) {
    word = (state === "tool_running" ? "a tool call is running; " : "") + "no silence limit is set for this step";
  }
  return liveFact("quiet", "Quiet",
    '<span class="pill ' + esc(known ? known.pill : "unknown") + '" data-state="' + esc(known ? known.state : "unknown") +
    '" data-silence="' + esc(state) + '">' + esc(state.replace(/_/g, " ")) + "</span> " +
    (text ? esc(text) + " · " : "") + '<span class="stat-sub">' + esc(word) + "</span>");
}

/* One recent turn in plain words: why the turn ended, then what the loop decided. */
function turnWords(turn) {
  var reason = liveWords(LIVE_STOP_REASONS, turn.stop_reason);
  var decision = liveWords(LIVE_DECISIONS, turn.decision);
  var words = [reason, decision ? "then " + decision : null].filter(Boolean).join("; ");
  return words || "not recorded";
}

function liveTurnRowHtml(turn) {
  return '<tr data-turn="' + esc(turn.turn) + '"><td data-th="Turn">' + esc(knownText(turn.turn)) + "</td>" +
    '<td data-th="Harness session">' + esc(knownText(turn.harness_session)) + "</td>" +
    '<td data-th="What happened">' + esc(turnWords(turn)) + "</td>" +
    '<td class="num" data-th="Duration">' + esc(typeof turn.elapsed_sec === "number" ? fmt.duration(turn.elapsed_sec * 1000) : "not recorded") + "</td>" +
    '<td class="num" data-th="Files changed">' + esc(knownText(turn.changed_total)) + "</td>" +
    '<td class="num" data-th="Context">' + esc(typeof turn.context_used === "number" ? fmt.tokens(turn.context_used) : "not recorded") + "</td></tr>";
}

function liveTurnsTable(rows, id) {
  return '<div class="table-wrap"><table class="table table-stack live-turns"' + (id ? ' id="' + id + '"' : "") + '><thead><tr>' +
    '<th>Turn</th><th>Harness session</th><th>What happened</th><th class="num">Duration</th>' +
    '<th class="num">Files changed</th><th class="num">Context</th></tr></thead><tbody>' +
    rows.map(liveTurnRowHtml).join("") + "</tbody></table></div>";
}

/* The attempt's recent turns, newest first: the last three shown, the rest one click away. */
function liveTurnsHtml(turns) {
  if (!Array.isArray(turns) || !turns.length) return "";
  var newest = turns.slice().reverse();
  var shown = newest.slice(0, 3), older = newest.slice(3);
  return '<div class="live-turns-block"><h3>Recent turns</h3>' + liveTurnsTable(shown, "live-turns") +
    (older.length ? '<details class="live-turns-more" data-key="live-turns-more"><summary>' + esc(older.length) +
      " earlier turn" + (older.length === 1 ? "" : "s") + "</summary>" + liveTurnsTable(older, "live-turns-older") + "</details>" : "") +
    "</div>";
}

/* The paused step: why, when it resumes, and how (only where the engine says it is resumable). */
function flowPausedBlockHtml(live, flowId) {
  var paused = live.paused || {};
  var known = FLOW_STOPS[paused.code] || {};
  var resume = paused.code === "flow.quota_wait" ? "Quota recovery resumes the flow on its own once the limit resets."
    : (known.resume ? "Resume with: garuda flow resume " + flowId : "");
  return '<div class="live-step live-paused" id="flow-live" data-state="paused">' +
    '<p class="live-paused-reason"><span class="pill paused" data-state="paused">paused</span> <strong>Paused</strong>' +
    (paused.step || live.step ? " at step " + esc(paused.step || live.step) : "") +
    (live.attempt ? " (attempt " + esc(live.attempt) + ")" : "") + ": " +
    esc(humanStamps(paused.message || known.text || paused.code || "no reason recorded")) + "</p>" +
    '<dl class="live-facts">' +
    (typeof paused.reset_at === "number"
      ? liveFact("reset_at", "Reset", esc(pauseResetText(paused.reset_at)) + ' <span class="stat-sub">(' +
          esc(new Date(paused.reset_at * 1000).toLocaleString()) + ")</span>") : "") +
    (typeof paused.next_check === "number"
      ? liveFact("next_check", "Next check", esc(untilEpochText(paused.next_check)) + ' <span class="stat-sub">(' +
          esc(new Date(paused.next_check * 1000).toLocaleString()) + ")</span>") : "") +
    liveFact("code", "Stop code", "<code>" + esc(paused.code || "unknown") + "</code>") +
    "</dl>" + (resume ? '<p class="live-resume"><code>' + esc(resume) + "</code></p>" : "") + "</div>";
}

/* The block for a flow's step in flight, or paused: the session page's "Step in flight" card
 * and the flow trace page's In progress card draw it through this one function. */
function flowLiveHtml(row) {
  var flow = row && row.flow;
  var live = flow && flow.in_flight;
  if (!live) {
    // A running flow whose reader returned nothing: an older flow, or unreadable records.
    return isActive((row || {}).state) && flow
      ? '<div class="live-step" id="flow-live" data-state="none"><p class="stat-sub">No live detail ' +
        "recorded for this step (an older flow, or records the reader could not read).</p></div>"
      : "";
  }
  // A paused flow is not running: its pause is said once, in the Result. A stop the read
  // model does not call a pause (a quarantine) keeps its block here.
  if (live.paused) return row.pause ? "" : flowPausedBlockHtml(live, row.session_id);
  // Not running now (its process is gone): no "Step in flight" card, no stale quiet line.
  if (live.running === false) return "";
  var sid = live.session_id;
  var identity = sid ? LIVE_IDENTITY[sid] : null;
  var elapsed = live.started_at && !isNaN(new Date(live.started_at).getTime())
    // Whole minutes: the block's text changes at most once a minute between turns.
    ? liveMinutes(Math.max(0, (Date.now() - new Date(live.started_at).getTime()) / 1000)) : null;
  var turns = typeof live.turns === "number"
    ? esc(live.turns + " turn" + (live.turns === 1 ? "" : "s")) +
      " · " + esc(knownText(live.rotations) + " rotation" + (live.rotations === 1 ? "" : "s")) +
      " · " + esc(knownText(live.continuations) + " continuation" + (live.continuations === 1 ? "" : "s")) +
      (typeof live.harness_sessions === "number" ? ' <span class="stat-sub">in ' +
        esc(live.harness_sessions) + " harness session" + (live.harness_sessions === 1 ? "" : "s") + "</span>" : "")
    : '<span class="stat-sub">no turn has finished yet</span>';
  var decision = liveWords(LIVE_DECISIONS, live.last_decision);
  var reason = liveWords(LIVE_STOP_REASONS, live.last_stop_reason);
  var lastTurn = decision || reason
    ? esc([reason ? "the turn ended because " + reason : null, decision ? "the step " + decision : null]
        .filter(Boolean).join("; ")) + (live.last_turn_at ? ' <span class="stat-sub">· ' + esc(fmt.when(live.last_turn_at)) + "</span>" : "")
    : '<span class="stat-sub">not recorded</span>';
  var activity = live.last_activity_at
    ? relativeTimeHtml(live.last_activity_at) + (typeof live.idle_sec === "number"
        ? ' <span class="stat-sub">· idle ' + esc(liveMinutes(live.idle_sec)) + "</span>" : "")
    : '<span class="stat-sub">not recorded</span>';
  var note = live.last_note;
  var noteHtml = note
    ? liveFact("note", "Agent's note",
        '<span class="stat-sub live-note-label">the agent\'s own words, not a Garuda fact</span>' +
        (note.length > LIVE_NOTE_PREVIEW
          ? '<details class="live-note" data-key="live-note"><summary>' + esc(fmt.truncate(note, LIVE_NOTE_PREVIEW)) +
            "</summary><p>" + esc(note) + "</p></details>"
          : '<p class="live-note">' + esc(note) + "</p>"))
    : "";
  var checkpoint = liveCheckpointText(live.last_checkpoint);
  return '<div class="live-step" id="flow-live" data-state="running" data-step="' + esc(live.step) + '">' +
    '<p class="live-step-head"><span class="pill running" data-state="running">running</span> <strong>' +
    esc(live.step) + "</strong> · attempt " + esc(knownText(live.attempt)) +
    " · " + (elapsed ? "running for " + esc(elapsed) : "start not recorded") +
    (sid ? ' · <a href="#/sessions/' + encodeURIComponent(sid) + '">step session</a> · <a href="#/runs/' +
      encodeURIComponent(sid) + '">trace</a>' : ' · <span class="stat-sub">step session not created yet</span>') + "</p>" +
    '<dl class="live-facts">' +
    liveFact("who", "Runs as", identity ? esc(identity) : '<span class="stat-sub">' +
      (!sid ? "not recorded yet" : (identity === false ? "reading the step session…"
        : (identity === "" ? "not recorded" : "shown on the step session's page"))) + "</span>") +
    liveFact("started", "Started", liveWhenHtml(live.started_at)) +
    liveFact("turns", "Turns", turns) +
    liveFact("last_turn", "Last turn", lastTurn) +
    liveFact("activity", "Last activity", activity) +
    liveFact("context", "Context occupancy", liveContextHtml(live.context_used, live.context_size)) +
    liveFact("deadline", "Deadline", liveDeadlineHtml(live.deadline_at)) +
    liveQuietFact(live) +
    (checkpoint ? liveFact("checkpoint", "Last checkpoint", esc(checkpoint)) : "") +
    noteHtml + "</dl>" + liveTurnsHtml(live.recent_turns) +
    '<p class="stat-sub live-foot">Context occupancy is how full the harness reported its window, not billed ' +
    "tokens. Recorded by the flow's own progress file and launch record; nothing here is estimated.</p></div>";
}

/* The session page's card around the block. */
function sessionLiveStepHtml(row) {
  var block = flowLiveHtml(row);
  if (!block) return "";
  var paused = flowPaused(row.flow);
  return '<section class="card live-step-card" id="session-live-step" data-state="' + (paused ? "paused" : "running") + '"><h2>' +
    (paused ? "Step paused" : "Step in flight") + "</h2>" + block + "</section>";
}

/* Fetch the in-flight step session's identity once (role → harness/model), then repaint. */
function loadLiveIdentity(page) {
  var live = page.row && flowInFlight(page.row.flow);
  var sid = live && live.session_id;
  if (!sid || sid in LIVE_IDENTITY) return;
  LIVE_IDENTITY[sid] = false;  // reading
  api("/api/sessions?limit=1&q=" + encodeURIComponent(sid)).then(function (payload) {
    var match = (payload.sessions || []).filter(function (r) { return r.session_id === sid; })[0];
    var role = match && match.role && match.role.name;
    LIVE_IDENTITY[sid] = match ? (role ? role + " → " : "") + (match.runtime || "native") +
      (match.model ? "/" + match.model : "") : "";
    if (STATE.sessionPage === page && page.row) paintHost("session-live-step-host", sessionLiveStepHtml(page.row), { keepSelection: true });
  }, function () { delete LIVE_IDENTITY[sid]; });
}

function failureEvidenceHtml(evidence) {
  if (!evidence) return "";
  var original = evidence.original_cause || {status: "unavailable"};
  var value = original.value || {};
  var detail = value.detail && value.detail.value ? value.detail.value : original.reason;
  var later = evidence.subsequent_failures && evidence.subsequent_failures.value || [];
  return '<section class="failure-evidence" id="failure-evidence"><h2>Failure evidence</h2>' +
    '<p>' + chip("original cause", original.status, original.status === "present" ? "chip-on" : "chip-off") +
    (value.code ? ' <code>' + esc(value.code) + '</code> · ' + esc(value.phase) + ' · ' + esc(value.boundary) : '') + '</p>' +
    (detail ? '<pre class="failure-detail">' + esc(detail) + '</pre>' : '') +
    (later.length ? '<h3>Later failures and refusals</h3><ul>' + later.map(function (item) {
      return '<li><code>' + esc(item.code) + '</code> · ' + esc(item.phase) + ' · ' +
        esc(item.kind || "failure") + '</li>';
    }).join("") + '</ul>' : '') +
    '<p class="stat-sub">Coverage labels describe retained evidence. Inspection is read-only and never replays work.</p></section>';
}

function artifactProjectionHtml(projection) {
  if (!projection) return "";
  var refs = projection.references || [];
  var omitted = projection.omitted_references || 0;
  var incomplete = projection.coverage && projection.coverage !== "complete";
  return '<section class="artifacts" id="artifact-projection"><h2>Artifact references</h2>' +
    (refs.length ? '<ul>' + refs.map(function (ref) {
      var owner = ref.owner || {};
      var availability = ref.availability || {state: "unknown", integrity: "unknown"};
      var reason = availability.reason ? ' · ' + availability.reason : '';
      var view = ref.omitted_view_bytes === null ? ' · view omissions unknown' :
        (ref.omitted_view_bytes > 0 ? ' · view omits ' + ref.omitted_view_bytes + ' bytes' : '');
      var withheld = ref.field_coverage && Object.keys(ref.field_coverage).length ?
        ' · some metadata withheld' : '';
      return '<li><code>' + esc(owner.kind || "artifact") + ':' + esc(owner.reference || "unknown") +
        '</code> · ' + chip("capture", ref.completeness || "unknown",
          ref.completeness === "complete" ? "chip-on" : "chip-off") + ' ' +
        chip("availability", availability.state || "unknown",
          availability.state === "available" ? "chip-on" : "chip-bad") +
        ' <span class="stat-sub">integrity ' + esc(availability.integrity || "unknown") +
        esc(reason) + esc(view) + esc(withheld) + '</span></li>';
    }).join("") + '</ul>' : '<p class="stat-sub">' +
      (incomplete ? 'Artifact references could not be fully inspected.' : 'No retained artifact references.') + '</p>') +
    (omitted ? '<p class="stat-sub">' + (projection.omission_count_status === 'lower-bound' ? 'At least ' : '') +
      esc(omitted) + ' references omitted by the read bound.</p>' : '') +
    (incomplete ? '<p class="stat-sub">Some artifact evidence is unavailable.</p>' : '') +
    '<p class="stat-sub">Completeness describes the captured source; retention is best effort.</p></section>';
}

function ownershipHtml(view) {
  if (!view) return "";
  var owner = view.owner || {};
  var progress = view.progress || {};
  var rec = view.reconciliation || {};
  var leases = view.workspace_leases || {};
  var workers = view.editing_workers || {};
  var waiting = (progress.waiting || []).map(function (w) {
    return w.reason + (w.position ? " #" + w.position : "") + (w.count ? " (" + w.count + ")" : "") +
      (w.operation ? " · " + w.operation : "");
  });
  var coverage = ["workspace_leases", "queue", "capacity"].filter(function (k) {
    return view[k] && view[k].coverage === "unavailable";
  });
  return '<section class="ownership" id="session-ownership"><h2>Ownership</h2>' +
    '<p>' + chip("owner", owner.liveness || "unknown",
      owner.liveness === "alive" ? "chip-on" : (owner.liveness === "dead" ? "chip-bad" : "chip-off")) + ' ' +
    chip("reconciliation", rec.status || "unknown", rec.status === "eligible" ? "chip-on" : "chip-off") + ' ' +
    chip("leases", leases.coverage === "complete" ? String((leases.holders || []).length) : "unknown") + '</p>' +
    (waiting.length ? '<p class="stat-sub">Waiting on: ' + esc(waiting.join(", ")) + '</p>' : '') +
    (workers.coverage && workers.coverage !== "complete" ?
      '<p class="stat-sub">Editing workers: unknown (incomplete inventory)</p>' : '') +
    (workers.entries || []).filter(function (w) { return w.repository_state !== "removed"; }).map(function (w) {
      return '<p class="stat-sub">Editing worker <code>' + esc(w.candidate_id) + '</code>: ' +
        esc(w.state) + ' · cleanup ' + esc(w.cleanup) + ' · ownership ' + esc(w.ownership) +
        '<br>Repository: <code>' + esc(w.workspace) + '</code></p>';
    }).join('') +
    ((rec.blockers || []).length ? '<p class="stat-sub">Blocked by: ' + esc(rec.blockers.join(", ")) + '</p>' : '') +
    ((rec.retained || []).length ? '<p class="stat-sub">Still retained: ' + esc(rec.retained.join(", ")) + '</p>' : '') +
    (rec.path ? '<p class="stat-sub">Existing path: <code>' + esc(rec.path.command) + '</code></p>' : '') +
    (coverage.length ? '<p class="stat-sub">Unreadable: ' + esc(coverage.join(", ")) + '</p>' : '') +
    '<p class="stat-sub">Inspection is read-only: it starts, stops and repairs nothing. ' +
    'The named path re-proves every prerequisite itself.</p></section>';
}

/* What the harness itself reported, labelled as such: an external session's adapter figure, or
 * for a flow the total over its step sessions. Garuda's own metered usage stays the
 * usage/cost chips above, which stay "unknown" when Garuda metered nothing. */
function reportedUsageHtml(row) {
  var flowTotal = row.kind === "flow" ? row.step_reported_usage : null;
  var rep = row.reported_usage;
  var text = null;
  if (flowTotal) {
    if (flowTotal.coverage !== "complete") text = "unknown (usage ledger unreadable)";
    else if (!flowTotal.step_sessions) {
      // Step sessions are counted from settled receipts: a step still in flight is not one yet.
      text = isActive(row.state) ? "none yet — no step has settled" : "no step session recorded";
    }
    else if (!flowTotal.reporting_sessions) text = "not reported by any of " + flowTotal.step_sessions + " step session(s)";
    else text = (typeof flowTotal.cost_usd === "number" ? "$" + flowTotal.cost_usd.toFixed(4) + (flowTotal.cost_complete ? "" : " (incomplete)") : "cost not reported") +
      " · " + flowTotal.reporting_sessions + " of " + flowTotal.step_sessions + " step sessions reporting" +
      (flowTotal.models.length ? " · " + flowTotal.models.join(", ") : "");
  } else if (rep) {
    text = (typeof rep.cost_usd === "number" ? "$" + rep.cost_usd.toFixed(4) : "cost not reported") +
      (rep.models && rep.models.length ? " · " + rep.models.join(", ") : "") +
      (rep.context_used !== null && rep.context_used !== undefined ? " · context " + rep.context_used + "/" + (rep.context_size || "?") : "");
  }
  if (text === null) return "";
  return '<p class="stat-sub" id="session-reported-usage">' + (flowTotal ? "Step sessions' reported usage: " : "Harness-reported usage: ") +
    esc(text) + " — adapter-reported, not metered by Garuda</p>";
}

/* The panels a session has nothing for, folded into one quiet line instead of empty cards. */
function quietSessionHtml(row, streamable) {
  var parts = [];
  if (!row.spend) parts.push("no spend snapshot (spend gates are recorded for native run trees)");
  if (!streamable) {
    parts.push(row.kind === "flow" ? "no event log of its own: each step's session below has its own"
      : (row.event_log && row.event_log.external ? "no native event log: the harness's own trail is on the Trace page"
                                                 : "no event log recorded"));
  }
  return parts.length ? '<p class="stat-sub" id="session-quiet">' + esc(parts.join(" · ")) + "</p>" : "";
}

function sessionFailed(row) {
  var outcome = (row.state || {}).outcome;
  return !!row.crashed || outcome === "failed" || outcome === "refused";
}

function sessionStreamable(row) {
  // A flow has no event log of its own: its step in flight has the live block instead of a
  // panel that would wait forever ("connecting…") on a log that never exists.
  if (row.kind === "flow") return false;
  return !row.event_log || row.event_log.native || isActive(row.state);
}

/* A flow step's stop code in one plain sentence. `resume`: the engine lists it among its
 * resumable checked stops (RESUMABLE_CHECKED_STOPS), so `garuda flow resume` runs the step
 * again. Codes not listed fall back to the recorded message. */
var FLOW_STOPS = {
  "flow.step_deadline": { text: "The step ran past its deadline (limits.deadline_min).", resume: true },
  "flow.step_silent": { text: "The step's harness went silent for longer than its silence limit.", resume: true },
  "flow.step_no_progress": { text: "The long step paused: its turns made no measurable progress.", resume: true },
  "flow.step_max_turns": { text: "The long step paused: it used its whole turn budget.", resume: true },
  "flow.step_refused": { text: "The long step paused: its harness refused to continue.", resume: true },
  "flow.step_failed": { text: "The step failed.", resume: true },
  "flow.output_missing": { text: "The step finished without a usable output.", resume: true },
  "flow.interrupted": { text: "The step was interrupted by a signal after its workers were stopped.", resume: true },
  "flow.step_backstop": { text: "The step's worker did not stop in time, so the supervisor's backstop killed it and the step is quarantined.",
                          next: "Next: inspect the step's workspace; a step that runs this long must set limits.deadline_min." },
  "flow.quota_wait": { text: "The flow is waiting for its harness's usage limit to reset.",
                       next: "Next: nothing; quota recovery resumes it when the harness is available." }
};

/* When a usage-limit pause resets: the quota-recovery record's reset_at, or the stop's own
 * cause.limit.resets_at (epoch seconds); null when neither was recorded. */
function quotaResetAt(flow) {
  var recovery = (flow && flow.quota_recovery) || {};
  var limit = (flow && flow.stop && flow.stop.limit) || {};
  if (typeof recovery.reset_at === "number") return recovery.reset_at;
  return typeof limit.resets_at === "number" ? limit.resets_at : null;
}

function flowStopSentence(stop, sessionId, flow) {
  if (!stop || !stop.code) return null;
  var reset = stop.code === "flow.quota_wait" ? quotaResetAt(flow) : null;
  if (reset !== null) {
    return { cause: "Paused: usage limit, resets " + untilEpochText(reset) + " (" + new Date(reset * 1000).toLocaleString() + ").",
             next: "Next: nothing; quota recovery resumes the flow once the limit has reset." };
  }
  var known = FLOW_STOPS[stop.code];
  var where = stop.step ? " (step " + stop.step + ")" : "";
  if (!known) {
    return { cause: "The flow stopped" + where + ": " + (stop.message || stop.code) + " (" + stop.code + ").",
             next: "Next: open the stopped step's session below to see what it did." };
  }
  return { cause: known.text.replace(/\.$/, "") + where + ".",
           next: known.resume ? "Next: garuda flow resume " + sessionId + " runs the step again." : known.next };
}

/* A flow whose process stopped mid-step (`flow_state: interrupted`) records no flow_stop: say
 * what is recorded. An attempt settled as flow.interrupted is run again by `flow resume` (the
 * engine lists it as resumable, as FLOW_STOPS says); a launched attempt with no receipt is
 * never replayed (engine.recover quarantines it; a checked flow's crash continuation settles
 * it and goes on with the next step). */
function interruptedSentence(row) {
  var flow = row.flow || {};
  if (flow.state !== "interrupted") return null;
  var open = flow.in_flight && !flow.in_flight.paused && flow.in_flight.step ? flow.in_flight : null;
  if (open) {
    return { cause: "Interrupted: the flow's process stopped while step " + open.step +
               (open.attempt ? " (attempt " + open.attempt + ")" : "") + " was running, and that attempt recorded no outcome.",
             next: "Next: open the step's session below to see what it did; garuda flow resume " + row.session_id +
               " does not run it again (a launched step without a recorded outcome is never replayed)." };
  }
  var last = null;
  (flow.steps || []).forEach(function (step) {
    (step.attempts || []).forEach(function (a) { if (a && a.stop) last = { step: step.id, attempt: a }; });
  });
  if (!last) {
    return { cause: "Interrupted: the flow's process stopped; no step attempt recorded why.",
             next: "Next: garuda flow show " + row.session_id + " lists what was recorded." };
  }
  var code = last.attempt.stop.code;
  var known = FLOW_STOPS[code] || {};
  return { cause: "Interrupted: the flow's process stopped before step " + last.step + " finished" +
             (code ? " (" + code + ")" : "") + ".",
           next: known.resume ? "Next: garuda flow resume " + row.session_id + " runs it again."
             : "Next: garuda flow resume " + row.session_id + " does not run this stop again; open the step's session below." };
}

/* Why it failed, in one plain sentence from what was recorded, and what to do next. A flow
 * a dashboard started resumes from its session page: `garuda flow resume` refuses it. */
function failureSentence(row) {
  var said = recordedFailureSentence(row);
  if (row.dashboard_operation && said && /garuda flow resume/.test(said.next || "")) {
    said = { cause: said.cause,
             next: "Next: resume it with Resume on its session page (garuda flow resume refuses a flow a dashboard started)." };
  }
  return said;
}

function recordedFailureSentence(row) {
  var evidence = (row.failure_evidence || {}).original_cause || {};
  var value = evidence.status === "present" ? (evidence.value || {}) : {};
  var detail = value.detail && value.detail.value ? firstLine(value.detail.value) : "";
  var flowStop = row.flow && row.flow.stop;
  if (row.pause) {
    var how = pauseResume(row.pause, row.session_id);
    return { cause: "Paused: " + pauseText(row.pause, flowStop) + ".",
             next: "Next (resume: " + how.resume + "): " + how.text + (how.command ? ": " + how.command : "") + "." };
  }
  var worded = row.kind === "flow" && flowStop && FLOW_STOPS[flowStop.code] ? flowStopSentence(flowStop, row.session_id, row.flow) : null;
  if (worded) return worded;
  var interrupted = row.kind === "flow" && !flowStop ? interruptedSentence(row) : null;
  if (interrupted) return interrupted;
  if (row.crashed) {
    return { cause: "The worker process is gone, and it exited without recording an outcome.",
             next: (row.ownership && row.ownership.reconciliation && row.ownership.reconciliation.path)
               ? "Next: reconcile it with " + row.ownership.reconciliation.path.command + "."
               : "Next: look at Runtimes for recovery, or start the task again." };
  }
  var cause;
  if (value.code) cause = "It failed with " + value.code + (value.phase ? " during " + value.phase : "") + (detail ? ": " + fmt.truncate(detail, 220) : ".");
  else if (row.error && row.error.code) cause = "It failed with " + row.error.code + (row.error.detail ? ": " + fmt.truncate(firstLine(row.error.detail), 220) : ".");
  else if (flowStop && (flowStop.code || flowStop.message)) cause = "The flow stopped: " + (flowStop.message || flowStop.code) + (flowStop.code && flowStop.message ? " (" + flowStop.code + ")." : ".");
  else cause = (row.state || {}).outcome === "refused" ? "Garuda refused to run it; no reason was recorded." : "It failed; no cause was recorded.";
  var next;
  if (row.flow && row.flow.quota_recovery) next = "Next: quota recovery is " + row.flow.quota_recovery.state + "; the flow resumes when the harness is available.";
  else if (row.kind === "flow") next = "Next: open the failed step's session below to see what it did.";
  else if (row.event_log && (row.event_log.native || row.event_log.external)) next = "Next: open the Trace to see the turn where it went wrong, then start it again.";
  else next = "Next: start the task again; this session recorded no event log to inspect.";
  return { cause: cause, next: next };
}

function sessionHeadHtml(page) {
  var row = page.row, state = row.state || {};
  var stop = page.stop;
  var stopHtml = "";
  if (row.background && canAnswer() && (isActive(state) || stop.phase === "sending")) {
    if (stop.phase === "confirm") {
      stopHtml = '<span class="stop-confirm" id="session-stop-box">Stop this session? ' +
        '<button type="button" class="btn btn-danger" id="session-stop-confirm">Stop session</button> ' +
        '<button type="button" class="btn" id="session-stop-cancel">Cancel</button></span>';
    } else {
      stopHtml = '<button type="button" class="btn" id="session-stop"' + (stop.phase === "sending" ? " disabled" : "") + ">" +
        (stop.phase === "sending" ? "Stopping…" : "Stop") + "</button>";
    }
  }
  if (stop.message) {
    stopHtml += ' <span class="stat-sub' + (stop.phase === "error" ? " err-text" : "") + '" id="session-stop-result">' + esc(stop.message) + "</span>";
  }
  var labelCls = row.pause ? "paused" : (row.crashed || sessionFailed(row) ? "failed" : (isActive(state) ? "running" : (state.outcome === "completed" ? "success" : "unknown")));
  var label = row.pause ? "paused" : row.label;
  return '<h1>' + esc(row.name || String(row.session_id).slice(0, 8)) + "</h1>" +
    '<span class="pill ' + labelCls + '" id="session-label" data-state="' + esc(label) + '">' + esc(label) + "</span>" +
    (row.pause ? '<span class="stat-sub" id="session-pause">' + esc(pauseText(row.pause, row.flow && row.flow.stop)) + "</span>" : "") +
    (row.kind === "flow" ? '<span class="pill unknown">flow</span>' : "") +
    '<a class="btn" href="#/runs/' + encodeURIComponent(row.session_id) + '">Trace</a>' +
    '<a class="btn" href="' + esc(sessionsHash(STATE.filters.sessions)) + '">All sessions</a>' +
    // Stop comes last: its confirmation changes width, and nothing after it may move under
    // the pointer (a second click once landed on the link that slid into its place).
    (stopHtml ? '<span class="session-stop-area">' + stopHtml + "</span>" : "") +
    (page.syncError ? '<span class="stat-sub err-text" id="session-sync">could not refresh: ' + esc(page.syncError) + "</span>"
                    : (isActive(state) ? '<span class="stat-sub" id="session-sync">live</span>' : ""));
}

function sessionOverviewHtml(row) {
  var state = row.state || {};
  var task = row.task ? String(row.task) : "";
  var lines = task.split("\n");
  var preview = lines.slice(0, TASK_PREVIEW_LINES).join("\n");
  if (preview.length > 400) preview = preview.slice(0, 399) + "…";
  var longer = preview !== task;
  var times = "Started " + relativeTimeHtml(row.created_at) + ' <span class="stat-sub">(' + esc(absoluteTime(row.created_at)) + ")</span>";
  if (isActive(state)) {
    times += " · running" + (row.created_at ? " for " + esc(fmt.duration(Math.max(0, Date.now() - new Date(row.created_at).getTime()))) : "");
  } else if (row.ended_at) {
    times += " · Ended " + relativeTimeHtml(row.ended_at) + ' <span class="stat-sub">(' + esc(absoluteTime(row.ended_at)) + ")</span>" +
      " · took " + esc(fmt.duration(row.duration_ms));
  } else {
    times += " · end not recorded · duration unknown";
  }
  return '<section class="card session-overview" id="session-overview">' +
    (task ? '<div class="session-task" id="session-task"><div class="stat-label">Task</div><p class="task-preview">' + esc(preview) + "</p>" +
      (longer ? '<details data-key="task"><summary>Show the full task</summary><pre class="task-full">' + esc(task) + "</pre></details>" : "") + "</div>"
          : '<p class="stat-sub" id="session-task">Task not recorded.</p>') +
    '<p class="session-line" id="session-identity">' + esc(identityText(row)) + (row.agent ? ' · agent ' + esc(row.agent) : "") + "</p>" +
    flowStepLinkHtml(row.flow_step) +
    '<p class="session-line stat-sub" id="session-where" title="' + esc(row.workspace || "") + '">' + esc(row.workspace || "workspace not recorded") +
      (row.branch ? " @ " + esc(row.branch) : "") + (row.isolation && row.isolation !== "shared" ? " (" + esc(row.isolation) + ")" : "") +
      (row.queue ? " · queue: " + esc(queueText(row.queue)) : "") + "</p>" +
    '<p class="session-line stat-sub" id="session-times">' + times + "</p></section>";
}

function sessionApprovalsHtml(row) {
  var approvals = row.approvals || [];
  if (!approvals.length) return "";
  var live = approvals.filter(function (a) { return !a.expired; }).length;
  return '<section class="notice approval" id="session-approvals-panel"><div class="notice-title">Waiting for you · ' + esc(live) + " approval" + (live === 1 ? "" : "s") +
    (approvals.length > live ? ' <span class="stat-sub">(' + esc(approvals.length - live) + " expired)</span>" : "") + "</div>" +
    '<ul id="session-approvals">' + approvals.map(function (a) {
      return "<li><code>" + esc(a.action) + '</code> <span class="stat-sub">' + esc(a.family) + " · ceiling " + esc(a.ceiling) +
        (a.expired ? " · expired — it will be denied" : "") + "</span></li>";
    }).join("") + '</ul><a class="btn btn-primary" href="#/inbox">Answer in Approvals</a></section>';
}

function sessionResultHtml(row) {
  var state = row.state || {};
  var body = "";
  if (row.final_message) {
    body += '<div class="session-final" id="session-final"><div class="stat-label">Final message <span class="stat-sub">(stored summary; may be clipped)</span></div>' +
      '<pre class="final-text">' + esc(row.final_message) + "</pre></div>";
  } else if (row.output_withheld) {
    body += '<p class="stat-sub" id="session-final">The output was withheld; it is not shown here.</p>';
  } else if (isActive(state)) {
    body += '<p class="stat-sub" id="session-final">Still running: the result appears here when it finishes.</p>';
  } else {
    body += '<p class="stat-sub" id="session-final">No final message was stored for this session.</p>';
  }
  var files = row.files_changed;
  if (files && files.files && files.files.length) {
    body += '<div id="session-files"><div class="stat-label">Files the agent recorded writing (' + esc(files.total) + ")</div>" +
      '<ul class="file-list">' + files.files.map(function (f) { return "<li><code>" + esc(f) + "</code></li>"; }).join("") + "</ul>" +
      (files.total > files.files.length ? '<p class="stat-sub">and ' + esc(files.total - files.files.length) + " more</p>" : "") +
      '<p class="stat-sub">From its working state; the Trace page has the workspace diff.</p></div>';
  }
  if (row.dashboard_operation && row.resume_check && row.kind === "flow" && row.resume_check.code !== "flow.resume_completed") {
    var ownedStop = row.flow && row.flow.stop;
    var ownedCause = row.pause ? "Paused: " + pauseText(row.pause, ownedStop) + "."
      : (sessionFailed(row) ? failureSentence(row).cause : null);
    var afterResume = (row.flow || {}).state === "quarantined" && row.dashboard_operation.resume_operation_id;
    body += '<div class="session-cause" id="session-cause"' + (row.pause ? ' data-state="paused"' : "") + ">" +
      (ownedCause ? '<p class="' + (row.pause ? "pause-text" : "err-text") + '">' + esc(ownedCause) + "</p>" : "") +
      (afterResume ? '<p class="err-text" id="session-quarantine">A resume of this flow could not prove its owned workers ' +
        "closed before its first step, so the flow is quarantined rather than refused: it is never resumed again. " +
        "Inspect the workspace (git status) and start a new run.</p>" : "") +
      (ownedStop && ownedStop.message ? '<p class="stat-sub">recorded stop: ' + esc(humanStamps(ownedStop.message)) + "</p>" : "") +
      resumeBlockHtml(row) + "</div>";
  } else if (row.pause) {
    var stopped = row.flow && row.flow.stop;
    body += '<div class="session-cause" id="session-cause" data-state="paused">' +
      pauseBlockHtml(row.pause, stopped, row.session_id, "session-pause-block") +
      (stopped && stopped.message ? '<p class="stat-sub">recorded stop: ' + esc(humanStamps(stopped.message)) + "</p>" : "") + "</div>";
  } else if (sessionFailed(row)) {
    var why = failureSentence(row);
    body += '<div class="session-cause" id="session-cause"><p class="err-text">' + esc(why.cause) + "</p>" +
      '<p class="stat-sub" id="session-next">' + esc(why.next) + "</p></div>";
  }
  return '<section class="card" id="session-result"><h2>Result</h2>' +
    // Outcome and verification are two facts: two chips, never one.
    '<div class="chips" id="session-facts">' + chip("outcome", state.outcome || "—", row.pause ? "chip-pause" : outcomeClass(state.outcome)) +
    verificationChip(row.verification) + selfCheckChip(row.self_check) +
    (row.flow && row.flow.review ? chip("review", reviewLabel(row.flow.review), "chip-off") : "") + "</div>" + body + "</section>";
}

function sessionUsageHtml(row, streamable) {
  return '<section class="card" id="session-usage"><h2>Usage</h2><div class="chips">' +
    chip("usage", row.usage) + chip("cost", row.cost) + "</div>" + reportedUsageHtml(row) +
    (row.spend ? '<div id="session-spend"><h3>Spend gate</h3><p class="stat-sub">' + esc(spendSummaryText(row.spend)) + '</p><div class="chips">' + spendHtml(row.spend) + "</div></div>" : "") +
    quietSessionHtml(row, streamable) + "</section>";
}

function sessionEvidenceHtml(row) {
  var state = row.state || {};
  return '<div class="evidence-block"><h2>State</h2><div class="chips" id="session-state-facts">' +
    chip("process", state.process) + chip("work", state.work) + chip("outcome", state.outcome || "—", outcomeClass(state.outcome)) +
    chip("queue", queueText(row.queue)) + "</div>" +
    '<p class="stat-sub">Session <code>' + esc(row.session_id) + "</code></p></div>" +
    ownershipHtml(row.ownership) + artifactProjectionHtml(row.artifacts) +
    (sessionFailed(row) && !row.pause ? "" : failureEvidenceHtml(row.failure_evidence));
}

/* Replace a host's HTML only when it changed, keeping what the user opened inside it open. */
/* Whether the user has text selected inside `node` (a repaint there would drop it). */
function selectionInside(node) {
  var selection = typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
  return !!(selection && !selection.isCollapsed && selection.anchorNode && node.contains(selection.anchorNode));
}

function paintHost(id, html, options) {
  var host = el(id);
  if (!host || host.__html === html) return;
  // In a block that repaints while the user reads it (the step in flight), text being
  // selected stays put and the next refresh paints the change. Only there: a state change
  // elsewhere (the label after Stop) must never wait on a stray selection.
  if (options && options.keepSelection && selectionInside(host)) return;
  var open = {};
  host.querySelectorAll("details[data-key]").forEach(function (d) { open[d.getAttribute("data-key")] = d.open; });
  host.innerHTML = html;
  host.__html = html;
  host.querySelectorAll("details[data-key]").forEach(function (d) {
    var key = d.getAttribute("data-key");
    if (key in open) d.open = open[key];
  });
}

function paintSessionPage(page) {
  var row = page.row;
  paintHost("session-head", sessionHeadHtml(page));
  paintHost("session-overview-host", sessionOverviewHtml(row));
  paintHost("session-approvals-host", sessionApprovalsHtml(row));
  paintHost("session-result-host", sessionResultHtml(row));
  // Failure evidence leads only when something failed; otherwise it is under the details.
  // A pause is not a failure to investigate: it leads with the pause block, its evidence
  // stays under the collapsed details.
  paintHost("session-failure-host", sessionFailed(row) && !row.pause && row.failure_evidence
    ? '<div class="card">' + failureEvidenceHtml(row.failure_evidence) + "</div>" : "");
  paintHost("session-live-step-host", sessionLiveStepHtml(row), { keepSelection: true });
  paintHost("session-flow-host", flowHtml(row.flow, row.flow_progress, { pause: row.pause, id: row.session_id, result: "session-cause",
    resumed: !!(row.dashboard_operation && row.dashboard_operation.resume_operation_id) }));
  // What each attempt's receipt measured, and the flow total: the trace page's own table.
  paintHost("session-measure-host", row.flow && typeof flowMeasuredHtml === "function"
    ? flowMeasuredHtml(row.flow) : "");
  paintHost("session-usage-host", sessionUsageHtml(row, page.streamable));
  paintHost("session-evidence-body", sessionEvidenceHtml(row));
  loadLiveIdentity(page);
}

function sessionDetailView(id) {
  stopSessionPages();
  reconnectInPlace();
  var page = STATE.sessionPage = { id: id, row: null, stop: { phase: "idle", message: "" }, syncError: null,
                                   refreshing: false, rowPoller: null, tail: null, streamable: false };
  render('<div class="loading">Loading session…</div>');
  return api("/api/sessions/" + encodeURIComponent(id)).then(function (row) {
    if (STATE.sessionPage !== page) return;
    page.row = row;
    // The live tail reads only the native event log. A finished session without one (a
    // flow, an external harness) has nothing to tail, and "0 events · finished" read as
    // if it had done nothing.
    page.streamable = sessionStreamable(row);
    render(
      '<div class="page-head" id="session-head"></div>' +
      '<div id="session-operation-host"></div>' +
      '<div id="session-approvals-host"></div>' +
      '<div id="session-overview-host"></div>' +
      '<div class="session-grid"><div id="session-result-host"></div><div id="session-failure-host"></div>' +
      '<div id="session-live-step-host"></div>' +
      '<div id="session-flow-host"></div><div id="session-measure-host"></div><div id="starter-result-host"></div>' +
      (page.streamable ? '<section class="card" id="session-live"><h2>Live events</h2>' +
        '<div class="live-status"><span id="live-events" class="stat-sub" aria-live="polite">connecting…</span> ' +
        '<button type="button" class="btn btn-small" id="live-retry" hidden>Retry now</button></div></section>' : "") +
      '<div id="session-usage-host"></div>' +
      '<details class="card session-evidence" id="session-evidence"><summary><h2>Details and evidence</h2>' +
      '<span class="stat-sub">state, ownership, artifact references, failure evidence, starter coverage</span></summary>' +
      '<div id="session-evidence-body"></div><div id="starter-evidence-host"></div></details></div>'
    );
    paintSessionPage(page);
    el("session-head").addEventListener("click", function (event) { onSessionHeadClick(page, event); });
    if (page.streamable) {
      page.tail = sessionTail(row.session_id, {
        keep: false,
        onUpdate: function (handle, payload) { onSessionTail(page, handle, payload); }
      });
      el("live-retry").addEventListener("click", function () { if (page.tail) page.tail.retry(); });
    }
    if (isActive(row.state)) startSessionRowPoller(page);
    if (row.starter_origin) loadStarterResult(row.session_id);
    if (routeParams().focus === "resume" && el("session-resume")) el("session-resume").scrollIntoView({ block: "center" });
    // Landed here from a dashboard start (Flows): the operation and its approvals.
    if (routeParams().operation) startSessionOperation(page, routeParams().operation);
  }).catch(function (err) {
    if (isSuperseded(err) || STATE.sessionPage !== page) return;
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    // A damaged record is its own answer (typed 409): say what is wrong, in the server's sentence.
    var unreadable = err.code === "session_unreadable" || err.code === "session_being_written";
    render('<div class="notice err" id="session-unreadable" data-code="' + esc(err.code || "") + '"><div class="notice-title">' +
           (err.code === "session_unreadable" ? "This session's record can't be read"
             : (err.code === "session_being_written" ? "This session is being written; try again in a moment"
               : "Could not load the session")) + "</div>" +
           '<div class="notice-body">' + esc(err.message) + (err.status ? " (" + esc(unreadable ? err.code + ", " : "") + esc(err.status) + ")" : "") +
           '</div><p><a href="#/sessions">All sessions</a></p></div>');
  });
}

function startSessionRowPoller(page) {
  if (page.rowPoller) return;
  var first = true;
  // The row was just read; the poller's first tick would only read it again.
  page.rowPoller = new Poller(function () {
    if (first) { first = false; return; }
    return refreshSessionRow(page);
  }, SESSION_ROW_MS).start();
}

function refreshSessionRow(page) {
  if (STATE.sessionPage !== page || page.refreshing || CONNECTION.down) return;
  page.refreshing = true;
  var wasActive = isActive(page.row.state);
  return api("/api/sessions/" + encodeURIComponent(page.id)).then(function (row) {
    page.refreshing = false;
    if (STATE.sessionPage !== page) return;
    page.row = row;
    page.syncError = null;
    paintSessionPage(page);
    if (!isActive(row.state)) {
      if (page.rowPoller) { page.rowPoller.stop(); page.rowPoller = null; }
      // The starter card summarises a finished run; read it again once it has finished.
      if (wasActive && row.starter_origin) loadStarterResult(row.session_id);
    } else {
      startSessionRowPoller(page);
    }
  }).catch(function (err) {
    page.refreshing = false;
    if (isSuperseded(err) || STATE.sessionPage !== page) return;
    page.syncError = err.message + (err.status ? " (" + err.status + ")" : "");
    paintHost("session-head", sessionHeadHtml(page));
  });
}

function onSessionTail(page, handle, payload) {
  if (STATE.sessionPage !== page) return;
  var panel = el("live-events"), retry = el("live-retry");
  if (panel) panel.textContent = tailStatusText(handle);
  if (retry) retry.hidden = !handle.error;
  if (!payload) return;
  var now = payload.state || {}, shown = page.row.state || {};
  // The tail carries the session's state with every poll: a change, or the end of the log of a
  // session shown as active, means the row is stale. At most one re-read every few seconds.
  var changed = now.work !== shown.work || now.outcome !== shown.outcome || now.process !== shown.process;
  var ended = handle.ended && isActive(shown);
  if ((changed || ended) && (ended || Date.now() - (page.lastTailRefresh || 0) > 3000)) {
    page.lastTailRefresh = Date.now();
    refreshSessionRow(page);
  }
}

function onSessionHeadClick(page, event) {
  var target = event.target.closest("button");
  if (!target || STATE.sessionPage !== page) return;
  if (target.id === "session-stop" && page.stop.phase !== "sending") {
    page.stop = { phase: "confirm", message: "" };
  } else if (target.id === "session-stop-cancel") {
    page.stop = { phase: "idle", message: "" };
  } else if (target.id === "session-stop-confirm" && page.stop.phase === "confirm") {
    guardAction(target, function () { return stopBackgroundSession(page); }, { busy: "stopping…" });
    return;
  } else {
    return;
  }
  paintHost("session-head", sessionHeadHtml(page));
}

/* Stop, confirmed, once: the button is disabled while the request is in flight and the
 * server's answer is shown beside it. */
function stopBackgroundSession(page) {
  page.stop = { phase: "sending", message: "" };
  paintHost("session-head", sessionHeadHtml(page));
  return api("/api/sessions/" + encodeURIComponent(page.id) + "/cancel", { method: "POST", body: {} })
    .then(function (r) {
      if (STATE.sessionPage !== page) return;
      page.stop = { phase: "done", message: r.result || "stop requested" };
      paintHost("session-head", sessionHeadHtml(page));
      return refreshSessionRow(page);
    })
    .catch(function (err) {
      if (isSuperseded(err) || STATE.sessionPage !== page) return;
      page.stop = { phase: "error", message: "Stop failed: " + err.message + (err.code ? " (" + err.code + ")" : "") };
      paintHost("session-head", sessionHeadHtml(page));
    });
}

/* --- the live tail ---------------------------------------------------------------------------
 * Polls /api/runs/<id>/tail with the shared Poller, so nothing is held open between polls: a
 * long-lived stream per tab used up the browser's six connections to this host and stalled
 * every other tab. The cursor is a byte offset into the session's log, so a later poll (or a
 * new handle given `offset`) continues with the next event: nothing repeats, nothing is
 * skipped. The Poller pauses while the tab is hidden. A refused request (401, 403, 404) stops
 * with its reason; anything else retries with exponential backoff up to 30 s and recovers on
 * its own. */

function tailStatusText(handle) {
  var count = handle.count;
  var last = count ? " · last: " + (handle.lastType || "?") : "";
  if (handle.error && handle.error.terminal) return "stopped: " + handle.error.message;
  if (handle.error) {
    return "disconnected (" + handle.error.message + ") · retrying in " + fmt.duration(handle.error.retryMs) +
      (count ? " · " + count + " events so far" : "");
  }
  if (handle.ended) return count ? count + " events" + last + " · finished" : "no events · finished";
  if (!handle.connected) return "connecting…";
  if (!count) return "connected · waiting for events";
  return count + " events" + last + " · live";
}

function tailErrorMessage(err) {
  if (err.status === 401) return "the dashboard token was refused (401); reopen the URL garuda web printed";
  if (err.status === 403) return "the dashboard refused this request (403)";
  if (err.status === 404) return "the session is not in this dashboard's store (404)";
  if (!err.status) return "the dashboard did not answer";
  return err.message + " (" + err.status + ")";
}

function sessionTail(id, options) {
  options = options || {};
  var base = options.intervalMs || SESSION_TAIL_MS;
  // `keep: false` counts events without holding them (the session page needs only the count).
  var keep = options.keep !== false;
  var handle = {
    offset: options.offset || 0, events: [], count: 0, lastType: null, ended: false, stopped: false, connected: false,
    error: null, failures: 0, inFlight: false, poller: null, malformed: 0,
    stop: function () { handle.stopped = true; if (handle.poller) handle.poller.stop(); },
    retry: function () {
      if (handle.stopped || handle.ended) return;
      if (handle.poller) handle.poller.stop();
      start();
    }
  };
  // Assigned before the first tick, which a poll compares against to drop stale answers.
  function start() {
    handle.poller = new Poller(poll, base);
    handle.poller.start();
  }
  function update(payload) { if (options.onUpdate) options.onUpdate(handle, payload); }
  function poll() {
    if (handle.stopped || handle.ended || handle.inFlight || CONNECTION.down) return;
    handle.inFlight = true;
    var poller = handle.poller;
    return api("/api/runs/" + encodeURIComponent(id) + "/tail?offset=" + handle.offset).then(function (payload) {
      handle.inFlight = false;
      if (handle.stopped || poller !== handle.poller) return;
      var before = handle.offset;
      if (payload.truncated) {
        // The log was replaced: start over rather than append to it.
        handle.events = [];
        handle.count = 0;
        handle.lastType = null;
      } else {
        (payload.events || []).forEach(function (event) {
          if (keep) handle.events.push(event);
          handle.count += 1;
          handle.lastType = event.type;
          if (options.onEvent) options.onEvent(event, handle);
        });
      }
      handle.malformed += payload.malformed || 0;
      handle.offset = payload.offset;
      handle.failures = 0;
      handle.error = null;
      handle.connected = true;
      var state = payload.state || {};
      if (payload.eof && (!isActive(state) || payload.crashed)) {
        handle.ended = true;
        poller.stop();
      } else {
        // More is waiting: read it straight away, but only when this poll made progress (a
        // torn final line reports eof false without moving the cursor).
        poller.interval = !payload.eof && handle.offset > before ? 50 : base;
      }
      update(payload);
    }, function (err) {
      handle.inFlight = false;
      if (isSuperseded(err) || handle.stopped || poller !== handle.poller) return;
      handle.connected = false;
      if ([400, 401, 403, 404].indexOf(err.status) >= 0) {
        handle.error = { terminal: true, message: tailErrorMessage(err), status: err.status };
        poller.stop();
      } else {
        handle.failures += 1;
        var delay = Math.min(SESSION_RETRY_MAX_MS, base * Math.pow(2, handle.failures - 1));
        handle.error = { terminal: false, message: tailErrorMessage(err), status: err.status, retryMs: delay };
        poller.interval = delay;
      }
      update(null);
    });
  }
  start();
  return handle;
}

/* Every poller the three pages here own; the router calls this on each navigation. */
function stopSessionPages() {
  if (STATE.sessionList && STATE.sessionList.poller) STATE.sessionList.poller.stop();
  STATE.sessionList = null;
  var page = STATE.sessionPage;
  if (page) {
    if (page.tail) page.tail.stop();
    if (page.rowPoller) page.rowPoller.stop();
    stopSessionOperation(page);
  }
  STATE.sessionPage = null;
  if (STATE.inbox && STATE.inbox.poller) STATE.inbox.poller.stop();
  STATE.inbox = null;
}

function stopSessionStream() { stopSessionPages(); }

// Back from an outage: these pages poll now, in place (their rows are patched, never rebuilt),
// so the router leaves them mounted (reconnectInPlace above).
onReconnect(function () {
  var list = STATE.sessionList;
  if (list && list.poller) list.poller.now();
  var page = STATE.sessionPage;
  if (page && page.rowPoller) page.rowPoller.now();
  if (page && page.tail) page.tail.retry();
  if (page && page.operation && page.operation.poller && !page.operation.done) page.operation.poller.now();
  if (STATE.inbox && STATE.inbox.poller) STATE.inbox.poller.now();
});

/* --- the approval inbox (D.5) ---------------------------------------------
 * Lists what is waiting across sessions and sends the person's answer, bound to the request
 * digest this page was shown, to the session's file channel. It never answers a runtime: the
 * broker validates and decides, so "answer recorded" is not "allowed". The page polls while it
 * is open and adds or removes cards only when the set of requests changes; a card under the
 * pointer is never rebuilt. */

var INBOX_POLL_MS = 5000;

function canAnswer() {
  return !!(STATE.health && STATE.health.mode === "read-write");
}

/* "expires in 12m" or "expired 3m ago", from the request's own expiry (epoch seconds). */
function expiryText(expiresAt) {
  var seconds = expiresAt - Date.now() / 1000;
  return seconds > 0 ? "expires in " + ageText(seconds).replace(/ ago$/, "").replace("just now", "under a minute")
                     : "expired " + ageText(-seconds);
}

function approvalKey(a) {
  return a.source === "dashboard-operation" ? "live\u0000" + a.ask_id + "\u0000" + a.digest
    : a.session_id + "\u0000" + a.approval_id + "\u0000" + a.digest;
}

/* An ask a dashboard-started flow is waiting on, held by this dashboard's broker (GET
 * /api/inbox `live`). Unlike a session-file request it is answered at once (the answer is
 * the decision, bound to the ask's owner and digest), and it is denied as "stopped
 * watching" when no tab has kept its run watched for the grace window. */
function liveApprovalRowHtml(a) {
  var buttons = canAnswer()
    ? '<button type="button" class="btn btn-primary" data-act="allow">Allow</button> ' +
      '<button type="button" class="btn" data-act="deny">Deny</button>'
    : '<span class="stat-sub">read-only dashboard</span>';
  var where = a.session_id ? '#/sessions/' + encodeURIComponent(a.session_id) + "?operation=" + encodeURIComponent(a.operation_id) : "";
  return (
    '<li class="approval-item" data-live="1" data-ask="' + esc(a.ask_id) + '" data-owner="' + esc(a.owner) +
    '" data-digest="' + esc(a.digest) + '">' +
    "<div><strong>" + (where ? '<a href="' + where + '">' + esc(a.name || String(a.session_id).slice(0, 8)) + "</a>"
                              : esc(a.name || "dashboard operation")) + "</strong> " +
    '<span class="chip chip-off">dashboard flow</span> <span class="stat-sub">waiting ' + esc(fmt.duration(a.waiting_ms || 0)) +
    " · answered here at once · denied after " + esc(a.timeout_seconds) + " s unanswered, " + esc(a.grace_seconds) +
    " s after no dashboard tab is watching its run, or at once if the dashboard stops</span></div>" +
    (a.task ? '<div class="stat-sub approval-task">' + esc(fmt.truncate(a.task, 140)) + "</div>" : "") +
    "<pre>" + esc(a.action) + "</pre>" +
    '<div class="approval-actions">' + buttons + ' <span class="stat-sub approval-result" aria-live="polite"></span></div></li>'
  );
}

/* {"<session>\0<approval>": true} for the step-session file requests a live ask covers. */
function coveredApprovalKeys(live) {
  var keys = {};
  (live || []).forEach(function (a) {
    (a.covers || []).forEach(function (c) { keys[c.session_id + "\u0000" + c.approval_id] = true; });
  });
  return keys;
}

function approvalRowHtml(a) {
  if (a.source === "dashboard-operation") return liveApprovalRowHtml(a);
  var buttons = canAnswer() && !a.expired
    ? '<button type="button" class="btn btn-primary" data-act="allow">Approve</button> ' +
      '<button type="button" class="btn" data-act="deny">Deny</button>'
    : '<span class="stat-sub">' + (a.expired ? "expired — it will be denied" : "read-only dashboard") + "</span>";
  return (
    '<li class="approval-item" data-session="' + esc(a.session_id) + '" data-approval="' + esc(a.approval_id) +
    '" data-digest="' + esc(a.digest) + '">' +
    '<div><strong><a href="#/sessions/' + encodeURIComponent(a.session_id) + '">' + esc(a.name || String(a.session_id).slice(0, 8)) + "</a></strong> " +
    '<span class="stat-sub">' + esc(a.family) + " · ceiling " + esc(a.ceiling) +
    (typeof a.expires_at === "number" ? " · " + esc(expiryText(a.expires_at)) : "") + "</span></div>" +
    (a.task ? '<div class="stat-sub approval-task">' + esc(fmt.truncate(firstLine(a.task), 140)) + "</div>" : "") +
    "<pre>" + esc(a.action) + "</pre>" +
    '<div class="approval-actions">' + buttons + ' <span class="stat-sub approval-result" aria-live="polite"></span></div></li>'
  );
}

function inboxView() {
  stopSessionPages();
  reconnectInPlace();
  var inbox = STATE.inbox = { answered: {}, loaded: false, busy: false, poller: null };
  render(
    '<div id="inbox-root"><div class="page-head"><h1>Approvals</h1><span class="meta" id="inbox-count">loading…</span>' +
    '<button type="button" class="btn" id="inbox-refresh">Refresh</button></div>' +
    // How long each kind of request waits (#871), exactly as the brokers enforce it.
    '<p class="stat-sub page-lede" id="inbox-timeouts">A session-file request (a chat, a background flow step) ' +
    "waits until its expiry, shown on its card (300 s; for a background step, <code>approvals.park_timeout_sec</code>, " +
    "300 s by default), then it is denied. A flow started from this dashboard asks here directly: its request is answered at once, " +
    "and denied after its timeout unanswered, about 90 s after no tab is watching its run, or at once if the " +
    "dashboard stops, also when the step runs in its own process.</p>" +
    '<div class="notice err" id="inbox-error" hidden></div><div id="inbox-unreadable"></div>' +
    '<ul class="approvals" id="inbox-list"></ul>' +
    '<div class="empty" id="inbox-empty" hidden><p>Nothing is waiting for you.</p></div>' +
    '<section class="inbox-expired" id="inbox-expired-section" hidden><h2>Expired</h2>' +
    '<p class="stat-sub">These timed out before anyone answered; the broker denies them.</p>' +
    '<ul class="approvals" id="inbox-expired"></ul></section></div>'
  );
  el("inbox-refresh").addEventListener("click", function () { refreshInbox(inbox); });
  el("inbox-root").addEventListener("click", function (event) {
    var button = event.target.closest("button[data-act]");
    if (!button || STATE.inbox !== inbox) return;
    answerInboxApproval(button.closest(".approval-item"), button.getAttribute("data-act") === "allow");
  });
  inbox.poller = new Poller(function () { return refreshInbox(inbox); }, INBOX_POLL_MS).start();
  return Promise.resolve();
}

/* Make `list` hold exactly `items`, in order, reusing the card already there for each one. */
function syncApprovalCards(list, items) {
  var existing = {};
  Array.prototype.forEach.call(list.children, function (li) { existing[li.__key] = li; });
  var previous = null;
  items.forEach(function (a) {
    var key = approvalKey(a);
    var node = existing[key];
    if (!node) {
      var holder = document.createElement("ul");
      holder.innerHTML = approvalRowHtml(a);
      node = holder.firstElementChild;
      node.__key = key;
    }
    delete existing[key];
    var slot = previous ? previous.nextElementSibling : list.firstElementChild;
    if (slot !== node) list.insertBefore(node, slot);
    previous = node;
  });
  // A card whose answer is still in flight stays until the answer settles.
  Object.keys(existing).forEach(function (key) {
    if (!existing[key].hasAttribute("data-busy")) existing[key].remove();
  });
}

function refreshInbox(inbox) {
  if (STATE.inbox !== inbox || inbox.busy || CONNECTION.down) return;
  inbox.busy = true;
  return api("/api/inbox").then(function (payload) {
    inbox.busy = false;
    if (STATE.inbox !== inbox) return;
    // This dashboard's own asks first (they expire with the heartbeat), then the session files'.
    var live = (payload.live || []).map(function (a) { return Object.assign({ source: "dashboard-operation" }, a); });
    // Listing an ask here opens its run in this tab: the app-level watcher keeps it alive.
    live.forEach(function (a) { if (typeof watchOperation === "function") watchOperation(a.owner); });
    // A step's own file request for an ask listed live is the same approval: shown once.
    var covered = coveredApprovalKeys(live);
    var files = (payload.approvals || []).filter(function (a) { return !covered[a.session_id + "\u0000" + a.approval_id]; });
    var all = live.concat(files).filter(function (a) { return !inbox.answered[approvalKey(a)]; });
    inbox.items = all;
    inbox.loaded = true;
    paintHost("inbox-unreadable", unreadableNote(payload));
    inbox.error = null;
    paintInbox(inbox);
  }).catch(function (err) {
    inbox.busy = false;
    if (isSuperseded(err) || STATE.inbox !== inbox) return;
    if (err.status === 401) { stopSessionPages(); render(tokenRequiredPanel()); return; }
    var box = el("inbox-error");
    if (box) {
      box.hidden = false;
      box.innerHTML = '<div class="notice-title">' + (inbox.loaded ? "Could not refresh approvals" : "Could not load approvals") +
        '</div><div class="notice-body">' + esc(err.message) + (err.code ? " (" + esc(err.code) + ")" : "") +
        (inbox.loaded ? " — showing the last list; it retries on its own." : "") + "</div>";
    }
    if (!inbox.loaded) { var count = el("inbox-count"); if (count) count.textContent = "unknown"; }
  });
}

function paintInbox(inbox) {
  var list = el("inbox-list"), expiredList = el("inbox-expired");
  if (!list || !expiredList) return;
  // An answer this page sent is final here even before the next poll drops the request: a
  // repaint (the answered card's own removal) must not bring it back as a fresh, live card.
  var open = inbox.items.filter(function (a) { return !inbox.answered[approvalKey(a)]; });
  var live = open.filter(function (a) { return !a.expired; });
  var expired = open.filter(function (a) { return a.expired; });
  syncApprovalCards(list, live);
  syncApprovalCards(expiredList, expired);
  el("inbox-error").hidden = true;
  el("inbox-expired-section").hidden = expired.length === 0;
  el("inbox-empty").hidden = live.length > 0 || list.children.length > 0;
  paintInboxCount(inbox);
}

/* Counts what can still be answered, as the nav badge does; expired requests are apart. */
function paintInboxCount(inbox) {
  var count = el("inbox-count");
  if (!count || !inbox.items) return;
  var live = inbox.items.filter(function (a) { return !a.expired && !inbox.answered[approvalKey(a)]; }).length;
  var expired = inbox.items.filter(function (a) { return a.expired; }).length;
  count.textContent = live + " waiting" + (expired ? " · " + expired + " expired" : "");
}

function answerInboxApproval(item, allow) {
  var inbox = STATE.inbox;
  if (!item || item.hasAttribute("data-busy") || item.getAttribute("data-state")) return;
  var result = item.querySelector(".approval-result");
  var buttons = item.querySelectorAll("button");
  item.setAttribute("data-busy", "1");
  buttons.forEach(function (b) { b.disabled = true; });
  result.textContent = "sending…";
  if (item.hasAttribute("data-live")) return answerLiveApproval(inbox, item, allow, result, buttons);
  return api("/api/sessions/" + encodeURIComponent(item.getAttribute("data-session")) +
             "/approvals/" + encodeURIComponent(item.getAttribute("data-approval")), {
    method: "POST", body: { allow: allow, digest: item.getAttribute("data-digest") }
  }).then(function () {
    item.removeAttribute("data-busy");
    item.setAttribute("data-state", "recorded");
    result.textContent = "answer recorded — the broker decides";
    if (typeof refreshInboxBadge === "function") refreshInboxBadge();
    if (!inbox || STATE.inbox !== inbox) return;
    // The request stays pending on disk until the broker decides; this page has answered it.
    inbox.answered[item.__key] = true;
    paintInboxCount(inbox);
    toast("Answer recorded — the broker decides");
    setTimeout(function () {
      if (STATE.inbox !== inbox) return;
      item.remove();
      if (inbox.items) paintInbox(inbox);
    }, 1500);
  }).catch(function (err) {
    item.removeAttribute("data-busy");
    if (isSuperseded(err)) return;
    // Stale page, an earlier answer, an expired request: say so in the server's words. A
    // refused answer cannot succeed on a retry; anything else (the dashboard unreachable) can.
    result.textContent = err.message + " (" + (err.code || "error") + (err.status ? ", " + err.status : "") + ")";
    item.setAttribute("data-state", err.code || "error");
    if (err.status !== 409 && err.status !== 410) {
      item.removeAttribute("data-state");
      buttons.forEach(function (b) { b.disabled = false; });
    }
  });
}

/* A dashboard ask is answered through the broker, bound to its owner and digest; the answer
 * is the decision. The same in-flight guard and error display as a session-file answer. */
function answerLiveApproval(inbox, item, allow, result, buttons) {
  return api("/api/approvals/" + encodeURIComponent(item.getAttribute("data-ask")), {
    method: "POST", body: { approved: allow, owner: item.getAttribute("data-owner"), digest: item.getAttribute("data-digest") }
  }).then(function () {
    item.removeAttribute("data-busy");
    item.setAttribute("data-state", "recorded");
    result.textContent = allow ? "allowed" : "denied";
    if (typeof refreshInboxBadge === "function") refreshInboxBadge();
    if (!inbox || STATE.inbox !== inbox) return;
    inbox.answered[item.__key] = true;
    paintInboxCount(inbox);
    toast(allow ? "Allowed: the flow continues" : "Denied: the step sees the denial");
    setTimeout(function () {
      if (STATE.inbox !== inbox) return;
      item.remove();
      if (inbox.items) paintInbox(inbox);
    }, 1500);
  }).catch(function (err) {
    item.removeAttribute("data-busy");
    if (isSuperseded(err)) return;
    result.textContent = err.message + " (" + (err.code || "error") + (err.status ? ", " + err.status : "") + ")";
    item.setAttribute("data-state", err.code || "error");
    // Settled already, or not this ask: a retry cannot succeed. Anything else can.
    if (err.status !== 409 && err.status !== 404) {
      item.removeAttribute("data-state");
      buttons.forEach(function (b) { b.disabled = false; });
    }
  });
}

/* --- the conversation panel (F.1) -------------------------------------------------------
 * Models used, grouped by work type, harness and model; the selected model kept apart from
 * what was reported; sessions this one tagged, was tagged by, resumed from. Every string is
 * escaped. "not reported" is shown as such and never replaced by the selected model. */

var CONSULT_PILL = { answered: "success", refused: "failed", failed: "failed", withheld: "failed",
  timeout: "failed", quarantined: "failed", interrupted: "unknown", in_progress: "running", unknown: "unknown" };

function consultChanges(ch) {
  if (!ch || ch.unchanged === null || ch.unchanged === undefined) return "unknown";
  return ch.unchanged ? "none observed" : String(ch.changed === null ? "?" : ch.changed) + " changed";
}

/* Nested lanes: each consult is a child of the asking session, with its admission, outcome,
 * duration, denied operations and observed changes. Identities and counts only; the question
 * and the answer are never stored, so never shown. */
function consultsHtml(c) {
  var list = c.consults || { entries: [], summary: { count: 0, by_status: {} } };
  if (!list.entries.length) return "";
  var lanes = list.entries.map(function (e) {
    var who = e.identity ? (e.identity.runtime + (e.identity.model_id ? " · " + e.identity.model_id : "")) : "identity unknown";
    return '<li class="consult-lane" data-request="' + esc(e.request_id) + '" data-status="' + esc(e.status) + '">' +
      '<span class="pill ' + (CONSULT_PILL[e.status] || "unknown") + '">' + esc(e.status.replace("_", " ")) + "</span> " +
      "<strong>" + esc(e.asker_role || "asker") + "</strong> asked <strong>" + esc(e.target || "?") + "</strong> (" + esc(who) + ")" +
      ' · admission ' + esc(e.admission) +
      " · " + (e.elapsed_ms === null || e.elapsed_ms === undefined ? "duration unknown" : esc((e.elapsed_ms / 1000).toFixed(1)) + " s") +
      " · denied " + (e.denied_operations === null || e.denied_operations === undefined ? "unknown" : esc(e.denied_operations)) +
      " · changes " + esc(consultChanges(e.changes)) +
      (e.code ? ' · <code>' + esc(e.code) + "</code>" : "") +
      (e.evidence === "state only" ? ' <span class="pill unknown">no receipt</span>' : "") +
      (e.child_session && e.identity ? ' · <a href="#/sessions/' + encodeURIComponent(e.child_session) + '">session</a>' : "") + "</li>";
  }).join("");
  var parts = Object.keys(list.summary.by_status).map(function (k) { return list.summary.by_status[k] + " " + k.replace("_", " "); }).join(", ");
  return '<div class="card" id="consults-panel"><h2>Consults</h2><p class="stat-sub" id="consults-summary">' +
    esc(list.summary.count) + " (" + esc(parts) + "). Advice from another model; it is not verification.</p>" +
    '<ul class="consult-lanes">' + lanes + "</ul></div>";
}

/* Which definition ran and the system prompt it actually sent: digests and lengths only. */
function agentPromptsHtml(prompts) {
  return (prompts || []).map(function (p) {
    return '<code class="prompt-digest" data-digest="' + esc(p.digest) + '">' + esc(String(p.digest).slice(0, 12)) + "</code>" +
      (p.chars !== null && p.chars !== undefined ? " (" + esc(fmt.tokens(p.chars)) + " chars)" : "");
  }).join(", ");
}

function agentLineHtml(a) {
  if (!a || (!a.name && !a.digest && !(a.prompts || []).length && !(a.segments || []).length && !(a.tenures || []).length)) return "";
  var segments = (a.segments || []).map(function (s) {
    var external = s.kind === "acp_execution";
    var tenure = s.runtime_tenure;
    return '<li class="agent-execution' + (external ? ' agent-acp-execution' : '') + '" data-segment="' + esc(s.id) + '" data-agent="' + esc(s.name) + '">' +
      '<strong>' + esc(s.name) + '</strong> · ' + (external ? esc(s.runtime) : 'native run') + ' · ' +
      (s.digest ? 'definition <code>' + esc(String(s.digest).slice(0, 12)) + '</code>' : 'definition unknown') +
      (tenure ? ' · runtime tenure ' + esc(tenure.index + 1) : ' · runtime tenure unknown') +
      ' · ' + (external ? 'ACP request (attempted) ' : 'system prompt ') + agentPromptsHtml(s.prompts) +
      (external ? ' · ' + esc(s.prompt_changes) + ' request attempts · internal system prompt unknown' :
        (s.prompt_changes > 1 ? ' · changed ' + esc(s.prompt_changes - 1) + '×' : '')) + '</li>';
  }).join("");
  var tenures = (a.tenures || []).map(function (t) {
    return '<li class="agent-tenure" data-tenure="' + esc(t.index) + '">Runtime tenure ' + esc(t.index + 1) +
      ' · ' + esc(t.runtime_id) + ' (' + esc(t.kind) + ') · ' + esc(t.execution_count) + ' recorded executions' +
      ' · agent definition ' + esc(t.agent_status) + ' · prompt ' + esc(t.prompt_status) +
      (t.omitted_executions ? ' · ' + esc(t.omitted_executions) + ' additional executions omitted' : '') + '</li>';
  }).join("");
  var unknown = a.unattributed || [];
  if (unknown.length) {
    segments += '<li class="agent-unattributed">Unattributed historical prompts · ' + agentPromptsHtml(unknown) + '</li>';
  }
  return '<div id="agent-line"><p class="stat-sub">Agent: <strong>' + esc(a.name || "—") + "</strong>" +
    (a.digest ? ' · definition <code id="agent-digest">' + esc(String(a.digest).slice(0, 12)) + "</code>" : "") +
    (a.segment && a.segment.previous_digest ? ' · <span class="pill unknown">definition changed on resume</span>' : "") + '</p>' +
    (tenures ? '<ul class="stat-sub" id="agent-tenures">' + tenures + '</ul>' : '') +
    (segments ? '<ul class="stat-sub">' + segments + '</ul>' : '<p class="stat-sub">No system prompt recorded</p>') + '</div>';
}

function conversationPanelHtml(c) {
  var used = c.models_used;
  var rows = used.rows.map(function (r) {
    return '<tr class="model-row" data-work="' + esc(r.work_type) + '" data-model="' + esc(r.model) + '" data-origin="' + esc(r.origin || "run") + '">' +
      '<td data-th="Work">' + esc(r.work_type) + (r.origin && r.origin !== "run" ? ' <span class="pill unknown origin-pill">' + esc(r.origin) + "</span>" : "") + '</td><td data-th="Harness">' + esc(r.harness) + "</td>" +
      '<td data-th="Model">' + (r.model === "not reported" ? '<span class="pill unknown">not reported</span>' : esc(r.model)) + "</td>" +
      '<td class="num" data-th="Calls">' + esc(r.calls) + '</td><td class="num" data-th="ACP turns">' + esc(r.turns) + "</td>" +
      '<td class="num" data-th="Tokens">' + esc(fmt.tokens(r.total_tokens)) + "</td>" +
      '<td class="num" data-th="Cost">' + (r.cost_unknown ? "unknown" : esc(fmt.cost(r.cost_usd))) +
      (r.cost_unknown && r.cost_usd ? " + " + esc(fmt.cost(r.cost_usd)) + " known" : "") + "</td></tr>";
  }).join("");
  function links(title, list, id) {
    if (!list.length) return "";
    return '<div id="' + id + '"><strong>' + title + "</strong> " + list.map(function (s) {
      return '<a href="#/sessions/' + encodeURIComponent(s.session_id) + '">' +
             esc(s.name || s.session_id.slice(0, 8)) + "</a>" +
             (s.cross_project ? ' <span class="pill unknown">other project</span>' +
               (s.receipt && s.receipt.present ? ' <span class="pill success receipt" data-fingerprint="' + esc(s.receipt.fingerprint) + '">receipt ' + esc(String(s.receipt.fingerprint || "").slice(0, 8)) + "</span>"
                                               : ' <span class="pill failed">no receipt</span>') : "");
    }).join(", ") + "</div>";
  }
  return (
    '<div class="card" id="conversation-panel"><h2>Models used</h2>' +
    agentLineHtml(c.agent) +
    (used.label ? '<p class="stat-sub" id="models-label">' + esc(used.label) + "</p>" : "") +
    '<p class="stat-sub">Selected: ' + esc((c.selected.harness || c.session.runtime) + " · " + (c.selected.model_id || "—")) +
    ". Reported attribution is shown separately; ACP turns are not call counts.</p>" +
    (rows ? '<div class="table-wrap"><table class="table table-stack" id="models-table"><thead><tr><th>Work</th><th>Harness</th><th>Model</th>' +
            "<th class=\"num\">Calls</th><th class=\"num\">ACP turns</th><th class=\"num\">Tokens</th><th class=\"num\">Cost</th></tr></thead><tbody>" + rows + "</tbody></table></div>"
          : '<p class="stat-sub">No model calls recorded.</p>') +
    (used.snapshots.length ? '<p class="stat-sub" id="snapshots-note">Context snapshots (occupancy, not billable usage): ' +
      used.snapshots.map(function (s) {
        return esc((s.context_used === null ? "?" : s.context_used) + "/" + (s.context_size === null ? "?" : s.context_size) +
          (s.model ? " · reported " + s.model : "") +
          (typeof s.cost_usd === "number" ? " · harness-estimated $" + s.cost_usd.toFixed(4) : ""));
      }).join(", ") + "</p>" : "") +
    (c.selected.fallback && c.selected.fallback.taken && c.selected.fallback.taken.index ?
      '<p class="stat-sub" id="fallback-note">Started on ' + esc(c.selected.fallback.taken.harness) + " after " +
      esc((c.selected.fallback.skipped || []).map(function (s) { return s.harness + " (" + s.reason + ")"; }).join(", ")) + ".</p>" : "") +
    (c.lanes.length ? '<p class="stat-sub" id="lanes-note">Lanes: ' + c.lanes.map(function (l) { return esc(l.runtime_id + " (" + l.kind + ")"); }).join(" → ") + "</p>" : "") +
    links("Resumed from", c.links.resumed_from, "link-resumed-from") +
    links("Continued by", c.links.resumed_into, "link-resumed-into") +
    links("Tagged", c.links.tagged, "link-tagged") +
    links("Tagged by", c.links.tagged_by, "link-tagged-by") +
    "</div>" + consultsHtml(c)
  );
}

function loadConversationPanel(sessionId) {
  var host = el("conversation-host");
  if (!host) return;
  api("/api/sessions/" + encodeURIComponent(sessionId) + "/conversation").then(function (c) {
    if (el("conversation-host") === host) host.innerHTML = conversationPanelHtml(c);
  }).catch(function (err) { if (!isSuperseded(err) && el("conversation-host") === host) host.innerHTML = ""; });
}

/* --- setup (F.4): read-only ---------------------------------------------------------------
 * What is configured and what is wrong. Fix commands are shown as text with a Copy button;
 * nothing here edits a file or starts anything. */

function copyButtonHtml(text) {
  return '<button type="button" class="btn copy-fix" data-copy="' + esc(text) + '">Copy</button>';
}

/* Agents (H.11): read-only. Source, declared settings with where each came from, the definition
 * digest, the static prompt digest and each prompt section's size. No instruction or prompt text. */
function setupAgentsHtml(agents) {
  if (!agents) return "";
  var rows = agents.map(function (a) {
    if (a.error) {
      return '<tr class="agent-row" data-agent="' + esc(a.qualified || a.name) + '"><td>' + esc(a.qualified || a.name) +
        '</td><td colspan="5"><span class="pill failed">cannot resolve</span> ' +
        '<code class="agent-diagnostic">' + esc(a.error_code || "agent.invalid_value") + '</code> ' + esc(a.error) +
        ((a.unsupported || []).length ? '<ul class="agent-unsupported">' + a.unsupported.map(function (f) {
          return '<li><code>' + esc(f.path) + '</code> <span class="stat-sub">(' + esc(f.source) + ')</span></li>';
        }).join("") + '</ul>' : '') + "</td></tr>";
    }
    var sections = (a.sections || []).map(function (s) {
      return '<span class="agent-section">' + esc(s.section) + " " + esc(fmt.tokens(s.bytes)) + " bytes / " +
        esc(fmt.tokens(s.chars)) + " chars / " + esc(fmt.tokens(s.tokens)) + " estimated tokens</span>";
    }).join("");
    var fields = (a.fields || []).map(function (f) {
      return "<li><code>" + esc(f.path) + "</code> = " + esc(f.value) + ' <span class="stat-sub">(' + esc(f.source) + ")</span></li>";
    }).join("");
    return '<tr class="agent-row" data-agent="' + esc(a.qualified) + '"><td>' + esc(a.qualified) +
      (a.shadowed_by ? ' <span class="pill unknown">shadowed by ' + esc(a.shadowed_by) + "</span>" : "") +
      "</td><td>" + esc(a.source) + (a.extends ? " · extends " + esc(a.extends) : "") + '</td><td><code class="agent-digest">' +
      esc(String(a.digest).slice(0, 12)) + '</code></td><td><code class="agent-prompt-digest">' + esc(String(a.prompt_digest).slice(0, 12)) +
      '</code></td><td class="num agent-size">' + esc(fmt.tokens(a.tokens)) + ' tokens<span class="stat-sub agent-sections">' + sections + "</span></td>" +
      "<td><details><summary>" + esc((a.fields || []).length) + " declared</summary><ul class=\"agent-fields\">" + fields + "</ul></details>" +
      (a.warnings && a.warnings.length ? '<div class="stat-sub">' + a.warnings.map(esc).join("; ") + "</div>" : "") + "</td></tr>";
  }).join("");
  return '<div class="card" id="agents-setup"><h2>Agents</h2><p class="stat-sub">Read-only. Digests identify exactly what a run would use; ' +
    "token sizes are estimated (chars/4). Instruction and prompt text is not shown here — use <code>garuda agent show NAME</code>.</p>" +
    '<div class="table-wrap"><table class="table table-stack" id="agents-table"><thead><tr><th>Agent</th><th>Source</th><th>Definition</th><th>Prompt</th>' +
    "<th class=\"num\">Size</th><th>Declared</th></tr></thead><tbody>" + rows + "</tbody></table></div></div>";
}

function setupConsultsHtml(k) {
  if (!k) return "";
  var grants = k.grants.map(function (g) {
    return '<tr class="consult-grant" data-asker="' + esc(g.asker) + '"><td>' + esc(g.asker) + "</td><td>" + esc(g.targets.join(", ")) +
      "</td><td>" + esc(g.transport === "native" ? "native (always available)" : "external harness: only on a proved adapter") + "</td></tr>";
  }).join("");
  var limits = Object.keys(k.limits).map(function (n) { return esc(n) + " " + esc(k.limits[n]); }).join(" · ");
  var adapters = k.adapters.map(function (a) {
    return '<tr class="consult-adapter" data-package="' + esc(a.package) + '" data-exposed="' + esc(a.exposed) + '"><td>' + esc(a.package + " " + a.version) +
      "</td><td>" + esc(a.forwarding) + "</td><td>" + esc(a.permission_provenance) + "</td><td>" + esc(a.quiescence) + "</td><td>" +
      (a.exposed ? '<span class="pill success">offered</span>' : '<span class="pill unknown">not offered</span> ' + esc(a.reason)) + "</td></tr>";
  }).join("");
  return '<div class="card" id="consults-setup"><h2>Consults</h2><p class="stat-sub">' + esc(k.note) + "</p>" +
    (grants ? '<div class="table-wrap"><table class="table table-stack" id="consult-grants"><thead><tr><th>Asker</th><th>May ask</th><th>Tool</th></tr></thead><tbody>' + grants + "</tbody></table></div>"
            : '<p class="stat-sub">No role is allowed to consult another. Grants live in your user <code>garuda.yaml</code> (<code>roles.&lt;name&gt;.consult</code>).</p>') +
    '<p class="stat-sub" id="consult-limits">Ceilings: ' + limits + "</p>" +
    '<div class="table-wrap"><table class="table table-stack" id="consult-adapters"><thead><tr><th>Adapter</th><th>Forwards MCP</th><th>Permission identity</th><th>Pauses workspace</th><th>Tool</th></tr></thead><tbody>' + adapters + "</tbody></table></div></div>";
}

var DIAG_LEVELS = ["error", "warning", "info"];

function diagnosticHtml(d) {
  return '<li class="diag" data-code="' + esc(d.code) + '" data-level="' + esc(d.level) + '">' +
    '<span class="pill ' + (d.level === "error" ? "failed" : (d.level === "warning" ? "running" : "unknown")) + '">' + esc(d.level) + "</span> " +
    "<strong>" + esc(d.code) + "</strong> " + esc(d.message) +
    (d.code && d.code.indexOf("harness.") === 0 && "checked_at" in d ? " <span class=\"stat-sub login-age\">(" + esc(loginAge(d)) + ")</span>" : "") +
    (d.fix ? '<div class="stat-sub">Fix: <code>' + esc(d.fix) + "</code> " + copyButtonHtml(d.fix) + "</div>" : "") + "</li>";
}

/* Worst level first, and a code that repeats (one stale lease per crashed run, say) folds into
 * one expandable group, so a single error is not buried under a hundred identical warnings.
 * Every diagnostic is still in the DOM, with its own fix and Copy button. */
function setupDiagnosticsHtml(list) {
  var rank = function (level) { var i = DIAG_LEVELS.indexOf(level); return i < 0 ? DIAG_LEVELS.length : i; };
  var groups = [];
  var byKey = {};
  list.forEach(function (d) {
    var key = d.level + "\u0000" + d.code;
    if (!byKey[key]) { byKey[key] = { level: d.level, code: d.code, items: [] }; groups.push(byKey[key]); }
    byKey[key].items.push(d);
  });
  groups.sort(function (a, b) { return rank(a.level) - rank(b.level); });
  var counts = {};
  list.forEach(function (d) { counts[d.level] = (counts[d.level] || 0) + 1; });
  var summary = list.length
    ? '<div class="diag-summary" id="diag-summary">' + Object.keys(counts).sort(function (a, b) { return rank(a) - rank(b); }).map(function (level) {
        return '<span class="chip ' + (level === "error" ? "chip-bad" : "chip-off") + '">' + esc(counts[level]) + " " + esc(level) + "</span>";
      }).join("") + "</div>"
    : '<p class="stat-sub">Nothing to report.</p>';
  var items = groups.map(function (g) {
    if (g.items.length <= 3) return g.items.map(diagnosticHtml).join("");
    return '<li class="diag-group" data-code="' + esc(g.code) + '"><details' + (g.level === "error" ? " open" : "") + "><summary>" +
      '<span class="pill ' + (g.level === "error" ? "failed" : (g.level === "warning" ? "running" : "unknown")) + '">' + esc(g.level) + "</span> " +
      "<strong>" + esc(g.code) + "</strong> × " + g.items.length + ' <span class="stat-sub">' + esc(fmt.truncate(g.items[0].message, 90)) + "</span>" +
      '</summary><ul class="diag-group-body">' + g.items.map(diagnosticHtml).join("") + "</ul></details></li>";
  }).join("");
  return summary + '<ul class="diagnostics" id="setup-diagnostics">' + items + "</ul>";
}

/* Every harness's login reading, the same record and age the Runtimes and Providers pages show. */
function setupLoginsHtml(logins) {
  if (!logins || !logins.length) return "";
  return '<p class="stat-sub" id="setup-logins">Logins: ' + logins.map(function (l) {
    return '<span class="setup-login" data-runtime="' + esc(l.runtime_id) + '">' + esc(l.runtime_id) + " " + loginPill(l) + "</span>";
  }).join(" · ") + "</p>";
}

function setupView() {
  render('<div class="loading">Loading setup…</div>');
  return api("/api/setup").then(function (s) {
    var diagnostics = setupDiagnosticsHtml(s.diagnostics);
    var roles = s.roles.map(function (r) {
      return '<tr class="role-row" data-role="' + esc(r.role) + '"><td>' + esc(r.role) + "</td><td>" + esc(r.harness) + "</td><td>" +
        esc(r.model_id || "—") + "</td><td>" + esc(r.effort || "—") + "</td><td>" +
        esc((r.permissions || "smart") + " / " + (r.write_policy || "edits")) + "</td><td>" +
        esc(r.harness_mode || "by permissions") + "</td><td>" + esc(r.agent || "—") + "</td><td>" +
        (r.fallback.length ? r.fallback.map(function (f) { return esc(f.harness + (f.model_id ? "/" + f.model_id : "")); }).join(" → ") : "—") +
        "</td><td>" + esc(r.source || "") + "</td></tr>";
    }).join("");
    var flows = s.flows.map(function (f) {
      return '<tr class="flow-row-setup" data-flow="' + esc(f.name) + '"><td>' + esc(f.name) + "</td><td>" +
        (f.example ? '<span class="pill unknown">example</span> ' : "") + esc(f.source) + "</td><td>" + esc(f.steps) + "</td><td>" +
        esc(f.roles.join(", ")) + "</td><td>" +
        (f.missing_roles.length ? '<span class="pill failed">missing: ' + esc(f.missing_roles.join(", ")) + "</span>" : "—") + "</td></tr>";
    }).join("");
    render(
      '<div id="setup-root"><div class="page-head"><h1>Setup</h1><span class="meta">read-only</span></div>' +
      '<p class="stat-sub">Files: ' + esc(s.files.user) + " (yours), " + esc(s.files.project) + " (this project).</p>" +
      '<div class="card"><h2>Diagnostics</h2>' + diagnostics + setupLoginsHtml(s.logins) + "</div>" +
      '<div class="card"><h2>Roles</h2>' + (roles ? '<div class="table-wrap"><table class="table table-stack" id="roles-table"><thead><tr><th>Role</th><th>Harness</th><th>Model</th><th>Effort</th><th>Permissions / writes</th><th>Harness mode</th><th>Agent</th><th>Fallback chain</th><th>From</th></tr></thead><tbody>' + roles + "</tbody></table></div>"
        : '<p class="stat-sub">No roles. <code>garuda init</code> proposes some.</p>') + "</div>" +
      setupAgentsHtml(s.agents) + setupConsultsHtml(s.consults) +
      '<div class="card"><h2>Flows</h2><div class="table-wrap"><table class="table table-stack" id="flows-table"><thead><tr><th>Flow</th><th>From</th><th>Steps</th><th>Roles</th><th>Missing</th></tr></thead><tbody>' + flows + "</tbody></table></div></div>" +
      '<div class="card"><h2>Where each value came from</h2>' + (s.withheld.length ? '<p class="stat-sub" id="withheld">Withheld until trusted: ' + esc(s.withheld.join(", ")) + "</p>" : "") +
      '<table class="table" id="provenance-table"><tbody>' + s.provenance.map(function (p) {
        return "<tr><td>" + esc(p.key) + "</td><td>" + esc(p.source) + "</td></tr>";
      }).join("") + "</tbody></table></div></div>"
    );
    el("setup-root").addEventListener("click", function (event) {
      var button = event.target.closest("button.copy-fix");
      if (!button) return;
      var text = button.getAttribute("data-copy");
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(function () { toast("Copied"); },
        function () { toast("Copy is not available here; select the text"); });
    });
  }).catch(function (err) {
    if (isSuperseded(err)) return;
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    render('<div class="notice err"><div class="notice-title">Could not load setup</div><div class="notice-body">' + esc(err.message) + "</div></div>");
  });
}
