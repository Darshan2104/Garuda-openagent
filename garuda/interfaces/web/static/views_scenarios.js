/* Starters: pure previews plus server-owned flow operations when supported. */
"use strict";

function stopStarterViews() {
  if (STATE.starterView && STATE.starterView.operationTimer) {
    clearTimeout(STATE.starterView.operationTimer);
  }
  STATE.starterView = null; STATE.starterResult = null;
}

function starterErrorHtml(err) {
  return '<div class="notice err"><div class="notice-title">' + esc(err.code || "Could not load starter") +
    '</div><div class="notice-body">' + esc(err.message) + "</div></div>";
}

function starterBindingsHtml(bindings, recorded) {
  var rows = recorded ? bindings || [] : Object.keys(bindings || {}).map(function (name) {
    return Object.assign({ role: name }, bindings[name]);
  });
  if (!rows.length) return '<p class="stat-sub">Role identities are unknown.</p>';
  return '<div class="table-wrap"><table class="starter-bindings table-stack"><thead><tr><th>Role / step</th><th>' +
    (recorded ? "Recorded" : "Configured") + ' runtime</th><th>Model</th><th>' +
    (recorded ? "Evidence" : "Role permissions / write policy") + '</th></tr></thead><tbody>' + rows.map(function (r) {
      return '<tr><td>' + esc(r.role || r.name || r.step || "unknown") +
        (r.attempt ? " · attempt " + esc(r.attempt) : "") + '</td><td>' + esc(r.runtime_id || "unknown") +
        '</td><td>' + esc(r.model_id || (recorded ? "not reported" : "harness default")) + '</td><td>' +
        esc(recorded ? r.evidence : (r.permissions || "inherited / unspecified") + " / " + (r.write_policy || "unknown")) +
        "</td></tr>";
    }).join("") + "</tbody></table></div>";
}

function starterReadinessHtml(state) {
  state = state || {};
  return '<p class="starter-status">' + chip("readiness", state.status || "not checked", state.can_run ? "chip-on" : "chip-off") +
    " " + chip("review", state.review_label || "unknown") + '</p><p class="stat-sub">Configured preflight; runtime login, version and observed capabilities are not proven by this preview.</p>' +
    ((state.diagnostics || []).length ? '<ul class="starter-diagnostics">' + state.diagnostics.map(function (d) {
      return '<li data-diagnostic="' + esc(d.code) + '">' + esc(d.message) + '<br><span class="stat-sub">' + esc(d.fix) + "</span></li>";
    }).join("") + "</ul>" : "") +
    ((state.remedies || []).length ? '<ol class="starter-remedies">' + state.remedies.map(function (r) {
      return '<li><strong>' + esc(r.title) + '</strong> — ' + esc(r.description) +
        (r.command ? '<pre class="starter-command">' + esc(r.command) + "</pre>" : "") + "</li>";
    }).join("") + "</ol>" : "") +
    '<details><summary>Configured roles and runtime evidence</summary>' + starterBindingsHtml(state.bindings, false) +
    '<pre class="starter-data">' + esc(JSON.stringify(state.runtime_evidence || {}, null, 2)) + "</pre></details>";
}

function starterWorkspaceHtml(selected) {
  var rows = (STATE.config && STATE.config.workspaces) || [];
  var current = rows.filter(function (r) { return r.index === selected; })[0];
  // A native select cannot wrap: each option shows the end of its path (the part that tells
  // two workspaces apart) and carries the whole path as its title, as does the select.
  return '<label class="starter-workspace">Workspace <select id="starter-workspace"' +
    (current ? ' title="' + esc(current.path) + '"' : "") + ">" + rows.map(function (r) {
    return '<option value="' + esc(r.index) + '" title="' + esc(r.path) + '"' + (r.index === selected ? " selected" : "") + '>' +
      esc(fmt.tail(r.path, 48)) + "</option>";
  }).join("") + "</select></label>";
}

