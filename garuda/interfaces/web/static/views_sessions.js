/* Sessions and flows: the shared read model (D.3), rendered (D.4).
 *
 * Everything shown here comes from /api/sessions*, the same rows `garuda sessions --json`
 * prints; nothing is computed in the browser. Unknown stays visibly unknown: usage and cost
 * read "unknown" until the usage ledger exists, a queue that could not be read says so, and
 * a review outcome is its own badge — never the verification one. */

"use strict";

function chip(label, value, cls) {
  return '<span class="chip ' + (cls || "chip-off") + '" data-chip="' + esc(label) + '">' +
         esc(label) + ": " + esc(value === null || value === undefined ? "unknown" : value) +
         "</span>";
}

function outcomeClass(value) {
  if (value === "completed") return "chip-on";
  if (value === "failed" || value === "cancelled" || value === "refused") return "chip-bad";
  return "chip-off";
}

function verificationChip(v) {
  v = v || {};
  var text = v.status || "unknown";
  if (v.authority && (v.status === "passed" || v.status === "failed")) text += " (" + v.authority + ")";
  return chip("verification", text, v.status === "passed" ? "chip-on" : (v.status === "failed" ? "chip-bad" : "chip-off"));
}

function identityText(row) {
  var parts = [row.runtime || "native"];
  if (row.role && row.role.role) parts.push("role " + row.role.role);
  if (row.model) parts.push(row.model);
  return parts.join(" · ");
}

function queueText(queue) {
  if (!queue) return "—";
  if (queue.state === "queued") return "queued #" + queue.position;
  if (queue.state === "running") return "has its slot";
  return queue.state || "unknown";
}

function sessionRowHtml(row) {
  var state = row.state || {};
  return (
    '<tr class="session-row" data-session="' + esc(row.session_id) + '" data-label="' + esc(row.label) + '">' +
    '<td><a href="#/sessions/' + encodeURIComponent(row.session_id) + '">' +
    esc(row.name || row.session_id.slice(0, 8)) + "</a>" +
    (row.kind === "flow" ? ' <span class="pill unknown">flow</span>' : "") + "</td>" +
    '<td><span class="pill ' + (row.crashed ? "failed" : (state.work === "working" ? "running" : "unknown")) + '">' + esc(row.label) + "</span></td>" +
    "<td>" + esc(identityText(row)) + "</td>" +
    "<td>" + chip("outcome", state.outcome || "—", outcomeClass(state.outcome)) + " " + verificationChip(row.verification) + "</td>" +
    "<td>" + esc(queueText(row.queue)) + "</td>" +
    "<td>" + esc(row.workspace ? fmt.truncate(row.workspace, 36) : "—") + (row.branch ? " @ " + esc(row.branch) : "") + "</td>" +
    '<td class="num">' + esc(row.usage) + " / " + esc(row.cost) + "</td>" +
    '<td class="num">' + (row.approvals_pending ? '<span class="pill running">' + row.approvals_pending + " waiting</span>" : "—") + "</td>" +
    "<td>" + esc(fmt.when(row.updated_at)) + "</td>" +
    "</tr>"
  );
}

function sessionsView() {
  render('<div class="loading">Loading sessions…</div>');
  return api("/api/sessions").then(function (payload) {
    var rows = payload.sessions;
    if (!rows.length) {
      render('<div class="empty"><p><strong>No sessions yet.</strong></p>' +
             "<p>Start one with <code>garuda run --bg -t &quot;your task&quot;</code>.</p></div>");
      return;
    }
    render(
      '<div class="page-head"><h1>Sessions</h1><span class="meta">' + rows.length + "</span>" +
      '<button type="button" class="btn" id="sessions-refresh">Refresh</button></div>' +
      '<div class="table-wrap"><table class="table" id="sessions-table"><thead><tr>' +
      "<th>Session</th><th>State</th><th>Runtime · role · model</th><th>Outcome / verification</th>" +
      "<th>Queue</th><th>Workspace</th><th>Usage / cost</th><th>Approvals</th><th>Updated</th>" +
      "</tr></thead><tbody>" + rows.map(sessionRowHtml).join("") + "</tbody></table></div>"
    );
    el("sessions-refresh").addEventListener("click", sessionsView);
  }).catch(function (err) {
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    render('<div class="notice err"><div class="notice-title">Could not load sessions</div>' +
           '<div class="notice-body">' + esc(err.message) + "</div></div>");
  });
}

