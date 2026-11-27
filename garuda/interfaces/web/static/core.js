/* Shared helpers. Loaded first; everything else assumes these globals.
 *
 * No modules, no build step, one global scope — the same posture as the rest of this
 * package's zero-dependency approach. */

"use strict";

/* --- token bootstrap ------------------------------------------------------
 * The token and the route both live in the hash, so the token occupies it for
 * exactly one tick before being swapped for the real route — at load here, and on every
 * later hash change in router.js: pasting the printed `#t=…` URL into an open tab is a
 * same-document hash change, not a page load. location.replace() keeps the token out of
 * the address bar and out of history.
 *
 * sessionStorage, not localStorage: per-tab, and it does not persist for whatever
 * else binds this port next week. A tab opened from history therefore has no token
 * and is told to relaunch, which is the fail-closed outcome. */

var TOKEN = "";
try { TOKEN = sessionStorage.getItem("garuda_token") || ""; } catch (e) { TOKEN = ""; }

function setToken(token) {
  TOKEN = token;
  try { sessionStorage.setItem("garuda_token", token); } catch (e) { /* this tab only */ }
}

/* The token in a `#t=…` hash, or null when the hash carries none. */
function hashToken(hash) {
  if ((hash || "").indexOf("#t=") !== 0) return null;
  var token = hash.slice(3);
  try { token = decodeURIComponent(token); } catch (e) { /* keep it as typed */ }
  return token;
}

/* Where to go once a token arrives: the page that lost its token, else the overview. */
function takeReturnRoute() {
  var route = "";
  try {
    route = sessionStorage.getItem("garuda_return") || "";
    sessionStorage.removeItem("garuda_return");
  } catch (e) { route = ""; }
  return route.indexOf("#/") === 0 ? route : "#/";
}

if (hashToken(location.hash) !== null) {
  setToken(hashToken(location.hash));
  location.replace(takeReturnRoute());
}

var STATE = {
  health: null, runs: [], run: null, config: null, poller: null, filters: {},
  // The live tail\'s state object, checked by identity so a late poll can tell it belongs to
  // a view that has since been replaced.
  live: null, chat: null, raw: null, openTurns: null
};

/* --- navigation generation ------------------------------------------------
 * Every navigation (router.js navigate()) starts a new generation. A GET the previous view
 * still had in flight — answered after the view that asked is gone — rejects with a
 * `Superseded` error (code "superseded"). It is not aborted: an aborted request is a failed
 * request in every log and check that watches them, for no gain at this scale. Every promise
 * settles: a Poller's call returns and its next tick runs, a chain's cleanup runs. What must
 * not happen is the old view painting over the new one, so every error path starts with
 * `if (isSuperseded(err)) return;` and success paths never see a superseded read. Writes
 * (any non-GET) settle normally.
 *
 * {keep: true} keeps a GET out of this (the nav badge, boot, a chat's heartbeat): an
 * optimisation for reads that truly outlive their view, not something correctness needs.
 *
 * viewGuard() returns a function that stays true while the view that called it is the one
 * on screen; check it before touching the DOM after anything asynchronous that is not a
 * GET through api() (a timer, a write's response, a chained re-render). */

var NAVIGATION = { generation: 0 };

function beginNavigation() {
  NAVIGATION.generation += 1;
}

function viewGuard() {
  var generation = NAVIGATION.generation;
  return function () { return generation === NAVIGATION.generation; };
}

function supersededError() {
  var err = new Error("Superseded by a navigation");
  err.name = "Superseded";
  err.code = "superseded";
  err.status = 0;
  return err;
}

/* True for a read the user navigated away from: ignore it, silently. */
function isSuperseded(err) {
  return !!err && err.code === "superseded";
}

// A superseded read nobody handled is not an error worth a console line. (Guarded: the
// static tests run this file in a bare VM context.)
if (typeof window !== "undefined" && window.addEventListener) {
  window.addEventListener("unhandledrejection", function (event) {
    if (isSuperseded(event.reason)) event.preventDefault();
  });
}

/* --- fetch ---------------------------------------------------------------- */