function startersView(starterId, query) {
  var params = new URLSearchParams(query || "");
  var workspace = Number(params.get("workspace") || 0);
  var state = { id: starterId, workspace: workspace, revision: 0, preview: null };
  STATE.starterView = state;
  render('<div class="loading">Loading starters…</div>');
  var suffix = "?workspace=" + encodeURIComponent(workspace);
  return Promise.all([api("/api/scenarios" + suffix), starterId ? api("/api/scenarios/" + encodeURIComponent(starterId) + suffix) : Promise.resolve(null)])
    .then(function (both) {
      if (STATE.starterView !== state) return;
      var library = both[0];
      state.entry = both[1];
      state.roles = {};
      library.starters.forEach(function (r) { Object.keys(r.readiness.bindings || {}).forEach(function (role) { state.roles[role] = true; }); });
      render('<div class="page-head"><h1>Starters</h1>' + starterWorkspaceHtml(workspace) +
        '<a class="btn" href="#/sessions">Follow a run</a></div><p>Choose a goal and preview it. Supported starters can start here; every starter keeps its equivalent command.</p>' +
        '<p class="starter-context">Workspace: <code>' + esc(library.workspace) + '</code></p>' +
        (state.entry ? '<a href="#/starters?workspace=' + workspace + '">← All starters</a>' + starterFormHtml(state) :
          '<div class="grid starter-grid" id="starter-cards">' + library.starters.map(function (r) {
            return '<section class="card" data-starter="' + esc(r.id) + '"><h2>' + esc(r.title) + '</h2>' + starterReadinessHtml(r.readiness) +
              '<p class="stat-sub">' + esc(r.launch.kind !== "flow" ? "Verification comes from acceptance receipts" : r.id === "build-review" ? "Verification comes from acceptance receipts at flow finalization, when checks are configured" : "Flow checks unavailable for this flow") +
              '</p><a class="btn" href="#/starters/' + encodeURIComponent(r.id) + '?workspace=' + workspace + '">Choose starter</a></section>';
          }).join("") + "</div>")
      );
      el("starter-workspace").addEventListener("change", function () {
        var next = new URLSearchParams(query || "");
        next.set("workspace", this.value);
        stopStarterViews();
        render('<div class="loading">Switching workspace…</div>');
        location.hash = "#/starters" + (starterId ? "/" + encodeURIComponent(starterId) : "") + "?" + next.toString();
      });
      if (state.entry) {
        wireStarterForm(state);
        var initial = {};
        ["variant", "plan_artifact"].forEach(function (key) { if (params.has(key)) initial[key] = params.get(key); });
        applyStarterInputs(state, initial);
      }
    }).catch(function (err) {
      if (isSuperseded(err)) return;
      if (STATE.starterView === state) render(err.status === 401 ? tokenRequiredPanel() : starterErrorHtml(err));
    });
}

function starterFieldHtml(name, field, entry) {
  var label = esc(field.label || name) + (field.required ? " *" : "");
  if (field.type === "execution-options") {
    return '<fieldset class="starter-options"><legend>Run options</legend>' + (entry.launch.options || []).map(function (option) {
      if (option === "bg") return '<label><input type="checkbox" data-option="bg"> Queue in background</label>';
      if (option === "isolation") return '<label>Workspace isolation <select data-option="isolation"><option value="">Use run default</option><option value="shared">shared</option><option value="worktree">worktree (uncommitted edits are not copied)</option><option value="auto">auto</option></select></label>';
      if (option === "checks") return '<label>Explicit checks <textarea data-option="checks" rows="3" placeholder="One command per line"></textarea></label>';
      return '<label>Session name <input type="text" data-option="name"></label>';
    }).join("") + "</fieldset>";
  }
  if (name === "variant") {
    return '<label>' + label + ' <select data-field="variant">' + Object.keys(entry.launch.variants || {}).map(function (v) {
      return '<option value="' + esc(v) + '">' + esc(v) + "</option>";
    }).join("") + "</select></label>";
  }
  if (name === "role") return '<label>' + label + ' <input type="text" data-field="role" list="starter-roles" placeholder="' + esc(entry.launch.role) + '"></label>';
  return '<label>' + label + ' <textarea data-field="' + esc(name) + '" rows="' + (field.type === "source-refs" ? 3 : 2) + '"' +
    (field.type === "source-refs" ? ' placeholder="One source per line: repo/path#section or session:NAME"' : "") + '></textarea></label>';
}

