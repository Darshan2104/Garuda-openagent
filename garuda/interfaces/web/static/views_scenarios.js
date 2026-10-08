/* Starters: read-only catalog, explicit preview/copy, and recorded result evidence. */
"use strict";

function stopStarterViews() { STATE.starterView = null; STATE.starterResult = null; }

function starterErrorHtml(err) {
  return '<div class="notice err"><div class="notice-title">' + esc(err.code || "Could not load starter") +
    '</div><div class="notice-body">' + esc(err.message) + "</div></div>";
}

function starterBindingsHtml(bindings, recorded) {
  var rows = recorded ? bindings || [] : Object.keys(bindings || {}).map(function (name) {
    return Object.assign({ role: name }, bindings[name]);
  });
  if (!rows.length) return '<p class="stat-sub">Role identities are unknown.</p>';
  return '<div class="table-wrap"><table class="starter-bindings"><thead><tr><th>Role / step</th><th>' +
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
  return '<label class="starter-workspace">Workspace <select id="starter-workspace">' + rows.map(function (r) {
    return '<option value="' + esc(r.index) + '"' + (r.index === selected ? " selected" : "") + '>' + esc(r.path) + "</option>";
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
        '<a class="btn" href="#/sessions">Follow a run</a></div><p>Choose a goal, preview it, then copy its command into your terminal.</p>' +
        '<p class="starter-context">Workspace: <code>' + esc(library.workspace) + '</code></p>' +
        (state.entry ? '<a href="#/starters?workspace=' + workspace + '">← All starters</a>' + starterFormHtml(state) :
          '<div class="grid starter-grid" id="starter-cards">' + library.starters.map(function (r) {
            return '<section class="card" data-starter="' + esc(r.id) + '"><h2>' + esc(r.title) + '</h2>' + starterReadinessHtml(r.readiness) +
              '<p class="stat-sub">' + esc(r.launch.kind === "flow" ? "Flow checks unavailable in this release" : "Verification comes from acceptance receipts") +
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
    '<div id="starter-preview" aria-live="polite"><p class="stat-sub">Preview starts nothing. Preview again after sources or configuration change.</p></div></section>';
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
  var target = el("starter-preview");
  if (target) target.innerHTML = '<p class="stat-sub">Inputs changed. Preview again before copying a command.</p>';
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
  var revision = ++state.revision;
  state.preview = null;
  var body = { starter_id: state.id, inputs: starterInputs(state), workspace: state.workspace };
  if (remedy) {
    body.remedy = "build-and-check";
    body.checks = el("starter-remedy-checks").value.split(/\r?\n/).filter(function (v) { return /\S/.test(v); });
  }
  el("starter-preview").innerHTML = '<p class="loading">Compiling preview…</p>';
  return api("/api/scenarios/preview", { method: "POST", body: body }).then(function (data) {
    if (STATE.starterView !== state || revision !== state.revision) return;
    state.preview = data;
    el("starter-preview").innerHTML = starterPreviewHtml(data);
    el("starter-copy").addEventListener("click", function () { copyStarterCommand(el("starter-command")); });
  }).catch(function (err) {
    if (STATE.starterView === state && revision === state.revision) el("starter-preview").innerHTML = starterErrorHtml(err);
  });
}

function copyStarterCommand(node) {
  navigator.clipboard.writeText(node.value).then(function () { toast("Command copied. Run it in your terminal."); }).catch(function () {
    node.focus(); node.select(); toast("Select and copy the command from the text box.");
  });
}

function starterPreviewHtml(data) {
  var plan = data.plan;
  return '<h3>Preview</h3>' + starterReadinessHtml(data.readiness) +
    '<p>' + chip("launch", plan.kind + " / " + plan.target) + " " + chip("verification", plan.provenance.verification) + " " + chip("cost", plan.limits.cost) + '</p>' +
    '<p class="stat-sub">Effective flow source: ' + esc(plan.provenance.flow_source) + ' · configured role invocations ≤ ' + esc(plan.limits.max_role_invocations) +
    ' · review pairs ≤ ' + esc(plan.limits.review_pairs) + '. ' + esc(plan.limits.note || "") + '</p>' +
    '<details><summary>Supplied source provenance and requested step posture</summary><p>Named repository sources are references; this does not prove a runtime has read them.</p><pre class="starter-data">' +
    esc(JSON.stringify({ sources: plan.sources, checks: plan.checks, options: plan.options, flow: plan.flow, provenance: plan.provenance, bindings: plan.bindings, configuration: data.configuration }, null, 2)) + '</pre></details>' +
    '<label>Equivalent CLI command <textarea id="starter-command" readonly rows="6">' + esc(plan.equivalent_command) + '</textarea></label>' +
    '<button type="button" class="btn" id="starter-copy">Copy command</button>' +
    '<p class="stat-sub">This equivalent command records an ordinary run. The CLI <code>garuda starter run</code> also records structured starter inputs and source provenance.</p>' +
    '<p class="stat-sub">Plan digest: <code>' + esc(plan.digest) + '</code>. Sources and configuration can change after preview; create a fresh preview when they do.</p>';
}

function starterResultHtml(row) {
  var state = row.state || {}, coverage = row.coverage || {}, action = row.next_action || {};
  var legacy = row.starter_metadata === "legacy";
  var workspace = ((STATE.config && STATE.config.workspaces) || []).find(function (w) { return w.path === row.workspace; });
  var implement = action.id === "implement-plan" && workspace ?
    '<a class="btn" id="starter-implement" href="#/starters/build-review?workspace=' + workspace.index +
    '&variant=pair&plan_artifact=' + encodeURIComponent(action.plan_artifact) + '">Preview implementation of this plan</a>' : "";
  return '<section class="card starter-result" id="starter-result"><h2>' +
    (legacy ? "Recorded run evidence" : "Starter result · " + esc(row.starter_id || "unknown")) + '</h2>' +
    (legacy ? '<p class="stat-sub">Starter input/source provenance was not recorded for this run.</p>' : "") +
    '<p>' + chip("process", state.process || "unknown") + " " + chip("work", state.work || "unknown") + " " +
    chip("outcome", state.outcome || "unknown") + " " + chip("verification", (row.verification || {}).status || "unknown") + " " +
    chip("review", row.review_label || "unknown") + '</p><p class="stat-sub">' + esc(row.process_evidence || "Stored evidence only; active-owner liveness is not probed.") + '</p>' +
    '<p id="starter-coverage">Selected-session coverage: <strong>' + (coverage.complete ? "complete" : "incomplete / unknown") +
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
    '<details><summary>Stored output · ' + esc((row.output || {}).scope || "unknown") + '</summary><p class="stat-sub">Stored summaries may be clipped. Missing or withheld output is not success evidence.</p><pre class="starter-data">' +
    esc((row.output || {}).text == null ? "Output unavailable or withheld." : row.output.text) + '</pre></details>' +
    '<h3>Next action</h3><p>' + esc(action.label || "Inspect recorded evidence") + '</p>' + implement +
    (action.id === "implement-plan" && !workspace ? '<p>The recorded workspace is outside this dashboard’s allowlist. Copy the command into a terminal in that workspace.</p>' : "") +
    (action.command ? '<label>Suggested CLI command <textarea id="starter-next-command" readonly rows="3">' + esc(action.command) + '</textarea></label><button type="button" class="btn" id="starter-next-copy">Copy command</button>' : "") +
    (coverage.next_offset != null ? '<button type="button" class="btn" id="starter-next-page">Next evidence page</button>' : "") + '</section>';
}

function loadStarterResult(sessionId, offset) {
  var host = el("starter-result-host");
  if (!host) return;
  // Newly admitted starter sessions have full UUIDs. Legacy non-UUID IDs
  // are not project-scoped names and must not trigger name resolution here.
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(sessionId)) return;
  var request = {};
  STATE.starterResult = request;
  return api("/api/scenario-runs/" + encodeURIComponent(sessionId) + "?offset=" + (offset || 0)).then(function (row) {
    if (STATE.starterResult !== request || !host.isConnected || el("starter-result-host") !== host) return;
    host.innerHTML = starterResultHtml(row);
    var copy = el("starter-next-copy");
    if (copy) copy.addEventListener("click", function () { copyStarterCommand(el("starter-next-command")); });
    var next = el("starter-next-page");
    if (next) next.addEventListener("click", function () { loadStarterResult(sessionId, row.coverage.next_offset); });
  }).catch(function (err) {
    if (STATE.starterResult === request && host.isConnected && el("starter-result-host") === host) host.innerHTML = starterErrorHtml(err);
  });
}
