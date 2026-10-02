/* The runtimes view: harness picker, health, handoff, diff, and recovery.
 *
 * One-shot fetches over the pure routes (`/api/runtimes*`,
 * `/api/runs/<id>/{handoff,diff,recover}`) — no pollers here, so there is
 * nothing to leak across navigations. Mutating controls (prepare, recover)
 * render disabled unless the dashboard runs read-write; the server refuses
 * them too, and the disabled buttons say so rather than failing silently. */

"use strict";

function canWrite() {
  return !!(STATE.health && STATE.health.mode === "read-write");
}

function runtimesView() {
  render('<div class="loading">Loading runtimes…</div>');
  api("/api/runtimes")
    .then(function (runtimes) {
      STATE.runtimes = runtimes;
      renderRuntimes(runtimes, null, null);
    })
    .catch(function (err) {
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      render('<div class="notice err"><div class="notice-title">Could not load runtimes</div>' +
             '<div class="notice-body">' + esc(err.message) + "</div></div>");
    });
}

function rtPill(rt) {
  if (!rt.available) return '<span class="pill unknown">unavailable</span>';
  return '<span class="pill success">available</span>';
}

function renderRuntimes(runtimes, detail, detailId) {
  var rows = runtimes.map(function (rt) {
    return "<tr" + (detailId === rt.runtime_id ? ' class="selected"' : "") + ">" +
      '<td><a href="#" data-rt="' + esc(rt.runtime_id) + '">' + esc(rt.runtime_id) + "</a></td>" +
      "<td>" + esc(rt.kind) + "</td>" +
      "<td>" + rtPill(rt) + "</td>" +
      "<td>" + esc(rt.version) + "</td>" +
      "<td>" + esc((rt.auth || "").toUpperCase()) + "</td>" +
      "</tr>";
  }).join("");
  var detailHtml = '<div class="empty" id="rt-detail"><p>Select a runtime for detail.</p></div>';
  if (detail) detailHtml = renderRtDetail(detail);
  render(
    '<div class="page-head"><h1>Runtimes</h1>' +
    '<span class="meta">' + runtimes.length + " harnesses</span></div>" +
    '<div class="table-wrap"><table id="rt-table"><thead><tr>' +
    "<th>Runtime</th><th>Kind</th><th>Health</th><th>Version</th><th>Auth</th>" +
    "</tr></thead><tbody>" + rows + "</tbody></table></div>" +
    detailHtml +
    renderRtOps()
  );
  var links = document.querySelectorAll("#rt-table a[data-rt]");
  for (var i = 0; i < links.length; i++) {
    links[i].addEventListener("click", function (ev) {
      ev.preventDefault();
      selectRuntime(ev.currentTarget.getAttribute("data-rt"));
    });
  }
  wireRtOps();
}

function renderRtDetail(rt) {
  var caps = (rt.capabilities || []).join(", ") || "—";
  var warn = (rt.warnings || []).map(function (w) {
    return '<div class="notice-body">⚠ ' + esc(w) + "</div>";
  }).join("");
  var auth = (rt.auth_guidance || []).map(function (line) {
    return "<div>" + esc(line) + "</div>";
  }).join("");
  return '<div class="card" id="rt-detail"><h2>' + esc(rt.runtime_id) + "</h2>" +
    '<div class="meta">kind ' + esc(rt.kind) + " · version " + esc(rt.version) +
    " · auth " + esc(rt.auth) + "</div>" +
    (rt.executable ? '<div class="meta">executable <code>' + esc(rt.executable) + "</code></div>" : "") +
    '<div class="meta">capabilities: ' + esc(caps) + "</div>" +
    (auth ? '<div class="stack">' + auth + "</div>" : "") +
    warn + "</div>";
}

function selectRuntime(runtimeId) {
  api("/api/runtimes/" + encodeURIComponent(runtimeId))
    .then(function (detail) {
      renderRuntimes(STATE.runtimes || [], detail, runtimeId);
    })
    .catch(function (err) {
      renderRuntimes(STATE.runtimes || [], null, null);
      var box = el("rt-select-error");
      if (box) {
        box.innerHTML = '<div class="notice err"><div class="notice-title">Could not inspect ' +
          esc(runtimeId) + '</div><div class="notice-body">' + esc(err.message) + "</div></div>";
      }
    });
}