function starterFormHtml(state) {
  var entry = state.entry;
  return '<section class="card starter-form"><h2>' + esc(entry.title) + '</h2>' + starterReadinessHtml(entry.readiness) +
    '<form id="starter-form" novalidate>' + Object.keys(entry.fields).map(function (name) { return starterFieldHtml(name, entry.fields[name], entry); }).join("") +
    '<datalist id="starter-roles">' + Object.keys(state.roles).map(function (r) { return '<option value="' + esc(r) + '"></option>'; }).join("") + '</datalist>' +
    '<div class="row"><button type="submit" class="btn btn-primary" id="starter-preview-button">Preview command</button>' +
    '<button type="button" class="btn" id="starter-example">Load reconnect example</button></div></form>' +
    (entry.id === "build-review" ? '<div class="starter-build-check"><h3>Build and check · no review</h3><p>Use the current goal and constraints with the coder role instead of an independent review.</p><label>Explicit checks <textarea id="starter-remedy-checks" rows="2" placeholder="One command per line"></textarea></label><button type="button" class="btn" id="starter-build-check">Preview Build and check</button></div>' : "") +
    '<div class="starter-preview" id="starter-preview" aria-live="polite"><p class="stat-sub">Preview starts nothing. Preview again after sources or configuration change.</p></div>' +
    '<div id="starter-operation" aria-live="polite"></div></section>';
}

function starterInputs(state) {
  var values = {}, options = {};
  el("starter-form").querySelectorAll("[data-field]").forEach(function (node) {
    var name = node.getAttribute("data-field");
    if (!node.value) return;
    values[name] = state.entry.fields[name].type === "source-refs" ? node.value.split(/\r?\n/).filter(function (v) { return v !== ""; }) : node.value;
  });
  el("starter-form").querySelectorAll("[data-option]").forEach(function (node) {
    var name = node.getAttribute("data-option");
    if (name === "bg") { if (node.checked) options.bg = true; }
    else if (node.value) options[name] = name === "checks" ? node.value.split(/\r?\n/).filter(function (v) { return /\S/.test(v); }) : node.value;
  });
  if (Object.keys(options).length) values.options = options;
  return values;
}

function invalidateStarterPreview(state) {
  state.revision++;
  state.preview = null;
  if (state.operation && state.operation.done) {
    if (state.operationTimer) clearTimeout(state.operationTimer);
    state.operation = null;
    var operation = el("starter-operation");
    if (operation) operation.innerHTML = "";
  }
  var target = el("starter-preview");
  if (target) target.innerHTML = '<p class="stat-sub">Inputs changed. Preview again before copying a command or starting a new operation.</p>';
}

function applyStarterInputs(state, values) {
  var form = el("starter-form");
  form.querySelectorAll("[data-field]").forEach(function (node) {
    var key = node.getAttribute("data-field");
    if (!Object.prototype.hasOwnProperty.call(values, key)) return;
    var value = Array.isArray(values[key]) ? values[key].join("\n") : values[key];
    if (node.tagName === "SELECT" && !Array.from(node.options).some(function (o) { return o.value === value; })) throw new Error("Unknown starter variant.");
    node.value = value;
  });
  form.querySelectorAll("[data-option]").forEach(function (node) {
    var key = node.getAttribute("data-option"), value = (values.options || {})[key];
    if (value === undefined) return;
    if (key === "bg") node.checked = value;
    else node.value = Array.isArray(value) ? value.join("\n") : value;
  });
  invalidateStarterPreview(state);
}

function wireStarterForm(state) {
  var form = el("starter-form");
  form.addEventListener("input", function () { invalidateStarterPreview(state); });
  form.addEventListener("change", function () { invalidateStarterPreview(state); });
  form.addEventListener("submit", function (event) { event.preventDefault(); requestStarterPreview(state, false); });
  el("starter-example").addEventListener("click", function () { form.reset(); applyStarterInputs(state, state.entry.example_inputs); });
  var remedy = el("starter-build-check");
  if (remedy) {
    remedy.addEventListener("click", function () { requestStarterPreview(state, true); });
    el("starter-remedy-checks").addEventListener("input", function () { invalidateStarterPreview(state); });
  }
}

