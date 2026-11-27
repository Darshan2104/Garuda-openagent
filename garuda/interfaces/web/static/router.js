/* Hash routing. Loaded last; bootstraps the app.
 *
 * Hash rather than the History API: no server-side rewrite rules, the back button is free,
 * and a reload of a deep link cannot 404. One handler, and each navigation tears down the
 * previous view\'s poller before mounting the next — a leaked poller would keep fetching a
 * run nobody is looking at.
 *
 * Starters compile read-only previews. Existing session and trace views show their
 * recorded evidence; navigation also invalidates pending starter requests. */

"use strict";

var ROUTES = [
  [/^#\/(?:overview)?$/, function () { return overviewView(); }],
  [/^#\/starters(?:\/([^/?]+))?(?:\?(.*))?$/, function (m) { return startersView(m[1] ? decodeURIComponent(m[1]) : null, m[2]); }],
  [/^#\/runs\/([^/]+)$/, function (m) { return runDetailView(decodeURIComponent(m[1])); }],
  // A list page's filters ride in its query (routeParams() below), so a filtered list
  // survives a reload, can be shared, and back/forward steps through it.
  [/^#\/runs(?:\?.*)?$/, function () { return runsView(); }],
  [/^#\/sessions\/([^/]+)$/, function (m) { return sessionDetailView(decodeURIComponent(m[1])); }],
  [/^#\/sessions(?:\?.*)?$/, function () { return sessionsView(); }],
  [/^#\/inbox$/, function () { return inboxView(); }],
  [/^#\/providers$/, function () { return providersView(); }],
  [/^#\/setup$/, function () { return setupView(); }],
  [/^#\/usage(?:\?range=([0-9a-z]+))?$/, function (m) { return usageView(m[1]); }],
  [/^#\/runtimes$/, function () { return runtimesView(); }],
  [/^#\/chat$/, function () { return chatView(); }]
];

function navigate() {
  // stopLive() clears STATE.live as well as the poller: an in-flight poll checks that
  // reference to decide whether its response is still wanted, so leaving it set lets a late
  // response render into the view that replaced it. leaveChat() unmounts the chat but keeps
  // its heartbeat while a turn runs or an approval waits: the chat lives on the server either
  // way, and stopping here denied the user's own approval. Closing the tab still stops it.
  // A new generation first: reads the old view still has in flight reject as Superseded.
  beginNavigation();
  VIEW_RECONNECTS_IN_PLACE = false;
  stopLive();
  leaveChat();
  if (typeof stopSessionStream === "function") stopSessionStream();
  stopStarterViews();
  stopHomeView();
  var hash = location.hash || "#/";
  setActiveNav(hash);
  for (var i = 0; i < ROUTES.length; i++) {
    var match = hash.match(ROUTES[i][0]);
    if (match) { ROUTES[i][1](match); return; }
  }
  render('<div class="empty" id="no-such-view"><p><strong>No such view.</strong></p>' +
         '<p class="stat-sub">Nothing lives at <code>' + esc(hash) + "</code>.</p>" +
         '<p class="empty-links"><a class="btn btn-primary" href="#/">Go to the overview</a> ' +
         '<a class="btn" href="#/runs">Runs</a> <a class="btn" href="#/sessions">Sessions</a></p></div>');
}

/* --- route query -------------------------------------------------------------
 * routeParams() reads the current route's query (`#/runs?q=fib&status=failed`) as a plain
 * object of strings: {q: "fib", status: "failed"}; {} when there is none.
 * setRouteParams(params, {push}) writes `params` back into the current route's query
 * without re-mounting the view (history.pushState/replaceState fire no hashchange). Empty
 * and null values are dropped. `push: true` adds a history entry (a filter was chosen);
 * otherwise the current entry is replaced (typing in a search box). Back and forward
 * change the hash, so the router re-mounts the view from the URL. */

function routeParams(hash) {
  hash = hash === undefined ? (location.hash || "") : hash;
  var params = {};
  var at = hash.indexOf("?");
  if (at < 0) return params;
  new URLSearchParams(hash.slice(at + 1)).forEach(function (value, key) { params[key] = value; });
  return params;
}

function setRouteParams(params, options) {
  var hash = location.hash || "#/";
  var query = new URLSearchParams();
  Object.keys(params || {}).forEach(function (key) {
    var value = params[key];
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  });
  var next = hash.split("?")[0] + (query.toString() ? "?" + query.toString() : "");
  if (next === hash) return;
  var url = location.pathname + location.search + next;
  if (options && options.push) history.pushState(null, "", url);
  else history.replaceState(null, "", url);
}

/* --- nav --------------------------------------------------------------------
 * Wide windows show every tab. Narrow ones show one menu button naming the current page
 * (with the approvals count), which opens the full list; Escape or choosing a page closes
 * it. Same links either way, so the number keys and Tab order do not change. */

function setActiveNav(hash) {
  var section = hash.slice(2).split("?")[0].split("/")[0] || "overview";
  var links = document.querySelectorAll(".nav-link");
  var current = null;
  for (var i = 0; i < links.length; i++) {
    var on = links[i].getAttribute("data-nav") === section;
    links[i].classList.toggle("active", on);
    if (on) { links[i].setAttribute("aria-current", "page"); current = links[i]; }
    else links[i].removeAttribute("aria-current");
  }
  var label = el("nav-current");
  if (label) label.textContent = current ? navLabel(current) : "Menu";
}

function navLabel(link) {
  return (link.childNodes[0] && link.childNodes[0].textContent || link.textContent).trim();
}

function navMenuOpen() {
  return document.querySelector(".topbar").classList.contains("nav-open");
}

function setNavMenu(open, focusTarget) {
  var button = el("nav-menu");
  document.querySelector(".topbar").classList.toggle("nav-open", open);
  button.setAttribute("aria-expanded", String(open));
  if (open) {
    var first = document.querySelector('.nav-link[aria-current="page"]:not([hidden])') || visibleNavLinks()[0];
    if (first) first.focus();
  } else if (focusTarget) {
    focusTarget.focus();
  }
}

el("nav-menu").addEventListener("click", function () { setNavMenu(!navMenuOpen()); });
el("nav").addEventListener("click", function (event) {
  if (event.target.closest(".nav-link") && navMenuOpen()) setNavMenu(false);
});
document.addEventListener("click", function (event) {
  if (navMenuOpen() && !event.target.closest(".topbar")) setNavMenu(false);
});

/* --- token arriving in an open tab -------------------------------------------- */

// Set while the router swaps a `#t=` hash for the real route: the hashchange that swap
// causes re-boots with the new token instead of only re-mounting the view.
var REBOOT_ON_HASH = false;

function adoptToken(token) {
  setToken(token);
  var route = takeReturnRoute();
  if ((location.hash || "") === route) { boot(); return; }
  REBOOT_ON_HASH = true;
  location.replace(route);
}

/* Listening from the start, so a `#t=` link pasted into the token page is seen. While a
 * boot is in flight a plain hash change is left to it: boot's own navigate() reads the
 * current hash, and mounting here too mounted the view twice (#840: the second mount's
 * "Loading…" wiped the first one's table). */
function onHashChange() {
  var token = hashToken(location.hash);
  if (token !== null) { adoptToken(token); return; }
  if (REBOOT_ON_HASH) { REBOOT_ON_HASH = false; boot(); return; }
  if (navMenuOpen()) setNavMenu(false);
  if (BOOT.pending) return;
  navigate();
}

/* A pasted dashboard link: {token, url} for this dashboard or another loopback one. */
function parseDashboardLink(text) {
  var at = text.indexOf("#t=");
  if (at < 0) return /^[A-Za-z0-9_.~-]{8,}$/.test(text) ? { token: text, url: null } : null;
  var token = hashToken(text.slice(at));
  if (!token) return null;
  if (at === 0) return { token: token, url: null };
  var url = null;
  try { url = new URL(text); } catch (e) { return null; }
  var loopback = ["127.0.0.1", "localhost", "[::1]"].indexOf(url.hostname) !== -1;
  if (!/^https?:$/.test(url.protocol) || !loopback) return null;
  return { token: token, url: url };
}

document.addEventListener("submit", function (event) {
  if (!event.target || event.target.id !== "token-form") return;
  event.preventDefault();
  var link = parseDashboardLink(el("token-input").value.trim());
  if (!link) {
    el("token-error").textContent = "That is not a dashboard link: paste the whole URL `garuda web` printed, " +
      "ending in #t= and the token.";
    return;
  }
  // The dashboard came back on another port: open the link there (its page reads the token).
  if (link.url && link.url.origin !== location.origin) { location.assign(link.url.href); return; }
  el("token-error").textContent = "Checking the link…";
  adoptToken(link.token);
});

/* --- boot ------------------------------------------------------------------- */

var THEME_READY = false;
// `seq` lets only the latest boot finish (a token pasted mid-boot starts another one);
// `pending` is true while one is in flight.
var BOOT = { seq: 0, pending: false };

function boot() {
  if (!THEME_READY) { initTheme(); THEME_READY = true; }
  var seq = ++BOOT.seq;
  BOOT.pending = true;
  // Health and config together: the chat composer is built from config, and rendering it from
  // a second round trip would flash an empty panel on every navigation. A 401 on health is
  // the one error worth replacing the whole view for.
  // App-wide reads ({keep: true}): a navigation while booting must not drop them.
  Promise.all([api("/api/health", { keep: true }),
               api("/api/config", { keep: true }).catch(function () { return {}; })])
    .then(function (both) {
      if (seq !== BOOT.seq) return;
      BOOT.pending = false;
      var health = both[0];
      STATE.config = both[1];
      STATE.health = health;
      var badge = el("mode-badge");
      badge.textContent = health.mode;
      badge.classList.toggle("rw", health.mode === "read-write");
      el("version-label").textContent = "v" + health.version + " · :" + health.port;
      // Chat needs an agent loop and a dashboard not started read-only, so the nav entry is
      // hidden rather than leading to a page that explains it cannot work.
      if (!health.capabilities.chat) {
        var chatLink = el("nav-chat");
        if (chatLink) chatLink.hidden = true;
      }
      navigate();
      if (INBOX_BADGE) refreshInboxBadge(); else startInboxBadge();
    })
    .catch(function (err) {
      if (seq !== BOOT.seq) return;
      BOOT.pending = false;
      if (err.status === 401) {
        authLost();
        var note = el("token-error");
        if (note && note.textContent) note.textContent = "That link was refused: it is not this dashboard's current link.";
        return;
      }
      render('<div class="notice err" id="boot-error"><div class="notice-title">Dashboard unreachable</div>' +
             '<div class="notice-body">' + esc(apiErrorText(err)) + "</div></div>");
    });
}

/* A view that refreshes itself in place when the dashboard comes back (its own onReconnect
 * handler polls now) calls this while mounting, so a reconnect does not re-mount it. */
var VIEW_RECONNECTS_IN_PLACE = false;

function reconnectInPlace() { VIEW_RECONNECTS_IN_PLACE = true; }

/* Re-mounting would discard unsubmitted edits (including after a field loses focus), and
 * the chat keeps its own poll. */
function canRemountView() {
  return (location.hash || "").indexOf("#/chat") !== 0 && !view().querySelector("[data-dirty]");
}

// Back from an outage: boot if it never finished, else refresh what is on screen.
onReconnect(function () {
  if (el("boot-error") || !STATE.health) { boot(); return; }
  if (el("token-panel")) return;
  if (canRemountView() && !VIEW_RECONNECTS_IN_PLACE) navigate();
  refreshInboxBadge();
});

/* --- approvals count in the nav --------------------------------------------
 * A slow read-only poll of the same /api/inbox the Approvals view renders, so a parked
 * request is visible from any page. It answers nothing and refreshes no heartbeat, and the
 * Poller pauses it while the tab is hidden. While the dashboard is unreachable it sends
 * nothing (the connection probe is the one request then), a failure doubles its interval up
 * to BADGE_MAX_MS, and a success or a reconnect resets it. The count is mirrored on the
 * narrow-window menu button so it is visible on every page at every width. */

var INBOX_BADGE = null;
var BADGE_BASE_MS = 20000;
var BADGE_MAX_MS = 160000;

function startInboxBadge() {
  if (INBOX_BADGE) return;
  INBOX_BADGE = new Poller(refreshInboxBadge, BADGE_BASE_MS).start();
}

function refreshInboxBadge() {
  if (CONNECTION.down) return Promise.resolve();
  return api("/api/inbox", { keep: true }).then(function (payload) {
    if (INBOX_BADGE) INBOX_BADGE.interval = BADGE_BASE_MS;
    var count = (payload && payload.approvals ? payload.approvals : []).filter(function (a) { return !a.expired; }).length;
    ["nav-inbox-count", "nav-menu-count"].forEach(function (id) {
      var badge = el(id);
      if (!badge) return;
      badge.textContent = count > 99 ? "99+" : String(count);
      badge.hidden = count === 0;
      badge.title = count + " approval" + (count === 1 ? "" : "s") + " waiting";
    });
  }).catch(function () {
    // The badge is a convenience; the Approvals view reports errors.
    if (INBOX_BADGE) INBOX_BADGE.interval = Math.min(INBOX_BADGE.interval * 2, BADGE_MAX_MS);
  });
}

/* --- keyboard shortcuts ------------------------------------------------------
 * Single keys, never while typing in a field and never with a modifier, so they cannot
 * collide with the chat composer's Cmd/Ctrl+Enter or with browser shortcuts. */

function isTypingTarget(node) {
  if (!node) return false;
  var tag = node.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || node.isContentEditable;
}

// Mark the fields themselves so replacing a view clears its edits. Views that preserve
// values across a render must preserve these markers too (as the runtime board does).
// A control marked `data-route-param` keeps its value in the URL (setRouteParams), so a
// re-mount restores it and it never counts as an unsubmitted edit.
var VIEW_INPUT_REVISION = 0;

function markViewInputDirty(event) {
  if (event.target.matches("input, textarea, select") && !event.target.hasAttribute("data-route-param")) {
    event.target.setAttribute("data-dirty", String(++VIEW_INPUT_REVISION));
  }
}

function submittedViewInputs(root) {
  var revisions = Array.from(root.querySelectorAll("[data-dirty]")).map(function (node) {
    return node.getAttribute("data-dirty");
  });
  // Call only after success: a failed request or edits made while it was in flight
  // must keep blocking reload. Unique revisions also survive a value-preserving render.
  return function () {
    view().querySelectorAll("[data-dirty]").forEach(function (node) {
      if (revisions.indexOf(node.getAttribute("data-dirty")) !== -1) node.removeAttribute("data-dirty");
    });
  };
}

function toggleKeysHelp(show) {
  var help = el("keys-help");
  if (!help) return;
  help.hidden = show === undefined ? !help.hidden : !show;
  if (!help.hidden) listTabKeys();
}

/* The number keys, listed from the tabs this dashboard shows (Chat is hidden when it
 * cannot work), so the help cannot drift from what the keys do. */
function listTabKeys() {
  var list = el("keys-tabs");
  if (!list) return;
  list.textContent = "";
  visibleNavLinks().slice(0, 10).forEach(function (link, i) {
    var item = document.createElement("li");
    var key = document.createElement("kbd");
    key.textContent = i === 9 ? "0" : String(i + 1);
    item.appendChild(key);
    item.appendChild(document.createTextNode(" " + navLabel(link)));
    list.appendChild(item);
  });
}

function visibleNavLinks() {
  return Array.prototype.filter.call(document.querySelectorAll(".nav-link"), function (link) { return !link.hidden; });
}

function onShortcut(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "Escape") {
    toggleKeysHelp(false);
    if (navMenuOpen()) { setNavMenu(false, el("nav-menu")); return; }
    if (isTypingTarget(document.activeElement)) document.activeElement.blur();
    return;
  }
  if (isTypingTarget(event.target)) return;
  if (event.key === "?") { toggleKeysHelp(); event.preventDefault(); return; }
  if (event.key === "/") {
    var search = view().querySelector('input[type="search"]');
    if (search) { search.focus(); search.select(); event.preventDefault(); }
    return;
  }
  if (event.key === "r") {
    if (canRemountView()) { navigate(); refreshInboxBadge(); }
    return;
  }
  if (event.key === "t") { toggleTheme(); return; }
  // 1-9 are the first nine tabs and 0 the tenth, as on a keyboard's number row.
  if (/^[0-9]$/.test(event.key)) {
    var link = visibleNavLinks()[event.key === "0" ? 9 : Number(event.key) - 1];
    if (link) location.hash = link.getAttribute("href");
  }
}

document.addEventListener("keydown", onShortcut);
view().addEventListener("input", markViewInputDirty);
view().addEventListener("change", markViewInputDirty);
el("keys-toggle").addEventListener("click", function () { toggleKeysHelp(); });
el("keys-help").addEventListener("click", function (event) {
  if (event.target === el("keys-help")) toggleKeysHelp(false);
});

el("conn-retry").addEventListener("click", probeConnection);

window.addEventListener("hashchange", onHashChange);
boot();