function artifactText(ref) {
  return ref.type + " from " + ref.producer_step + " (" + String(ref.digest || "").slice(0, 8) + ")";
}

function attemptHtml(attempt) {
  var delta = attempt.delta === null ? "unknown" : (attempt.delta ? "changed" : "unchanged");
  return (
    '<li class="attempt" data-attempt="' + esc(attempt.attempt) + '" data-status="' + esc(attempt.status) + '">' +
    "attempt " + esc(attempt.attempt) + (attempt.role ? " · " + esc(attempt.role) : "") +
    " " + chip("status", attempt.status, attempt.status === "done" ? "chip-on" : "chip-bad") +
    " " + chip("workspace", delta, "chip-off") +
    (attempt.no_edits ? " " + chip("no-edits", attempt.no_edits, attempt.no_edits === "unchanged" ? "chip-on" : "chip-bad") : "") +
    (attempt.session_id ? ' · <a href="#/sessions/' + encodeURIComponent(attempt.session_id) + '">session</a>' : "") +
    (attempt.stop ? '<div class="stat-sub">stopped: ' + esc(attempt.stop.code) + "</div>" : "") +
    (attempt.inputs.length ? '<div class="stat-sub">in: ' + attempt.inputs.map(function (r) { return esc(artifactText(r)); }).join(", ") + "</div>" : "") +
    (attempt.outputs.length ? '<div class="stat-sub">out: ' + attempt.outputs.map(function (r) { return esc(artifactText(r)); }).join(", ") + "</div>" : "") +
    (attempt.members.length ? '<div class="stat-sub">reviewers: ' + attempt.members.map(function (m) { return esc(m.role) + (m.success ? " ✓" : " ✗"); }).join(", ") + "</div>" : "") +
    "</li>"
  );
}

function flowHtml(flow) {
  if (!flow) return "";
  var review = flow.review;
  return (
    '<section class="flow" id="flow-detail"><h2>Flow ' + esc(flow.name) + " " + chip("flow", flow.state, "chip-off") + "</h2>" +
    // A review is advice about the work. It has its own badge and never says "verified".
    (review ? '<p id="flow-review">' + chip("review", review.verdict || review.outcome || "recorded", "chip-off") +
              ' <span class="stat-sub">a review is not verification</span></p>' : "") +
    '<ol class="steps">' + flow.steps.map(function (step) {
      return '<li class="step" data-step="' + esc(step.id) + '"><strong>' + esc(step.id) + "</strong>" +
             (step.attempts.length ? '<ul class="attempts">' + step.attempts.map(attemptHtml).join("") + "</ul>"
                                   : ' <span class="stat-sub">not run yet</span>') + "</li>";
    }).join("") + "</ol>" +
    (flow.edges.length ? '<h3>Artifact edges</h3><ul id="flow-edges">' + flow.edges.map(function (e) {
      return "<li>" + esc(e.from_step) + " → " + esc(e.to_step) + " · " + esc(e.type) + "</li>";
    }).join("") + "</ul>" : "") + "</section>"
  );
}