function requestStarterPreview(state, remedy) {
  if (state.operation && state.operation.done) {
    if (state.operationTimer) clearTimeout(state.operationTimer);
    state.operation = null;
    el("starter-operation").innerHTML = "";
  }
  var revision = ++state.revision;
  state.preview = null;
  var body = { starter_id: state.id, inputs: starterInputs(state), workspace: state.workspace };
  if (remedy) {
    body.remedy = "build-and-check";
    body.checks = el("starter-remedy-checks").value.split(/\r?\n/).filter(function (v) { return /\S/.test(v); });
  }
  var submitted = submittedViewInputs(el("starter-form"));
  var submittedRemedy = remedy ? submittedViewInputs(el("starter-remedy-checks").parentElement) : function () {};
  el("starter-preview").innerHTML = '<p class="loading">Compiling preview…</p>';
  return api("/api/scenarios/preview", { method: "POST", body: body }).then(function (data) {
    if (STATE.starterView !== state || revision !== state.revision) return;
    submitted();
    submittedRemedy();
    state.preview = data;
    state.previewInputs = body.inputs;
    el("starter-preview").innerHTML = starterPreviewHtml(data, state);
    el("starter-copy").addEventListener("click", function () { copyStarterCommand(el("starter-command")); });
    var start = el("starter-start");
    if (start) start.addEventListener("click", function () { requestStarterStart(state); });
  }).catch(function (err) {
    if (isSuperseded(err)) return;
    if (STATE.starterView === state && revision === state.revision) el("starter-preview").innerHTML = starterErrorHtml(err);
  });
}

function copyStarterCommand(node) {
  navigator.clipboard.writeText(node.value).then(function () { toast("Command copied. Run it in your terminal."); }).catch(function () {
    node.focus(); node.select(); toast("Select and copy the command from the text box.");
  });
}

function starterPreviewHtml(data, state) {
  var plan = data.plan;
  var authority = data.authority || {};
  var canStart = (!state.operation || state.operation.done) &&
    (plan.kind === "flow" || plan.kind === "run") &&
    authority.can_start && data.readiness.can_run &&
    STATE.health && STATE.health.capabilities && STATE.health.capabilities.starter_start;
  var authorityHtml = authority.digest ?
    '<details><summary>Dashboard execution authority</summary><pre class="starter-data">' +
    esc(JSON.stringify(authority, null, 2)) + '</pre></details>' : '';
  return '<h3>Preview</h3>' + starterReadinessHtml(data.readiness) +
    '<p>' + chip("launch", plan.kind + " / " + plan.target) + " " + chip("verification", plan.provenance.verification) + " " + chip("cost", plan.limits.cost) + '</p>' +
    '<p class="stat-sub">Effective flow source: ' + esc(plan.provenance.flow_source) + ' · configured role invocations ≤ ' + esc(plan.limits.max_role_invocations) +
    ' · review pairs ≤ ' + esc(plan.limits.review_pairs) + '. ' + esc(plan.limits.note || "") + '</p>' +
    '<details><summary>Supplied source provenance and requested step posture</summary><p>Named repository sources are references; this does not prove a runtime has read them.</p><pre class="starter-data">' +
    esc(JSON.stringify({ sources: plan.sources, checks: plan.checks, options: plan.options, flow: plan.flow, provenance: plan.provenance, bindings: plan.bindings, configuration: data.configuration }, null, 2)) + '</pre></details>' +
    '<label>Equivalent CLI command <textarea id="starter-command" readonly rows="6">' + esc(plan.equivalent_command) + '</textarea></label>' +
    '<button type="button" class="btn" id="starter-copy">Copy command</button>' +
    (canStart ? '<button type="button" class="btn btn-primary" id="starter-start">' +
      (plan.kind === "flow" ? "Start flow" : plan.provenance.no_edits ? "Ask role" : "Start run") + '</button>' : '') +
    '<p class="stat-sub">This equivalent command records an ordinary run. The CLI <code>garuda starter run</code> also records structured starter inputs and source provenance.</p>' +
    '<p class="stat-sub">Plan digest: <code>' + esc(plan.digest) + '</code>. Sources and configuration can change after preview; create a fresh preview when they do.</p>' + authorityHtml;
}