function api(path, opts) {
  opts = opts || {};
  var method = opts.method || "GET";
  var tied = method === "GET" && !opts.keep;
  var current = tied ? viewGuard() : function () { return true; };
  // While the dashboard is unreachable, one backed-off probe asks for everyone: a read fails
  // here without a request, so N pollers do not become N retry loops against a dead server.
  // A view's own backoff still runs on these failures; onReconnect() is the all-clear.
  // Writes still go out (a user's click is worth one real attempt, and its success ends
  // the outage early).
  if (CONNECTION.down && method === "GET") return Promise.reject(connectionDownError());
  var headers = { "X-Garuda-Token": TOKEN };
  if (opts.body) headers["Content-Type"] = "application/json";
  return fetch(path, {
    method: method,
    headers: headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  }).then(function (response) {
    if (!current()) throw supersededError();
    return response.text().then(function (text) {
      if (!current()) throw supersededError();
      var payload = null;
      try { payload = text ? JSON.parse(text) : null; } catch (e) { payload = null; }
      if (response.ok) {
        if (CONNECTION.down) connectionRestored();
        return payload;
      }
      var err = new Error((payload && payload.error && payload.error.message) || response.statusText);
      err.code = (payload && payload.error && payload.error.code) || "unavailable";
      err.status = response.status;
      if (isConnectionLimit(response, payload)) {
        err.code = "too_many_connections";
        err.retryAfter = retryAfterSeconds(response);
        err.message = TOO_MANY_TABS;
        connectionLost(err);
      } else {
        if (CONNECTION.down) connectionRestored();
        if (response.status === 401) authLost();
      }
      throw err;
    }, function () {
      throw current() ? unreachableError() : supersededError();
    });
  }, function () {
    // Failed after a navigation: the view that asked is gone.
    if (!current()) throw supersededError();
    // fetch() rejects only when no response arrived: the server is down or unreachable. The
    // browser's own words ("Failed to fetch", "Load failed") say nothing a user can act on.
    throw unreachableError();
  });
}

function connectionDownError() {
  var tabs = CONNECTION.reason === "too_many_connections";
  var err = new Error(tabs ? TOO_MANY_TABS : UNREACHABLE);
  err.code = tabs ? "too_many_connections" : "network";
  err.status = 0;
  return err;
}

function unreachableError() {
  var err = new Error(UNREACHABLE);
  err.code = "network";
  err.status = 0;
  connectionLost(err);
  return err;
}

/* --- DOM ----------------------------------------------------------------- */

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function el(id) { return document.getElementById(id); }

function view() { return el("view"); }

function render(html) { view().innerHTML = html; }

function toast(message) {
  var node = el("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(function () { node.hidden = true; }, 4200);
}

/* --- formatting ---------------------------------------------------------- */

var fmt = {
  cost: function (value) {
    if (value === null || value === undefined) return "—";
    if (value === 0) return "$0";
    return value < 0.01 ? "$" + value.toFixed(5) : "$" + value.toFixed(4);
  },
  tokens: function (value) {
    if (value === null || value === undefined) return "—";
    if (value >= 1000000) return (value / 1000000).toFixed(1) + "M";
    if (value >= 1000) return (value / 1000).toFixed(1) + "k";
    return String(value);
  },
  duration: function (ms) {
    if (ms === null || ms === undefined) return "—";
    if (ms < 1000) return Math.round(ms) + "ms";
    if (ms < 60000) return (ms / 1000).toFixed(1) + "s";
    var minutes = Math.floor(ms / 60000);
    var seconds = Math.round((ms % 60000) / 1000);
    return minutes + "m" + (seconds ? " " + seconds + "s" : "");
  },
  when: function (iso) {
    if (!iso) return "—";
    var then = new Date(iso);
    if (isNaN(then.getTime())) return "—";
    var seconds = (Date.now() - then.getTime()) / 1000;
    if (seconds < 60) return "just now";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    if (seconds < 604800) return Math.floor(seconds / 86400) + "d ago";
    return then.toISOString().slice(0, 10);
  },
  bytes: function (value) {
    if (value === null || value === undefined) return "—";
    if (value >= 1048576) return (value / 1048576).toFixed(1) + " MB";
    if (value >= 1024) return Math.round(value / 1024) + " KB";
    return value + " B";
  },
  num: function (value) {
    if (value === null || value === undefined) return "—";
    return String(value);
  },
  /* Keeps the end of a path, which is the part that tells two workspaces apart. */
  tail: function (text, limit) {
    text = text === null || text === undefined ? "" : String(text);
    return text.length > limit ? "…" + text.slice(text.length - limit + 1) : text;
  },
  truncate: function (text, limit) {
    text = text === null || text === undefined ? "" : String(text);
    return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
  }
};

/* Every legacy `status` word a session record carries, by meaning: a native run writes
 * `success`, an external harness or a flow `completed`, a dashboard chat `finished` — one
 * outcome, three words. Anything else (queued, cancelled, a word from a newer writer) stays
 * the neutral colour rather than claiming success or failure. */
var STATUS_PILL = { success: "success", completed: "success", finished: "success",
                    failed: "failed", refused: "failed", running: "running" };

/* A harness login reading, always with when it was taken: a check days old must not read
 * as current. Runtimes, Providers and Setup all render the same record through this. */
/* An age in seconds as "just now", "5m ago", "3h ago" or "3d ago"; "unknown" when absent. */
function ageText(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds)) return "unknown";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
  return Math.floor(seconds / 86400) + "d ago";
}

