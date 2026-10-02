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
      (row.background && isActive(state) && canAnswer()
        ? '<button type="button" class="btn" id="session-stop">Stop</button>' : "") +
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
      flowHtml(row.flow) +
      '<h2>Live events</h2><div id="live-events" class="stat-sub">connecting…</div>'
    );
    var stop = el("session-stop");
    if (stop) stop.addEventListener("click", function () {
      stop.disabled = true;
      api("/api/sessions/" + encodeURIComponent(row.session_id) + "/cancel", { method: "POST", body: {} })
        .then(function (r) { toast(r.result); return sessionDetailView(row.session_id); })
        .catch(function (err) { toast(err.message); stop.disabled = false; });
    });
    startSessionStream(row.session_id);
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
      answerInboxApproval(item, button.getAttribute("data-act") === "allow");
    });
  }).catch(function (err) {
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    render('<div class="notice err"><div class="notice-title">Could not load approvals</div>' +
           '<div class="notice-body">' + esc(err.message) + "</div></div>");
  });
}

function answerInboxApproval(item, allow) {
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

function isActive(state) { return ["queued", "working", "waiting"].indexOf(state.work) >= 0; }

/* --- the live stream (D.3), read with fetch so the token header can be sent ------------
 * Reconnects with the id of the last frame it saw, which is a byte offset into the session's
 * log, so nothing repeats and nothing is skipped. */

function streamSession(id, options) {
  options = options || {};
  var handle = {
    lastId: options.lastId || "0", events: [], ended: false, stopped: false, connections: 0,
    stop: function () { handle.stopped = true; if (handle._abort) handle._abort.abort(); }
  };
  function connect() {
    if (handle.stopped || handle.ended) return;
    handle.connections += 1;
    handle._abort = new AbortController();
    fetch("/api/sessions/" + encodeURIComponent(id) + "/stream", {
      headers: { "X-Garuda-Token": TOKEN, "Last-Event-ID": handle.lastId },
      signal: handle._abort.signal
    }).then(function (response) {
      if (!response.ok || !response.body) throw new Error("stream " + response.status);
      var reader = response.body.getReader(), decoder = new TextDecoder(), buffer = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) return;
          buffer += decoder.decode(chunk.value, { stream: true });
          var blocks = buffer.split("\n\n");
          buffer = blocks.pop();
          blocks.forEach(function (block) {
            var frame = {};
            block.split("\n").forEach(function (line) {
              var at = line.indexOf(": ");
              if (at > 0) frame[line.slice(0, at)] = line.slice(at + 2);
            });
            if (frame.id) handle.lastId = frame.id;
            if (frame.event === "end") { handle.ended = true; }
            else if (frame.event === "event") {
              var data = JSON.parse(frame.data);
              handle.events.push(data);
              if (options.onEvent) options.onEvent(data, handle);
            }
          });
          return pump();
        });
      }
      return pump();
    }).catch(function () { /* dropped: reconnect below */ }).then(function () {
      if (!handle.stopped && !handle.ended) setTimeout(connect, options.retryMs || 1000);
      if (options.onClose) options.onClose(handle);
    });
  }
  connect();
  return handle;
}

function startSessionStream(id) {
  stopSessionStream();
  var panel = el("live-events");
  STATE.sessionStream = streamSession(id, {
    onEvent: function (event, handle) {
      if (!panel) return;
      panel.textContent = handle.events.length + " events · last: " + (event.type || "?");
    },
    onClose: function (handle) {
      if (panel && handle.ended) panel.textContent = handle.events.length + " events · finished";
    }
  });
}

function stopSessionStream() {
  if (STATE.sessionStream) { STATE.sessionStream.stop(); STATE.sessionStream = null; }
}

/* --- the conversation panel (F.1) -------------------------------------------------------
 * Models used, grouped by work type, harness and model; the selected model kept apart from
 * what was reported; sessions this one tagged, was tagged by, resumed from. Every string is
 * escaped. "not reported" is shown as such and never replaced by the selected model. */