function requestStarterStart(state) {
  if (!state.preview || !state.preview.authority || !state.preview.authority.can_start) return;
  if (!state.operation) state.operation = { id: crypto.randomUUID() };
  var operation = state.operation;
  var button = el("starter-start");
  if (button) button.disabled = true;
  el("starter-operation").innerHTML = '<p class="loading">Durably admitting this operation…</p>';
  return api("/api/scenarios/start", { method: "POST", body: {
    operation_id: operation.id,
    starter_id: state.id,
    inputs: state.previewInputs,
    workspace: state.workspace,
    preview_digest: state.preview.plan.digest,
    authority_digest: state.preview.authority.digest
  }}).then(function (row) {
    // Its approvals stay alive from any page of this tab (views_flows.js watcher).
    if (typeof watchOperation === "function") watchOperation(operation.id);
    if (STATE.starterView !== state || state.operation !== operation) return;
    renderStarterOperation(state, row, []);
    pollStarterOperation(state);
  }).catch(function (err) {
    if (isSuperseded(err)) return;
    if (STATE.starterView !== state || state.operation !== operation) return;
    if (err.code === "starter.preview_changed" || err.code === "starter.authority_changed") {
      state.preview = null;
      state.operation = null;
    }
    el("starter-operation").innerHTML = starterErrorHtml(err) +
      '<button type="button" class="btn" id="starter-retry-preview">Create a fresh preview</button>';
    el("starter-retry-preview").addEventListener("click", function () { requestStarterPreview(state, false); });
    if (button) button.disabled = false;
  });
}

function scheduleStarterOperation(state, delay) {
  // One timer per view: an approval answer polls immediately and must not
  // start a second loop beside the pending one.
  if (state.operationTimer) clearTimeout(state.operationTimer);
  state.operationTimer = setTimeout(function () { pollStarterOperation(state); }, delay);
}

function pollStarterOperation(state) {
  var operation = state.operation;
  if (!operation || STATE.starterView !== state) return;
  if (state.operationTimer) clearTimeout(state.operationTimer);
  state.operationTimer = null;
  Promise.all([
    api("/api/scenario-operations/" + encodeURIComponent(operation.id)),
    api("/api/approvals?owner=" + encodeURIComponent(operation.id)).catch(function () { return { approvals: [] }; })
  ]).then(function (both) {
    if (STATE.starterView !== state || state.operation !== operation) return;
    renderStarterOperation(state, both[0], both[1].approvals || [], both[1].settled || []);
    if (OPERATION_SETTLED.indexOf(both[0].status) === -1) {
      scheduleStarterOperation(state, 900);
    }
  }).catch(function (err) {
    if (isSuperseded(err)) return;
    if (STATE.starterView !== state || state.operation !== operation) return;
    el("starter-operation").innerHTML = starterErrorHtml(err);
    // Polling also keeps waiting approvals alive, so a transient failure
    // retries; a definitive client error (scope changed, not found) stops.
    if (!err.status || err.status >= 500) scheduleStarterOperation(state, 2000);
  });
}

/* The operation phases after which nothing more happens. */
var OPERATION_SETTLED = ["terminal", "uncertain", "quarantined", "refused", "abandoned"];

function renderStarterOperation(state, row, approvals, settled) {
  var host = el("starter-operation");
  if (!host) return;
  if (OPERATION_SETTLED.indexOf(row.status) !== -1) {
    state.operation.done = true;
  }
  host.innerHTML = starterOperationHtml(row, approvals, "starter", null, null, settled);
  wireOperationApprovals(host, function () { pollStarterOperation(state); });
}

/* One owned operation and its approvals, shared by the Starters page ("starter"), a Flows
 * start waiting for admission ("flow") and the session page's operation panel ("session").
 * `grace` and `timeout` are the broker's heartbeat grace and answer timeout in seconds, when
 * known (/api/approvals publishes both); the defaults are the broker's own (#871). */
