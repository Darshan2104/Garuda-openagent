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
