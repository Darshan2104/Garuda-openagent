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
  location.replace("#/runs");
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
 * behind itself. Pauses when the tab is hidden, which matters more later — a
 * background tab's poll is what keeps an approval parked in write mode. */

function Poller(fn, intervalMs) {
  this.fn = fn;
  this.base = intervalMs;
  this.interval = intervalMs;
  this.timer = null;
  this.stopped = false;
  var self = this;
  this._onVisible = function () {
    if (document.hidden) {
      clearTimeout(self.timer);
    } else if (!self.stopped) {
      self.tick();
    }
  };
  document.addEventListener("visibilitychange", this._onVisible);
}

Poller.prototype.tick = function () {
  var self = this;
  clearTimeout(this.timer);
  if (this.stopped || document.hidden) return;
  Promise.resolve(this.fn()).catch(function () { /* handled by the caller */ }).then(function () {
    if (!self.stopped) self.timer = setTimeout(function () { self.tick(); }, self.interval);
  });
};

Poller.prototype.start = function () { this.stopped = false; this.tick(); return this; };

Poller.prototype.stop = function () {
  this.stopped = true;
  clearTimeout(this.timer);
  document.removeEventListener("visibilitychange", this._onVisible);
};

/* --- theme -------------------------------------------------------------- */

function initTheme() {
  var stored = localStorage.getItem("garuda_theme");
  if (stored) document.documentElement.setAttribute("data-theme", stored);
  el("theme-toggle").addEventListener("click", function () {
    var current = document.documentElement.getAttribute("data-theme");
    if (!current) {
      current = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    var next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("garuda_theme", next);
  });
}

function tokenRequiredPanel() {
  return (
    '<div class="empty">' +
    "<p><strong>This dashboard needs its token.</strong></p>" +
    "<p>Relaunch <code>garuda web</code> and open the URL it prints.</p>" +
    "</div>"
  );
}