/* The asks of this operation already settled, and who settled each, from the broker's own
 * record (GET /api/approvals `settled`): "denied by you", "the dashboard stopped watching",
 * "nobody answered". The step's session records only allow or deny, so this is the one place
 * a person's decision is told apart from a timeout's or the reaper's. The dashboard keeps
 * these in memory, so a restart forgets them; it says so. */
function operationSettledHtml(settled) {
  if (!settled || !settled.length) return "";
  return '<div class="operation-settled" id="operation-settled"><h4>Answered</h4><ul>' + settled.map(function (s) {
    return '<li data-decided-by="' + esc(s.decided_by || "unknown") + '"><code>' + esc(fmt.truncate(s.action || "", 160)) +
      "</code> — " + esc(s.summary || "settled; who decided was not recorded") +
      (s.settled_at ? ' <span class="stat-sub">' + esc(fmt.when(s.settled_at)) + "</span>" : "") + "</li>";
  }).join("") + '</ul><p class="stat-sub">As this dashboard recorded it; it forgets these when it restarts.</p></div>';
}

/* The flow chip of an operation. A start a resume continued reports no flow state of its
 * own (`flow_state: null`), but it was admitted (it has a session): it read "not admitted". */
function operationFlowState(row) {
  if (row.flow_state) return row.flow_state;
  if (!row.session_id) return "not admitted";
  return row.resumed_by ? "continued by its resume" : "unknown";
}

function starterOperationHtml(row, approvals, where, grace, timeout, settled) {
  var session = row.session_id && where !== "session" ?
    '<a class="btn" href="#/sessions/' + encodeURIComponent(row.session_id) + '">Open session details</a>' : '';
  var keep = '. Answer its approvals here or on the <a href="#/inbox">Approvals</a> page, on any page of an open dashboard tab ' +
    '(also when a step runs in its own process). Each waits up to ' + esc(timeout || 300) + ' seconds unanswered, then it is denied; ' +
    'about ' + esc(grace || 90) + ' seconds after no dashboard tab is watching this run (its tabs were closed), a waiting approval ' +
    'is denied as "stopped watching" and the step sees the denial, while the run keeps going on the server. If the dashboard ' +
    'stops, a waiting approval is denied at once.</p>';
  var resume = row.kind === "resume";
  var follow = (resume ? '<p class="stat-sub" data-resumes="' + esc(row.resumes) + '">Resumes start operation <code>' + esc(row.resumes) + "</code>.</p>" : "") +
    (row.resumed_by ? '<p class="stat-sub" id="operation-resumed-by" data-resumed-by="' + esc(row.resumed_by) + '">Resumed by operation ' +
      (row.session_id ? '<a href="#/sessions/' + encodeURIComponent(row.session_id) + "?operation=" + encodeURIComponent(row.resumed_by) + '">' : "") +
      "<code>" + esc(row.resumed_by) + "</code>" + (row.session_id ? "</a>" : "") + "; this start's own outcome is shown here.</p>" : "");
  return '<section class="card starter-operation-card"' + (resume ? ' data-kind="resume"' : "") + '><h3>' +
    (resume ? "Dashboard resume" : where === "session" ? "Dashboard operation" : "Starter operation") + '</h3>' + follow + '<p>' +
    chip("operation", row.status || row.phase) + ' ' + chip("flow", operationFlowState(row)) +
    '</p><p class="stat-sub">Operation <code>' + esc(row.operation_id) + '</code>' +
    (row.session_id ? ' · session <code>' + esc(row.session_id) + '</code>' : '') +
    (row.background ? '. This run continues after the dashboard closes. Answer its durable requests in <a href="#/inbox">Approval inbox</a>.</p>' : keep) +
    (row.reason ? '<p class="notice err">' + esc(row.reason) + '</p>' : '') + session +
    '<div class="starter-operation-approvals">' + (row.background ? '<p class="stat-sub">Background approvals use the session channel.</p>' : approvals.length ? approvals.map(function (ask) {
      return '<div class="notice warn"><div class="notice-title">Approval required</div><div class="notice-body"><code>' +
        esc(ask.action) + '</code></div><button type="button" class="btn btn-primary" data-flow-approval="' +
        esc(ask.ask_id) + '" data-owner="' + esc(ask.owner) + '" data-digest="' + esc(ask.digest) +
        '" data-approved="true">Allow</button><button type="button" class="btn" data-flow-approval="' +
        esc(ask.ask_id) + '" data-owner="' + esc(ask.owner) + '" data-digest="' + esc(ask.digest) +
        '" data-approved="false">Deny</button></div>';
    }).join("") : '<p class="stat-sub">No approval is waiting.</p>') + (row.background ? "" : operationSettledHtml(settled)) +
    '</div></section>';
}