function renderRtOps() {
  var write = canWrite();
  var gate = write ? "" : '<div class="notice"><div class="notice-body">' +
    "Dashboard is read-only: prepare and recover actions are disabled.</div></div>";
  return '<div class="grid cols-4">' +
    '<div class="card"><h2>Handoff</h2>' +
    '<label>Session <input id="rt-handoff-session" placeholder="session id"></label>' +
    '<label>Target <input id="rt-handoff-target" placeholder="runtime id"></label>' +
    '<div class="row"><button type="button" class="btn" id="rt-handoff-preview">Preview</button>' +
    '<button type="button" class="btn btn-primary" id="rt-handoff-prepare"' +
    (write ? "" : " disabled") + ">Prepare</button></div>" +
    '<div id="rt-handoff-result"></div></div>' +
    '<div class="card"><h2>Diff</h2>' +
    '<label>Session <input id="rt-diff-session" placeholder="session id"></label>' +
    '<div class="row"><button type="button" class="btn" id="rt-diff-load">Load diff</button></div>' +
    '<div id="rt-diff-result"></div></div>' +
    '<div class="card"><h2>Recovery</h2>' +
    '<label>Session <input id="rt-recover-session" placeholder="session id"></label>' +
    '<div class="row"><button type="button" class="btn" id="rt-recover-classify">Classify</button>' +
    '<button type="button" class="btn btn-primary" id="rt-recover-run"' +
    (write ? "" : " disabled") + ">Recover</button></div>" +
    '<div id="rt-recover-result"></div></div>' +
    "</div>" + gate + '<div id="rt-select-error"></div>';
}

function rtResult(boxId, html) {
  var box = el(boxId);
  if (box) box.innerHTML = html;
}

function rtError(err) {
  return '<div class="notice err"><div class="notice-title">Request failed</div>' +
    '<div class="notice-body">' + esc(err.message) + "</div></div>";
}

