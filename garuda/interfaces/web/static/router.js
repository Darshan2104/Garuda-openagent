/* Hash routing. Loaded last; bootstraps the app.
 *
 * Hash rather than the History API: no server-side rewrite rules, the back button is free,
 * and a reload of a deep link cannot 404. One handler, and each navigation tears down the
 * previous view\'s poller before mounting the next — a leaked poller would keep fetching a
 * run nobody is looking at.
 *
 * Two views, and that is the whole app: the run list (with the stats over it) and one run\'s
 * trace, plus the conversation. Anything that was neither reading a trace nor talking to the
 * agent has been removed rather than kept behind a nav entry. */

"use strict";

var ROUTES = [
  [/^#\/runs\/([^/]+)$/, function (m) { return runDetailView(decodeURIComponent(m[1])); }],
  [/^#\/runs$/, function () { return runsView(); }],
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
  var hash = location.hash || "#/runs";
  setActiveNav(hash);
  for (var i = 0; i < ROUTES.length; i++) {
    var match = hash.match(ROUTES[i][0]);
    if (match) { ROUTES[i][1](match); return; }
  }
  render('<div class="empty"><p>No such view.</p><p><a href="#/runs">Back to runs</a></p></div>');
}

function setActiveNav(hash) {
  var section = hash.slice(2).split("/")[0] || "runs";
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
    })
    .catch(function (err) {
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      render('<div class="notice err"><div class="notice-title">Dashboard unreachable</div>' +
             '<div class="notice-body">' + esc(err.message) + "</div></div>");
    });
}

window.addEventListener("hashchange", navigate);
boot();