/* Answer an operation's approval, bound to the ask's owner and digest, then `after()`. */
function wireOperationApprovals(host, after) {
  host.querySelectorAll("[data-flow-approval]").forEach(function (button) {
    button.addEventListener("click", function () {
      var ask = this.getAttribute("data-flow-approval");
      var body = { approved: this.getAttribute("data-approved") === "true",
                   owner: this.getAttribute("data-owner"), digest: this.getAttribute("data-digest") };
      guardAction(this, function () {
        return api("/api/approvals/" + encodeURIComponent(ask), { method: "POST", body: body }).then(after);
      }, { busy: "Sending…" });
    });
  });
}

/* The starter card. With `split` (the session page) it returns the result and next action
 * apart from the coverage, integrity, identities and receipts, which that page keeps under
 * "Details and evidence"; the session page already shows the state chips, so only the review
 * chip stays. Without it (the Trace page) it is one card, as before. */
function starterResultHtml(row, split) {
  var state = row.state || {}, coverage = row.coverage || {}, action = row.next_action || {};
  var legacy = row.starter_metadata === "legacy";
  var workspace = ((STATE.config && STATE.config.workspaces) || []).find(function (w) { return w.path === row.workspace; });
  var implement = action.id === "implement-plan" && workspace ?
    '<a class="btn" id="starter-implement" href="#/starters/build-review?workspace=' + workspace.index +
    '&variant=pair&plan_artifact=' + encodeURIComponent(action.plan_artifact) + '">Preview implementation of this plan</a>' : "";
  var chips = split ? '<p>' + chip("review", row.review_label || "unknown") + '</p>' :
    '<p>' + chip("process", state.process || "unknown") + " " + chip("work", state.work || "unknown") + " " +
    chip("outcome", state.outcome || "unknown") + " " + chip("verification", (row.verification || {}).status || "unknown") + " " +
    chip("review", row.review_label || "unknown") + '</p>';
  var evidence = '<p id="starter-coverage">Selected-session coverage: <strong>' + (coverage.complete ? "complete" : "incomplete / unknown") +
    '</strong> · returned ' + esc(coverage.returned_records == null ? "unknown" : coverage.returned_records) +
    ' of ' + esc(coverage.total_records == null ? "unknown" : coverage.total_records) +
    ' records · offset ' + esc(coverage.offset) + ', limit ' + esc(coverage.limit) +
    '. This does not establish project-wide completion.</p>' +
    '<details><summary>Coverage and evidence integrity</summary><pre class="starter-data">' + esc(JSON.stringify({ starter_metadata: row.starter_metadata || "unknown", coverage: coverage, approvals: row.approvals_evidence }, null, 2)) + '</pre></details>' +
    '<h3>Recorded identities</h3>' + starterBindingsHtml(row.identities, true) +
    '<details><summary>Recorded goal, constraints and supplied sources</summary><p>' + esc(row.source_scope || "Supplied context; file reads are not proven.") +
    '</p><pre class="starter-data">' + esc(JSON.stringify({ goal: row.goal, requirements: row.requirements, constraints: row.constraints, exclude: row.exclude, approved_scope: row.approved_scope, supplied_sources: row.supplied_sources, recorded_task: legacy ? row.task : undefined }, null, 2)) + '</pre></details>' +
    '<details><summary>Artifacts, review and acceptance receipts</summary><p>' + esc(row.verification_evidence || "Flow checks are unavailable in this release; review is separate from verification.") +
    '</p><pre class="starter-data">' + esc(JSON.stringify({ artifacts: row.artifacts || [], review: row.review, verification: row.verification, acceptance_receipts: row.acceptance_receipts || [] }, null, 2)) + '</pre></details>' +
    (coverage.next_offset != null ? '<button type="button" class="btn" id="starter-next-page">Next evidence page</button>' : "");
  // A next action only when there is one: "inspect recorded results" is what the page already
  // is, and on the session page a running session's evidence is incomplete by nature (its
  // live block says what it is doing), unless an approval waits on the person.
  var realAction = action.id && action.id !== "inspect" &&
    !(split && ["queued", "working", "waiting"].indexOf(state.work) >= 0 && action.id !== "approval");
  var again = typeof flowRunAgainHtml === "function" ? flowRunAgainHtml(row) : "";
  var result = '<details><summary>Stored output · ' + esc((row.output || {}).scope || "unknown") + '</summary><p class="stat-sub">Stored summaries may be clipped. Missing or withheld output is not success evidence.</p><pre class="starter-data">' +
    esc((row.output || {}).text == null ? "Output unavailable or withheld." : row.output.text) + '</pre></details>' +
    // "Run again" (a flow started from the Flows page) is a real next action on its own.
    (realAction || again ? '<h3>Next action</h3>' : "") +
    (realAction ? '<p>' + esc(action.label || "Inspect recorded evidence") + '</p>' + implement : "") + again +
    (realAction ? (action.id === "implement-plan" && !workspace ? '<p>The recorded workspace is outside this dashboard’s allowlist. Copy the command into a terminal in that workspace.</p>' : "") +
    (action.command ? '<label>Suggested CLI command <textarea id="starter-next-command" readonly rows="3">' + esc(action.command) + '</textarea></label><button type="button" class="btn" id="starter-next-copy">Copy command</button>' : "") : "");
  var head = '<section class="card starter-result" id="starter-result"><h2>' +
    (legacy ? "Recorded run evidence" : "Starter result · " + esc(row.starter_id || "unknown")) + '</h2>' +
    (legacy ? '<p class="stat-sub">Starter input/source provenance was not recorded for this run.</p>' : "") +
    chips + '<p class="stat-sub">' + esc(row.process_evidence || "Stored evidence only; active-owner liveness is not probed.") + '</p>';
  if (split) {
    return { card: head + result + '</section>',
             evidence: '<section class="starter-evidence" id="starter-evidence"><h2>Starter evidence</h2>' + evidence + '</section>' };
  }
  return head + evidence + result + '</section>';
}

