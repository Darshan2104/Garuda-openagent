/* Shared helpers. Loaded first; everything else assumes these globals.
 *
 * No modules, no build step, one global scope — the same posture as the rest of this
 * package's zero-dependency approach. */

"use strict";

/* --- token bootstrap ------------------------------------------------------
 * The token and the route both live in the hash, so the token occupies it for
 * exactly one tick before being swapped for the real route.
 *
 * sessionStorage, not localStorage: per-tab, and it does not persist for whatever
 * else binds this port next week. A tab opened from history therefore has no token
 * and is told to relaunch, which is the fail-closed outcome. */

var TOKEN = sessionStorage.getItem("garuda_token") || "";
if (location.hash.indexOf("#t=") === 0) {
  TOKEN = location.hash.slice(3);
  sessionStorage.setItem("garuda_token", TOKEN);
  location.replace("#/");
}

var STATE = {
  health: null, runs: [], run: null, config: null, poller: null, filters: {},
  // The live tail\'s state object, checked by identity so a late poll can tell it belongs to
  // a view that has since been replaced.
  live: null, chat: null, raw: null, openTurns: null
};

/* --- fetch ---------------------------------------------------------------- */

function api(path, opts) {
  opts = opts || {};
  var headers = { "X-Garuda-Token": TOKEN };
  if (opts.body) headers["Content-Type"] = "application/json";
  return fetch(path, {
    method: opts.method || "GET",
    headers: headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  }).then(function (response) {
    return response.text().then(function (text) {
      var payload = null;
      try { payload = text ? JSON.parse(text) : null; } catch (e) { payload = null; }
      if (response.ok) return payload;
      var err = new Error((payload && payload.error && payload.error.message) || response.statusText);
      err.code = (payload && payload.error && payload.error.code) || "unavailable";
      err.status = response.status;
      throw err;
    });
  });
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

function statusPill(status) {
  var known = { success: 1, failed: 1, running: 1 };
  var cls = known[status] ? status : "unknown";
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
  Promise.resolve(result).catch(function () { /* handled by the caller */ }).then(function () {
    self.running = false;
    if (self.stopped) return;
    if (self.again) { self.again = false; self.tick(); return; }
    var delay = document.hidden ? Math.max(self.interval, self.hiddenInterval) : self.interval;
    self.timer = setTimeout(function () { self.tick(); }, delay);
  });
};

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

function tokenRequiredPanel() {
  return (
    '<div class="empty">' +
    "<p><strong>This dashboard needs its token.</strong></p>" +
    "<p>Relaunch <code>garuda web</code> and open the URL it prints.</p>" +
    "</div>"
  );
}