function wireRtOps() {
  el("rt-handoff-preview").addEventListener("click", function () {
    var sid = el("rt-handoff-session").value.trim();
    var target = el("rt-handoff-target").value.trim();
    if (!sid || !target) {
      rtResult("rt-handoff-result", '<div class="notice err"><div class="notice-body">' +
        "Session and target are both required.</div></div>");
      return;
    }
    api("/api/runs/" + encodeURIComponent(sid) + "/handoff?to=" + encodeURIComponent(target))
      .then(function (preview) {
        rtResult("rt-handoff-result",
          '<div class="meta">source <strong>' + esc(preview.source_runtime) + "</strong> → target <strong>" +
          esc(preview.target_runtime) + "</strong></div>" +
          '<div class="meta">handoff state: ' + esc(preview.handoff_state) + "</div>" +
          '<div class="meta">requires confirm: ' + esc(String(preview.requires_confirm)) + "</div>");
      })
      .catch(function (err) { rtResult("rt-handoff-result", rtError(err)); });
  });
  el("rt-handoff-prepare").addEventListener("click", function () {
    var sid = el("rt-handoff-session").value.trim();
    var target = el("rt-handoff-target").value.trim();
    if (!sid || !target) {
      rtResult("rt-handoff-result", '<div class="notice err"><div class="notice-body">' +
        "Session and target are both required.</div></div>");
      return;
    }
    api("/api/runs/" + encodeURIComponent(sid) + "/handoff",
        { method: "POST", body: { target: target } })
      .then(function (prepared) {
        rtResult("rt-handoff-result",
          '<div class="meta">handoff state: <strong>' + esc(prepared.handoff_state) + "</strong></div>" +
          '<div class="meta">' + esc(prepared.handoff_file || "") + "</div>");
      })
      .catch(function (err) { rtResult("rt-handoff-result", rtError(err)); });
  });
  el("rt-diff-load").addEventListener("click", function () {
    var sid = el("rt-diff-session").value.trim();
    if (!sid) {
      rtResult("rt-diff-result", '<div class="notice err"><div class="notice-body">' +
        "Session is required.</div></div>");
      return;
    }
    api("/api/runs/" + encodeURIComponent(sid) + "/diff")
      .then(function (timeline) {
        if (!timeline.baseline_recorded) {
          rtResult("rt-diff-result", '<div class="meta">No baseline recorded for this session.</div>');
          return;
        }
        var rows = (timeline.files || []).map(function (f) {
          return "<tr><td>" + esc(f.path) + "</td><td>" + esc(f.kind) + "</td>" +
            "<td>" + (f.preexisting ? "pre-existing" : "session") + "</td></tr>";
        }).join("");
        rtResult("rt-diff-result",
          '<div class="meta">baseline <code>' + esc((timeline.baseline_commit || "").slice(0, 12)) +
          "</code></div>" +
          '<div class="table-wrap"><table><thead><tr><th>Path</th><th>Kind</th><th>Whose</th>' +
          "</tr></thead><tbody>" + rows + "</tbody></table></div>");
      })
      .catch(function (err) { rtResult("rt-diff-result", rtError(err)); });
  });
  function recover(method) {
    var sid = el("rt-recover-session").value.trim();
    if (!sid) {
      rtResult("rt-recover-result", '<div class="notice err"><div class="notice-body">' +
        "Session is required.</div></div>");
      return;
    }
    api("/api/runs/" + encodeURIComponent(sid) + "/recover", { method: method })
      .then(function (report) {
        if (report.error) {
          rtResult("rt-recover-result", rtError({ message: report.error }));
          return;
        }
        var notes = (report.notes || []).map(function (n) {
          return "<div>" + esc(n) + "</div>";
        }).join("");
        rtResult("rt-recover-result",
          '<div class="meta">state: <strong>' + esc(report.state) + "</strong></div>" +
          '<div class="meta">resume: ' + esc(report.resume_session_id) + "</div>" + notes);
      })
      .catch(function (err) { rtResult("rt-recover-result", rtError(err)); });
  }
  el("rt-recover-classify").addEventListener("click", function () { recover("GET"); });
  el("rt-recover-run").addEventListener("click", function () { recover("POST"); });
}

/* --- providers and limits (F.2) -------------------------------------------------------------
 * One card per harness and per API provider. Every limit value shows its source and when it
 * was observed; one without a proved source says "unknown". Observed use is "through Garuda
 * only". Refresh re-runs the documented status reads and never sends a prompt. */

function pct(value) {
  return value === null || value === undefined ? "unknown" : Math.round(value * 100) + "%";
}