/* Only a session the server records as admitted from a Starter gets the card: a plain run
 * (`starter_metadata: "legacy"`) has no starter provenance to show, and its state is already
 * on the page. */
function loadStarterResult(sessionId, offset) {
  var host = el("starter-result-host");
  if (!host) return;
  // Newly admitted starter sessions have full UUIDs. Legacy non-UUID IDs
  // are not project-scoped names and must not trigger name resolution here.
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(sessionId)) return;
  var request = {};
  STATE.starterResult = request;
  var evidenceHost = el("starter-evidence-host");
  function current() {
    return STATE.starterResult === request && host.isConnected && el("starter-result-host") === host;
  }
  return api("/api/scenario-runs/" + encodeURIComponent(sessionId) + "?offset=" + (offset || 0)).then(function (row) {
    if (!current()) return;
    if (row.starter_metadata === "legacy") {
      host.innerHTML = "";
      if (evidenceHost) evidenceHost.innerHTML = "";
      return;
    }
    if (evidenceHost) {
      var parts = starterResultHtml(row, true);
      host.innerHTML = parts.card;
      evidenceHost.innerHTML = parts.evidence;
    } else {
      host.innerHTML = starterResultHtml(row);
    }
    var copy = el("starter-next-copy");
    if (copy) copy.addEventListener("click", function () { copyStarterCommand(el("starter-next-command")); });
    var next = el("starter-next-page");
    if (next) next.addEventListener("click", function () { loadStarterResult(sessionId, row.coverage.next_offset); });
  }).catch(function (err) {
    if (isSuperseded(err)) return;
    if (current()) host.innerHTML = starterErrorHtml(err);
  });
}
