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
  [/^#\/starters(?:\/([^/?]+))?(?:\?(.*))?$/, function (m) { return startersView(m[1] ? decodeURIComponent(m[1]) : null, m[2]); }],
  [/^#\/runs\/([^/]+)$/, function (m) { return runDetailView(decodeURIComponent(m[1])); }],
  [/^#\/runs$/, function () { return runsView(); }],
  [/^#\/sessions\/([^/]+)$/, function (m) { return sessionDetailView(decodeURIComponent(m[1])); }],
  [/^#\/sessions$/, function () { return sessionsView(); }],
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
  // response render into the view that replaced it. stopChat() is the same idea for the chat
  // poller — and leaving that one running would keep refreshing the heartbeat for a chat
  // nobody is looking at, which is precisely what the reaper is for.
  stopLive();
  stopChat();
  if (typeof stopSessionStream === "function") stopSessionStream();
  stopStarterViews();
  var hash = location.hash || "#/runs";
  setActiveNav(hash);
  for (var i = 0; i < ROUTES.length; i++) {
    var match = hash.match(ROUTES[i][0]);
    if (match) { ROUTES[i][1](match); return; }
  }
  render('<div class="empty"><p>No such view.</p><p><a href="#/runs">Back to runs</a></p></div>');
}

function setActiveNav(hash) {
  var section = hash.slice(2).split("?")[0].split("/")[0] || "runs";
  var links = document.querySelectorAll(".nav-link");
  for (var i = 0; i < links.length; i++) {
    links[i].classList.toggle("active", links[i].getAttribute("data-nav") === section);
  }
}

function boot() {
  initTheme();
  // Health and config together: the chat composer is built from config, and rendering it from
  // a second round trip would flash an empty panel on every navigation. A 401 on health is
  // the one error worth replacing the whole view for.
  Promise.all([api("/api/health"), api("/api/config").catch(function () { return {}; })])
    .then(function (both) {
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
      startInboxBadge();
    })
    .catch(function (err) {
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      render('<div class="notice err"><div class="notice-title">Dashboard unreachable</div>' +
             '<div class="notice-body">' + esc(err.message) + "</div></div>");
    });
}

/* --- approvals count in the nav --------------------------------------------
 * A slow read-only poll of the same /api/inbox the Approvals view renders, so a parked
 * request is visible from any page. It answers nothing and refreshes no heartbeat, and the
 * Poller pauses it while the tab is hidden. */

var INBOX_BADGE = null;

function startInboxBadge() {
  if (INBOX_BADGE) return;
  INBOX_BADGE = new Poller(refreshInboxBadge, 20000).start();
}

function refreshInboxBadge() {
  return api("/api/inbox").then(function (payload) {
    var count = (payload && payload.approvals ? payload.approvals : []).filter(function (a) { return !a.expired; }).length;
    var badge = el("nav-inbox-count");
    if (!badge) return;
    badge.textContent = count > 99 ? "99+" : String(count);
    badge.hidden = count === 0;
    badge.title = count + " approval" + (count === 1 ? "" : "s") + " waiting";
  }).catch(function () { /* the badge is a convenience; the Approvals view reports errors */ });
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
var VIEW_INPUT_REVISION = 0;

function markViewInputDirty(event) {
  if (event.target.matches("input, textarea, select")) {
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
}

function visibleNavLinks() {
  return Array.prototype.filter.call(document.querySelectorAll(".nav-link"), function (link) { return !link.hidden; });
}

function onShortcut(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "Escape") {
    toggleKeysHelp(false);
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
    // Re-mounting would discard unsubmitted edits, including after a field loses focus.
    if ((location.hash || "").indexOf("#/chat") !== 0 && !view().querySelector("[data-dirty]")) {
      navigate(); refreshInboxBadge();
    }
    return;
  }
  if (event.key === "t") { toggleTheme(); return; }
  if (/^[1-9]$/.test(event.key)) {
    var link = visibleNavLinks()[Number(event.key) - 1];
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

window.addEventListener("hashchange", navigate);
boot();