/* A future epoch-seconds time as "in 3h"; a past one as "3h ago" (via ageText). */
function untilEpochText(epochSeconds) {
  var seconds = epochSeconds - Date.now() / 1000;
  if (seconds <= 0) return ageText(-seconds);
  return seconds < 60 ? "in under a minute" : "in " + ageText(seconds).replace(/ ago$/, "");
}

function loginAge(check) {
  if (check && check.state === "no_probe") return "no documented status command";
  if (!check || typeof check.age_seconds !== "number") return "never checked";
  return "checked " + ageText(check.age_seconds);
}

function loginPill(check) {
  var state = (check && check.state) || "not_checked";
  var cls = state === "authenticated" ? "success" : (state === "logged_out" ? "failed" : "unknown");
  return '<span class="pill ' + cls + '" data-login="' + esc(state) + '">login: ' + esc(state.replace(/_/g, " ")) +
         "</span> " + '<span class="stat-sub login-age">' + esc(loginAge(check)) + "</span>";
}

function statusPill(status) {
  var cls = STATUS_PILL[status] || "unknown";
  return '<span class="pill ' + cls + '">' + esc(status || "?") + "</span>";
}

/* --- polling -------------------------------------------------------------
 * A setTimeout chain, never setInterval: a slow poll must not stack requests
 * behind itself. One call in flight at a time: `now()` (and a tab becoming
 * visible) during a call does not start a second one — it runs the next call as
 * soon as this one settles. Two calls in flight both read the same cursor, and
 * each used to schedule its own successor, so one visibility change doubled the
 * poll for the rest of the view's life.
 *
 * Pauses while the tab is hidden: nobody is looking, so ordinary views stop
 * fetching. A poll that is also a heartbeat — the chat's, which keeps a parked
 * approval alive — passes `keepAliveHidden`, a predicate; while it returns true
 * the poll keeps running in a hidden tab at `hiddenInterval` (browsers throttle
 * hidden-tab timers anyway, so asking for less than a few seconds buys nothing). */

function Poller(fn, intervalMs, options) {
  options = options || {};
  this.fn = fn;
  this.base = intervalMs;
  this.interval = intervalMs;
  this.hiddenInterval = options.hiddenInterval || intervalMs;
  this.keepAliveHidden = options.keepAliveHidden || null;
  this.timer = null;
  this.stopped = false;
  this.running = false;
  this.again = false;
  this.inflight = null;
  var self = this;
  this._onVisible = function () {
    if (!document.hidden) {
      if (!self.stopped) self.tick();
    } else if (self._paused()) {
      clearTimeout(self.timer);
    }
  };
  document.addEventListener("visibilitychange", this._onVisible);
}

Poller.prototype._paused = function () {
  return !!document.hidden && !(this.keepAliveHidden && this.keepAliveHidden());
};

Poller.prototype.tick = function () {
  var self = this;
  clearTimeout(this.timer);
  this.timer = null;
  if (this.stopped || this._paused()) return;
  if (this.running) { this.again = true; return; }
  this.running = true;
  var result;
  try { result = this.fn(); } catch (err) { result = null; }
  this.inflight = Promise.resolve(result).catch(function () { /* handled by the caller */ }).then(function () {
    self.running = false;
    self.inflight = null;
    if (self.stopped) return;
    if (self.again) { self.again = false; self.tick(); return; }
    var delay = document.hidden ? Math.max(self.interval, self.hiddenInterval) : self.interval;
    self.timer = setTimeout(function () { self.tick(); }, delay);
  });
};

/* Resolves once no call is in flight. After `stop()`, that means nothing more will be sent:
 * what a caller about to delete the thing being polled needs to wait for. */
Poller.prototype.idle = function () { return this.inflight || Promise.resolve(); };

/* Poll as soon as possible without overlapping the call in flight. */
Poller.prototype.now = function () { this.tick(); return this; };