function sessionDetailView(id) {
  render('<div class="loading">Loading session…</div>');
  return api("/api/sessions/" + encodeURIComponent(id)).then(function (row) {
    var state = row.state || {};
    render(
      '<div class="page-head"><h1>' + esc(row.name || row.session_id.slice(0, 8)) + "</h1>" +
      '<span class="pill unknown" id="session-label">' + esc(row.label) + "</span>" +
      '<a class="btn" href="#/runs/' + encodeURIComponent(row.session_id) + '">Trace</a>' +
      '<a class="btn" href="#/sessions">All sessions</a></div>' +
      '<div class="chips" id="session-facts">' +
      chip("process", state.process) + chip("work", state.work) +
      chip("outcome", state.outcome || "—", outcomeClass(state.outcome)) +
      verificationChip(row.verification) +
      chip("queue", queueText(row.queue)) + chip("usage", row.usage) + chip("cost", row.cost) +
      "</div>" +
      '<p class="stat-sub">' + esc(identityText(row)) + " · " + esc(row.workspace || "") +
      (row.branch ? " @ " + esc(row.branch) : "") + (row.isolation !== "shared" ? " (" + esc(row.isolation) + ")" : "") + "</p>" +
      (row.approvals.length ? '<h2>Waiting for you</h2><ul id="session-approvals">' + row.approvals.map(function (a) {
        return "<li><code>" + esc(a.action) + '</code> <span class="stat-sub">' + esc(a.family) + " · ceiling " + esc(a.ceiling) + "</span></li>";
      }).join("") + "</ul>" : "") +
      flowHtml(row.flow)
    );
  }).catch(function (err) {
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    render('<div class="notice err"><div class="notice-title">Could not load the session</div>' +
           '<div class="notice-body">' + esc(err.message) + "</div></div>");
  });
}

/* --- the approval inbox (D.5) ---------------------------------------------
 * Lists what is waiting across sessions and sends the person's answer, bound to the request
 * digest this page was shown, to the session's file channel. It never answers a runtime: the
 * broker validates and decides, so "answer recorded" is not "allowed". */

function canAnswer() {
  return !!(STATE.health && STATE.health.mode === "read-write");
}

function approvalRowHtml(a) {
  var buttons = canAnswer() && !a.expired
    ? '<button type="button" class="btn btn-primary" data-act="allow">Approve</button> ' +
      '<button type="button" class="btn" data-act="deny">Deny</button>'
    : '<span class="stat-sub">' + (a.expired ? "expired — it will be denied" : "read-only dashboard") + "</span>";
  return (
    '<li class="approval-item" data-session="' + esc(a.session_id) + '" data-approval="' + esc(a.approval_id) +
    '" data-digest="' + esc(a.digest) + '">' +
    "<div><strong>" + esc(a.name || a.session_id.slice(0, 8)) + "</strong> " +
    '<span class="stat-sub">' + esc(a.family) + " · ceiling " + esc(a.ceiling) + "</span></div>" +
    "<pre>" + esc(a.action) + "</pre>" +
    '<div class="approval-actions">' + buttons + ' <span class="stat-sub approval-result"></span></div></li>'
  );
}

function inboxView() {
  render('<div class="loading">Loading approvals…</div>');
  return api("/api/inbox").then(function (payload) {
    var items = payload.approvals;
    render(
      '<div class="page-head"><h1>Approvals</h1><span class="meta">' + items.length + " waiting</span>" +
      '<button type="button" class="btn" id="inbox-refresh">Refresh</button></div>' +
      (items.length ? '<ul class="approvals" id="inbox-list">' + items.map(approvalRowHtml).join("") + "</ul>"
                    : '<div class="empty" id="inbox-empty"><p>Nothing is waiting for you.</p></div>')
    );
    el("inbox-refresh").addEventListener("click", inboxView);
    var list = el("inbox-list");
    if (!list) return;
    list.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-act]");
      if (!button) return;
      var item = button.closest(".approval-item");
      answerApproval(item, button.getAttribute("data-act") === "allow");
    });
  }).catch(function (err) {
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    render('<div class="notice err"><div class="notice-title">Could not load approvals</div>' +
           '<div class="notice-body">' + esc(err.message) + "</div></div>");
  });
}

function answerApproval(item, allow) {
  var result = item.querySelector(".approval-result");
  var buttons = item.querySelectorAll("button");
  buttons.forEach(function (b) { b.disabled = true; });
  return api("/api/sessions/" + encodeURIComponent(item.getAttribute("data-session")) +
             "/approvals/" + encodeURIComponent(item.getAttribute("data-approval")), {
    method: "POST", body: { allow: allow, digest: item.getAttribute("data-digest") }
  }).then(function () {
    result.textContent = "answer recorded — the broker decides";
    item.setAttribute("data-state", "recorded");
  }).catch(function (err) {
    // Stale page, an earlier answer, an expired request: say so and let them reload.
    result.textContent = err.message + " (" + (err.code || err.status) + ")";
    item.setAttribute("data-state", err.code || "error");
  });
}