function resetText(epoch) {
  return epoch ? new Date(epoch * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "no reset time";
}

function useRowHtml(use) {
  return Object.keys(use.windows).map(function (name) {
    var w = use.windows[name];
    return "<tr><td>" + esc(name) + '</td><td class="num">' + esc(w.sessions) + '</td><td class="num">' + esc(w.native_calls) +
      '</td><td class="num">' + esc(w.acp_turns) + '</td><td class="num">' + esc(fmt.tokens(w.total_tokens)) +
      '</td><td class="num">' + (w.cost_usd === null ? "unknown" : esc(fmt.cost(w.cost_usd))) +
      (w.cost_unknown_records && w.cost_usd !== null ? " + " + esc(w.cost_unknown_records) + " unpriced" : "") + "</td></tr>";
  }).join("");
}

function limitsHtml(l) {
  if (l.status === "unknown") {
    return '<div class="limits" data-status="unknown"><span class="pill unknown">limits unknown</span> <span class="stat-sub">' +
           esc(l.reason || "") + "</span>" + notesHtml(l) + eventsHtml(l) + "</div>";
  }
  var windows = l.windows.map(function (w) {
    return "<li>" + esc(w.name) + (w.window_minutes ? " (" + esc(w.window_minutes) + " min)" : "") + ": " +
           esc(pct(w.used_fraction)) + " used · resets " + esc(resetText(w.resets_at)) + "</li>";
  }).join("");
  return '<div class="limits" data-status="' + esc(l.status) + '">' +
    '<span class="pill ' + (l.status === "known" ? "success" : "unknown") + '">' + esc(l.status) + "</span> " +
    '<span class="stat-sub">source ' + esc(l.source) + " · observed " + esc(Math.round(l.age_seconds)) + "s ago" +
    (l.status === "stale" ? " (older than a minute: not used for fallback)" : "") + "</span>" +
    "<ul>" + windows + "</ul>" + notesHtml(l) + eventsHtml(l) + "</div>";
}

function notesHtml(l) {
  return (l.notes || []).map(function (n) { return '<div class="stat-sub limit-note">' + esc(n) + "</div>"; }).join("");
}

function eventsHtml(l) {
  return (l.events || []).map(function (e) {
    var text = e.state === "historical" ? "limit reached — no reset time was reported (history only)"
             : e.state === "active" ? "limit reached — resets " + resetText(e.reset_at)
             : "limit reached — reset " + resetText(e.reset_at) + " has passed";
    return '<div class="limit-event" data-state="' + esc(e.state) + '"><span class="pill ' +
           (e.state === "active" ? "failed" : "unknown") + '">' + esc(e.state) + "</span> " + esc(text) + "</div>";
  }).join("");
}

function providerCardHtml(card) {
  var head = '<h2>' + esc(card.id) + ' <span class="stat-sub">' +
    (card.kind === "harness" ? esc(card.runtime_kind) + " harness · " + esc(card.version || "version unknown")
                             : "API provider · " + esc(card.models.join(", "))) + "</span></h2>";
  var body = "";
  if (card.kind === "harness") {
    var login = card.login.state;
    body += '<p><span class="pill ' + (login === "authenticated" ? "success" : (login === "logged_out" ? "failed" : "unknown")) +
            '" data-login="' + esc(login) + '">login: ' + esc(login.replace("_", " ")) + "</span> " +
            (card.available ? '<span class="pill success">available</span>' : '<span class="pill unknown">unavailable</span>') + "</p>";
    body += limitsHtml(card.limits);
  } else {
    body += '<p class="stat-sub">' + esc(card.rate_limits.status) + " — " + esc(card.rate_limits.reason) + "</p>";
  }
  body += '<p class="stat-sub">Observed use (' + esc(card.observed_use.label) + ")</p>" +
    '<table class="table"><thead><tr><th>Window</th><th>Sessions</th><th>Native calls</th><th>ACP turns</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>' +
    useRowHtml(card.observed_use) + "</tbody></table>";
  return '<div class="card provider-card" data-provider="' + esc(card.id) + '" data-kind="' + esc(card.kind) + '">' + head + body + "</div>";
}

function providersView() {
  render('<div class="loading">Loading providers…</div>');
  return api("/api/providers").then(function (payload) {
    var cards = payload.harnesses.concat(payload.providers);
    render(
      '<div class="page-head"><h1>Providers and limits</h1>' +
      '<button type="button" class="btn" id="providers-refresh"' + (canWrite() ? "" : " disabled") + ">Refresh</button>" +
      '<span class="meta" id="providers-note">' + (canWrite() ? "" : "read-only dashboard: refresh is off") + "</span></div>" +
      '<p class="stat-sub">Limits come only from sources Garuda has proved for the exact harness version; everything else reads unknown. ' +
      "Refresh re-runs the documented status reads and never sends a prompt.</p>" +
      '<div class="grid cols-2" id="provider-cards">' + (cards.length ? cards.map(providerCardHtml).join("") : '<div class="empty">No harnesses or providers yet.</div>') + "</div>"
    );
    el("providers-refresh").addEventListener("click", function () {
      el("providers-note").textContent = "refreshing…";
      api("/api/providers/refresh", { method: "POST", body: {} })
        .then(function () { return providersView(); })
        .catch(function (err) { el("providers-note").textContent = err.message; });
    });
  }).catch(function (err) {
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    render('<div class="notice err"><div class="notice-title">Could not load providers</div>' +
           '<div class="notice-body">' + esc(err.message) + "</div></div>");
  });
}