Poller.prototype.start = function () { this.stopped = false; this.tick(); return this; };

Poller.prototype.stop = function () {
  this.stopped = true;
  this.again = false;
  clearTimeout(this.timer);
  document.removeEventListener("visibilitychange", this._onVisible);
};

/* --- theme -------------------------------------------------------------- */

function initTheme() {
  var stored = localStorage.getItem("garuda_theme");
  if (stored) document.documentElement.setAttribute("data-theme", stored);
  el("theme-toggle").addEventListener("click", toggleTheme);
}

function toggleTheme() {
  var current = document.documentElement.getAttribute("data-theme");
  if (!current) {
    current = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  var next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("garuda_theme", next);
}

/* --- sessions a list could not read -------------------------------------------------
 * A list route skips a session whose record is damaged instead of failing (and says so
 * in `unreadable_sessions`), so one bad directory cannot hide every other run. The page
 * says how many were left out rather than silently showing fewer; the ids are in the
 * tooltip and the server log has the reason. */

function unreadableNote(payload) {
  var info = payload && payload.unreadable_sessions;
  var count = info && typeof info.count === "number" ? info.count : 0;
  if (count < 1) return "";
  var ids = Array.isArray(info.ids) ? info.ids.join(", ") : "";
  return '<div class="notice unreadable-note" role="status" title="' + esc(ids) + '">' +
    esc(count + (count === 1 ? " session" : " sessions") + " could not be read") +
    '<span class="notice-body"> — left out of this list; the dashboard’s server log says why.</span></div>';
}

/* --- lost or missing token --------------------------------------------------
 * Any 401 means this tab's token is not the running dashboard's: it never had one, or the
 * dashboard restarted with a new one. One full-page state for both, with a field to paste
 * the printed link into (router.js handles it), since a hard reload was the only other way
 * to replace a token sessionStorage still holds. */

function tokenRequiredPanel() {
  return (
    '<div class="empty token-panel" id="token-panel">' +
    "<p><strong>" + (TOKEN
      ? "This dashboard was restarted or the link expired — open the new link printed by <code>garuda web</code>"
      : "This dashboard needs its link — open the one printed by <code>garuda web</code>") + "</strong></p>" +
    "<p>Or paste that link here (it ends in <code>#t=…</code>):</p>" +
    '<form id="token-form" class="token-form" autocomplete="off">' +
    '<input type="text" id="token-input" spellcheck="false" aria-label="Dashboard link printed by garuda web"' +
    ' placeholder="http://127.0.0.1:…/#t=…">' +
    '<button type="submit" class="btn btn-primary">Open</button></form>' +
    '<p class="stat-sub" id="token-error" role="alert"></p>' +
    "</div>"
  );
}

/* Show the token state once, remembering the page to come back to. */
function authLost() {
  if (el("token-panel")) return;
  var hash = location.hash || "";
  if (hash.indexOf("#/") === 0) {
    try { sessionStorage.setItem("garuda_return", hash); } catch (e) { /* back to the overview */ }
  }
  render(tokenRequiredPanel());
}

/* --- connection ------------------------------------------------------------
 * One banner for the whole app when the dashboard cannot be reached (the server stopped, or
 * it refuses connections because too many tabs are open). A light /api/health probe retries
 * with exponential backoff, capped at 30 s and never sooner than a Retry-After; when it
 * answers, the banner clears itself and every onReconnect() callback runs (the router's
 * refreshes the current view). api() reports failures here itself, so a view renders
 * `err.message` (or apiErrorText(err)) and never needs its own banner or retry loop. */

var UNREACHABLE = "Can’t reach the dashboard — is `garuda web` still running?";
var TOO_MANY_TABS = "Too many open dashboard tabs — the dashboard serves at most 32 connections at once; close some tabs.";
var PROBE_MAX_MS = 30000;
var CONNECTION = { down: false, attempt: 0, delay: 0, timer: null, listeners: [] };

/* The server's connection-limit refusal: a 503 without a route's error envelope, or with a
 * Retry-After. A route that 503s (read-only, no flow gateway) answers in its envelope. */
function isConnectionLimit(response, payload) {
  return response.status === 503 &&
    (!(payload && payload.error) || !!retryAfterHeader(response));
}

function retryAfterHeader(response) {
  return response.headers && typeof response.headers.get === "function" ? response.headers.get("Retry-After") : null;
}

function retryAfterSeconds(response) {
  var value = Number(retryAfterHeader(response));
  return isFinite(value) && value > 0 ? value : 0;
}

/* Whether `err` (from api()) is about the connection rather than the request. */
function isConnectionError(err) {
  return !!err && (err.code === "network" || err.code === "too_many_connections");
}

/* A request failure as one line: the server's message and its error code. */
function apiErrorText(err) {
  if (!err) return "Request failed";
  if (isConnectionError(err) || !err.code) return String(err.message || "Request failed");
  return err.message + " (" + err.code + (err.status ? ", HTTP " + err.status : "") + ")";
}

/* Run `fn` each time the dashboard becomes reachable again. Returns an unsubscribe. */
function onReconnect(fn) {
  CONNECTION.listeners.push(fn);
  return function () {
    CONNECTION.listeners = CONNECTION.listeners.filter(function (other) { return other !== fn; });
  };
}

function connectionLost(err) {
  CONNECTION.reason = (err && err.code) || "network";
  showConnectionBanner(err);
  if (CONNECTION.down) return;
  CONNECTION.down = true;
  CONNECTION.attempt = 0;
  scheduleProbe(err && err.retryAfter);
}

function scheduleProbe(retryAfter) {
  clearTimeout(CONNECTION.timer);
  var delay = Math.min(1000 * Math.pow(2, CONNECTION.attempt), PROBE_MAX_MS);
  if (retryAfter) delay = Math.max(delay, retryAfter * 1000);
  CONNECTION.attempt += 1;
  CONNECTION.delay = delay;
  CONNECTION.timer = setTimeout(probeConnection, delay);
}

function probeConnection() {
  clearTimeout(CONNECTION.timer);
  if (!CONNECTION.down) return;
  fetch("/api/health", { headers: { "X-Garuda-Token": TOKEN }, cache: "no-store" }).then(function (response) {
    if (isConnectionLimit(response, null)) {
      var err = { code: "too_many_connections", retryAfter: retryAfterSeconds(response) };
      CONNECTION.reason = err.code;
      showConnectionBanner(err);
      scheduleProbe(err.retryAfter);
      return;
    }
    connectionRestored();
    if (response.status === 401) authLost();
  }, function () {
    CONNECTION.reason = "network";
    showConnectionBanner({ code: "network" });
    scheduleProbe();
  });
}

function connectionRestored() {
  if (!CONNECTION.down) return;
  CONNECTION.down = false;
  clearTimeout(CONNECTION.timer);
  var banner = el("conn-banner");
  if (banner) banner.hidden = true;
  CONNECTION.listeners.slice().forEach(function (fn) {
    try { fn(); } catch (e) { /* one listener must not stop the rest */ }
  });
}

function showConnectionBanner(err) {
  var banner = el("conn-banner");
  if (!banner) return;
  var tabs = err && err.code === "too_many_connections";
  el("conn-banner-text").textContent = (tabs ? TOO_MANY_TABS : UNREACHABLE) + " Retrying…";
  banner.setAttribute("data-reason", tabs ? "too_many_connections" : "network");
  banner.hidden = false;
}

/* --- write actions -----------------------------------------------------------
 * guardAction(button, fn, {confirm, busy, errorTarget}) runs one write behind a button: it
 * asks `confirm` first when given, disables the button and shows "working…" (or `busy`)
 * until the promise `fn` returns settles, so a double-click sends one request, then
 * restores the button. A failure is shown with the server's message and error code, in
 * `errorTarget` (an element or its id) or else a toast, and resolves to undefined; success
 * resolves to what `fn` returned. */

function guardAction(button, fn, options) {
  options = options || {};
  if (!button || button.disabled || button.getAttribute("aria-busy") === "true") return Promise.resolve(undefined);
  if (options.confirm && !window.confirm(options.confirm)) return Promise.resolve(undefined);
  var label = button.innerHTML;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = options.busy || "working…";
  function restore() {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.innerHTML = label;
  }
  return Promise.resolve().then(fn).then(function (value) {
    restore();
    return value;
  }, function (err) {
    restore();
    if (isSuperseded(err)) return undefined;  // the page moved on; nothing to report
    var text = apiErrorText(err);
    var target = typeof options.errorTarget === "string" ? el(options.errorTarget) : options.errorTarget;
    if (target) {
      target.innerHTML = '<div class="notice err" role="alert"><div class="notice-title">Request failed</div>' +
        '<div class="notice-body">' + esc(text) + "</div></div>";
    } else {
      toast(text);
    }
    return undefined;
  });
}