function conversationPanelHtml(c) {
  var used = c.models_used;
  var rows = used.rows.map(function (r) {
    return '<tr class="model-row" data-work="' + esc(r.work_type) + '" data-model="' + esc(r.model) + '">' +
      "<td>" + esc(r.work_type) + "</td><td>" + esc(r.harness) + "</td>" +
      "<td>" + (r.model === "not reported" ? '<span class="pill unknown">not reported</span>' : esc(r.model)) + "</td>" +
      '<td class="num">' + esc(r.calls) + "</td><td class=\"num\">" + esc(r.turns) + "</td>" +
      '<td class="num">' + esc(fmt.tokens(r.total_tokens)) + "</td>" +
      '<td class="num">' + (r.cost_unknown ? "unknown" : esc(fmt.cost(r.cost_usd))) +
      (r.cost_unknown && r.cost_usd ? " + " + esc(fmt.cost(r.cost_usd)) + " known" : "") + "</td></tr>";
  }).join("");
  function links(title, list, id) {
    if (!list.length) return "";
    return '<div id="' + id + '"><strong>' + title + "</strong> " + list.map(function (s) {
      return '<a href="#/sessions/' + encodeURIComponent(s.session_id) + '">' +
             esc(s.name || s.session_id.slice(0, 8)) + "</a>" +
             (s.cross_project ? ' <span class="pill unknown">other project</span>' : "");
    }).join(", ") + "</div>";
  }
  return (
    '<div class="card" id="conversation-panel"><h2>Models used</h2>' +
    (used.label ? '<p class="stat-sub" id="models-label">' + esc(used.label) + "</p>" : "") +
    '<p class="stat-sub">Selected: ' + esc((c.selected.harness || c.session.runtime) + " · " + (c.selected.model_id || "—")) +
    ". Reported attribution is shown separately; ACP turns are not call counts.</p>" +
    (rows ? '<div class="table-wrap"><table class="table" id="models-table"><thead><tr><th>Work</th><th>Harness</th><th>Model</th>' +
            "<th>Calls</th><th>ACP turns</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>" + rows + "</tbody></table></div>"
          : '<p class="stat-sub">No model calls recorded.</p>') +
    (used.snapshots.length ? '<p class="stat-sub" id="snapshots-note">Context snapshots (occupancy, not billable usage): ' +
      used.snapshots.map(function (s) { return esc((s.context_used === null ? "?" : s.context_used) + "/" + (s.context_size === null ? "?" : s.context_size)); }).join(", ") + "</p>" : "") +
    (c.lanes.length ? '<p class="stat-sub" id="lanes-note">Lanes: ' + c.lanes.map(function (l) { return esc(l.runtime_id + " (" + l.kind + ")"); }).join(" → ") + "</p>" : "") +
    links("Resumed from", c.links.resumed_from, "link-resumed-from") +
    links("Continued by", c.links.resumed_into, "link-resumed-into") +
    links("Tagged", c.links.tagged, "link-tagged") +
    links("Tagged by", c.links.tagged_by, "link-tagged-by") +
    "</div>"
  );
}

function loadConversationPanel(sessionId) {
  var host = el("conversation-host");
  if (!host) return;
  api("/api/sessions/" + encodeURIComponent(sessionId) + "/conversation").then(function (c) {
    if (el("conversation-host") === host) host.innerHTML = conversationPanelHtml(c);
  }).catch(function () { host.innerHTML = ""; });
}

/* --- setup (F.4): read-only ---------------------------------------------------------------
 * What is configured and what is wrong. Fix commands are shown as text with a Copy button;
 * nothing here edits a file or starts anything. */

function copyButtonHtml(text) {
  return '<button type="button" class="btn copy-fix" data-copy="' + esc(text) + '">Copy</button>';
}

function setupView() {
  render('<div class="loading">Loading setup…</div>');
  return api("/api/setup").then(function (s) {
    var diagnostics = s.diagnostics.map(function (d) {
      return '<li class="diag" data-code="' + esc(d.code) + '" data-level="' + esc(d.level) + '">' +
        '<span class="pill ' + (d.level === "error" ? "failed" : (d.level === "warning" ? "running" : "unknown")) + '">' + esc(d.level) + "</span> " +
        "<strong>" + esc(d.code) + "</strong> " + esc(d.message) +
        (d.fix ? '<div class="stat-sub">Fix: <code>' + esc(d.fix) + "</code> " + copyButtonHtml(d.fix) + "</div>" : "") + "</li>";
    }).join("");
    var roles = s.roles.map(function (r) {
      return '<tr class="role-row" data-role="' + esc(r.role) + '"><td>' + esc(r.role) + "</td><td>" + esc(r.harness) + "</td><td>" +
        esc(r.model_id || "—") + "</td><td>" + esc(r.effort || "—") + "</td><td>" +
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
      '<div class="card"><h2>Diagnostics</h2><ul class="diagnostics" id="setup-diagnostics">' + diagnostics + "</ul></div>" +
      '<div class="card"><h2>Roles</h2>' + (roles ? '<div class="table-wrap"><table class="table" id="roles-table"><thead><tr><th>Role</th><th>Harness</th><th>Model</th><th>Effort</th><th>Fallback chain</th><th>From</th></tr></thead><tbody>' + roles + "</tbody></table></div>"
        : '<p class="stat-sub">No roles. <code>garuda init</code> proposes some.</p>') + "</div>" +
      '<div class="card"><h2>Flows</h2><div class="table-wrap"><table class="table" id="flows-table"><thead><tr><th>Flow</th><th>From</th><th>Steps</th><th>Roles</th><th>Missing</th></tr></thead><tbody>' + flows + "</tbody></table></div></div>" +
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
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    render('<div class="notice err"><div class="notice-title">Could not load setup</div><div class="notice-body">' + esc(err.message) + "</div></div>");
  });
}
