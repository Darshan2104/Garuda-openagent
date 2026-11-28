/* Flows: every flow this workspace can run, its pipeline, and a run of it from here.
 *
 * Reads /api/flows (the library) and /api/flows/<name> (one flow): pure reads that resolve
 * each flow the way the `run-flow` starter does, so a card says what a preview and a start
 * will check. A run is the `run-flow` starter: POST /api/scenarios/preview with
 * {flow, goal, sources, options.checks}, then POST /api/scenarios/start with the preview and
 * authority digests. The browser sends a flow's name, never its definition.
 *
 * Start is offered only when the dashboard may start this flow now (write mode, the Linux
 * supervisor, a flow shape the owned engine runs, permissions within the ceiling, readiness
 * without errors). Otherwise the page says why and stays copy-only: Preview and Copy always
 * work. A started flow continues on its session page (#/sessions/<id>?operation=<uuid>),
 * whose operation panel shows the operation and its approvals and keeps them alive. */

"use strict";

var FLOW_SOURCE_LABEL = { packaged: "packaged example", user: "your config", project: "project file" };
var FLOW_GUIDE = "https://darshan2104.github.io/Garuda-openagent/guides/configuration/#roles-and-flows-garudayaml";

function stopFlowViews() { STATE.flowView = null; }

function flowWorkspaceIndex() {
  var raw = routeParams().workspace;
  var index = raw === undefined ? 0 : Number(raw);
  return Number.isInteger(index) && index >= 0 ? index : 0;
}

function flowWorkspaceHtml(selected) {
  var rows = (STATE.config && STATE.config.workspaces) || [];
  return '<label class="starter-workspace">Workspace <select id="flows-workspace" data-route-param="workspace">' +
    rows.map(function (r) {
      return '<option value="' + esc(r.index) + '"' + (r.index === selected ? " selected" : "") + ">" + esc(r.path) + "</option>";
    }).join("") + "</select></label>";
}

function wireFlowWorkspace(path) {
  var select = el("flows-workspace");
  if (!select) return;
  select.addEventListener("change", function () {
    stopFlowViews();
    location.hash = path + "?workspace=" + encodeURIComponent(this.value);
  });
}

function flowHref(name, workspace) {
  return "#/flows/" + encodeURIComponent(name) + "?workspace=" + encodeURIComponent(workspace);
}

function flowSourceHtml(row) {
  var label = FLOW_SOURCE_LABEL[row.source] || row.source || "unknown";
  var html = chip("source", label, row.source === "project" ? "chip-bad" : "chip-off");
  if (row.source === "project") {
    html += ' <span class="stat-sub flow-project-note" data-flow-project="' + esc(row.trusted ? "trusted" : "untrusted") + '">' +
      "Defined by this project's garuda.yaml" + (row.trusted ? " (trusted)" : ", which is not trusted") +
      ", not your own configuration. Review its steps before starting.</span>";
  }
  return html;
}

function flowMemberText(member) {
  var who = member.runtime_id || "harness unknown";
  if (member.model_id) who += "/" + member.model_id;
  return member.role + " → " + who;
}

/* One line per step: role → harness/model, review loops and parallel groups visible. */
function compactPipelineHtml(pipeline) {
  var steps = (pipeline && pipeline.steps) || [];
  if (!steps.length) return '<p class="stat-sub">No steps could be read.</p>';
  return '<ol class="flow-compact">' + steps.map(function (step) {
    var members = (step.members || []).map(flowMemberText);
    var line = step.kind === "parallel" ? "parallel: " + members.join(" & ") : members.join("");
    var review = step.review ? '<span class="flow-compact-review">↻ reviewed by ' + esc(step.review.by) +
      ", up to " + esc(step.review.max_rounds + 1) + " round" + (step.review.max_rounds ? "s" : "") + "</span>" : "";
    return '<li data-step="' + esc(step.id) + '"><strong>' + esc(step.id) + "</strong> " + esc(line) + " " + review + "</li>";
  }).join("") + "</ol>";
}

/* Reasons that belong to this dashboard rather than to a flow; the page says them once. */
var FLOW_HOST_REASONS = ["dashboard.read_only", "starter.ownership_unavailable", "starter.approval_unavailable"];

/* Flow shapes that stay CLI-only (a user decision): the reason says so with the command.
 * Whether a flow starts here is the server's answer (`start.supported`, the preview's
 * authority); this list only words the reason. Step `retries` start here; only a checked
 * flow refuses them (FLOW_CHECKED_RETRIES). */
var FLOW_CLI_ONLY = ["starter.parallel_ownership_unavailable"];
var FLOW_CHECKED_RETRIES = "flow.retries_unsupported";

function shellWord(text) {
  text = String(text);
  return /^[A-Za-z0-9_@%+=:,.\/-]+$/.test(text) ? text : "'" + text.replace(/'/g, "'\"'\"'") + "'";
}

/* `garuda flow run` for a flow, with the task when known, else a placeholder to fill. */
function flowCliCommand(name, workspace, task) {
  return "garuda flow run " + shellWord(name) + " --task " + (task === undefined ? '"<your task>"' : shellWord(task)) +
    (workspace ? " --workspace " + shellWord(workspace) : "");
}

function flowCliOnlyHtml(command) {
  return 'Parallel groups run only from the CLI: <code class="flow-cli-command">' + esc(command) + "</code>";
}

/* A checked flow's retries: the CLI with checks refuses them too, so say what works. */
function flowCheckedRetriesHtml(command) {
  return "A flow with checks cannot retry steps — remove the checks, or start it from the CLI without checks" +
    (command ? ': <code class="flow-cli-command">' + esc(command) + "</code>" : ".");
}

/* The equivalent command for a checked flow's retries reason, without its `--check`
 * arguments (shlex-quoted words): `garuda flow run` without checks runs the retries.
 * Configured checks are not on the command line; the reason says to remove them. */
function flowCommandWithoutChecks(command, code) {
  if (code !== FLOW_CHECKED_RETRIES || !command) return command;
  return String(command).replace(/\s--check\s+(?:'[^']*'(?:"'"'[^']*')*|\S+)/g, "");
}

/* The fix line under a reason: the CLI-only and checked-retries rules in their own words. */
function flowReasonFixHtml(reason, command) {
  if (reason.code === FLOW_CHECKED_RETRIES) return flowCheckedRetriesHtml(command);
  if (FLOW_CLI_ONLY.indexOf(reason.code) !== -1 && command) return flowCliOnlyHtml(command);
  return esc(reason.fix);
}

function flowStartLineHtml(start, host, command) {
  start = start || { supported: false, reasons: [] };
  if (start.supported) return '<p class="flow-start-line" data-start="yes">Start from dashboard: <strong>yes</strong></p>';
  var reasons = (start.reasons || []).filter(function (r) {
    return !(host && !host.start_available && FLOW_HOST_REASONS.indexOf(r.code) !== -1);
  });
  return '<div class="flow-start-line" data-start="copy-only"><p>Start from dashboard: <strong>copy-only</strong>' +
    (reasons.length < (start.reasons || []).length ? ' <span class="stat-sub">(this dashboard' +
      (reasons.length ? ", and:" : ")") + "</span>" : "") + "</p>" +
    '<ul class="starter-diagnostics">' + reasons.map(function (r) {
      return '<li data-reason="' + esc(r.code) + '"><code>' + esc(r.code) + "</code> " + esc(r.message) +
        '<br><span class="stat-sub">' + flowReasonFixHtml(r, command) +
        "</span></li>";
    }).join("") + "</ul></div>";
}

function flowCardHtml(row, workspace, host) {
  var state = row.readiness || {};
  return '<section class="card flow-card" data-flow="' + esc(row.name) + '"><h2><a href="' + flowHref(row.name, workspace) + '">' +
    esc(row.name) + "</a></h2>" +
    '<p class="chips flow-card-meta">' + flowSourceHtml(row) + " " +
    chip("readiness", row.error ? "error" : (state.status || "not checked"), state.can_run ? "chip-on" : (row.error ? "chip-bad" : "chip-off")) +
    " " + chip("steps", row.steps) + "</p>" +
    (row.description ? '<p class="stat-sub">' + esc(row.description) + "</p>" : "") +
    compactPipelineHtml(row.pipeline) +
    (row.error ? '<div class="notice err flow-error" data-error="' + esc(row.error.code) + '"><div class="notice-title">' +
      esc(row.error.code) + '</div><div class="notice-body">' + esc(row.error.message) + "<br>" + esc(row.error.fix) + "</div></div>" : "") +
    ((row.missing_roles || []).length ? '<p class="flow-missing">Missing roles: <code>' + row.missing_roles.map(esc).join("</code>, <code>") +
      "</code> — define them in garuda.yaml (<code>garuda init</code> proposes them).</p>" : "") +
    flowStartLineHtml(row.start, host, flowCliCommand(row.name, host && host.workspace)) +
    '<a class="btn" href="' + flowHref(row.name, workspace) + '">Open flow</a></section>';
}

function flowHostNoticeHtml(host) {
  if (!host || host.start_available) return "";
  var why = host.mode === "read-only" ? "This dashboard is read-only." :
    "This host has no Linux supervisor, which owned dashboard starts need.";
  return '<div class="notice" id="flows-copy-only"><div class="notice-title">Copy-only here</div><div class="notice-body">' +
    esc(why) + " Preview any flow and copy its command to run it in a terminal.</div></div>";
}

function flowsView() {
  var workspace = flowWorkspaceIndex();
  var state = STATE.flowView = { workspace: workspace };
  render('<div class="loading">Loading flows…</div>');
  return api("/api/flows?workspace=" + encodeURIComponent(workspace)).then(function (data) {
    if (STATE.flowView !== state) return;
    var own = data.flows.filter(function (row) { return row.source !== "packaged"; });
    // Your own flows first, then the packaged examples; by name within each.
    var ordered = own.concat(data.flows.filter(function (row) { return row.source === "packaged"; }));
    render('<div class="page-head"><h1>Flows</h1>' + flowWorkspaceHtml(workspace) +
      '<a class="btn" href="#/sessions">Follow a run</a></div>' +
      "<p>A flow runs ordered steps, each by a role bound to a coding agent. Choose one to see its pipeline, preview a run and start it.</p>" +
      flowHostNoticeHtml(data.host) +
      (data.config_error ? '<div class="notice err" id="flows-config-error"><div class="notice-title">' + esc(data.config_error.code) +
        '</div><div class="notice-body">' + esc(data.config_error.message) + "<br>" + esc(data.config_error.fix) +
        " Only the packaged flows are listed until it loads.</div></div>" : "") +
      (own.length ? "" : '<div class="empty" id="flows-empty"><p><strong>No flows of your own yet.</strong> A flow is a list of steps under ' +
        "<code>flows:</code> in garuda.yaml, each run by one of your roles; <code>garuda config show --flow plan-only</code> prints one to copy.</p>" +
        '<p><a href="' + FLOW_GUIDE + '" target="_blank" rel="noopener noreferrer">How to define a flow</a> · the packaged examples are below.</p></div>') +
      '<div class="grid starter-grid" id="flow-cards">' + ordered.map(function (row) { return flowCardHtml(row, workspace, Object.assign({ workspace: data.workspace }, data.host)); }).join("") + "</div>");
    wireFlowWorkspace("#/flows");
  }).catch(function (err) {
    if (isSuperseded(err) || STATE.flowView !== state) return;
    render(err.status === 401 ? tokenRequiredPanel() : starterErrorHtml(err));
  });
}

/* --- one flow -------------------------------------------------------------- */

function flowLimitsText(limits) {
  if (!limits) return "unknown";
  var parts = [["deadline", limits.deadline_min], ["silence", limits.silence_min], ["tool silence", limits.tool_silence_min]]
    .filter(function (p) { return p[1] !== null && p[1] !== undefined; })
    .map(function (p) { return p[0] + " " + p[1] + " min"; });
  return (parts.length ? parts.join(" · ") : "none") + (limits.basis === "configured" ? " (configured; not checked here)" : "");
}

function flowBackstopText(backstop) {
  if (!backstop || backstop.timeout_min === null || backstop.timeout_min === undefined) return "unknown";
  return backstop.timeout_min + " min " + (backstop.rule === "deadline-plus-margin" ?
    "(its deadline plus a margin)" : "(no deadline_min: the supervisor's default)");
}

function flowInputText(input) {
  return input.type + (input.from ? " from " + input.from : (input.producer ? " (latest from " + input.producer + ")" : ""));
}

function flowMembersHtml(members) {
  // .table-stack with data-th: at phone width each row stacks into labelled cells.
  function cell(label, html) { return '<td data-th="' + label + '">' + html + "</td>"; }
  return '<div class="table-wrap"><table class="starter-bindings table-stack flow-members"><thead><tr><th>Role</th><th>Runtime / model</th>' +
    "<th>Effort</th><th>Permission: configured → effective</th><th>Writes</th><th>Fallbacks</th></tr></thead><tbody>" +
    (members || []).map(function (m) {
      var perm = m.permission || {};
      return '<tr data-role="' + esc(m.role) + '">' + cell("Role", esc(m.role)) +
        cell("Runtime / model", esc(m.runtime_id || "unknown") + " / " + esc(m.model_id || "harness default") +
          (m.harness_mode ? " · mode " + esc(m.harness_mode) : "")) +
        cell("Effort", esc(m.effort || "default")) +
        cell("Permission", esc(perm.configured || "unset") + " → " + esc(perm.effective || "unknown")) +
        cell("Writes", esc((m.write_policy || {}).effective || "unknown")) +
        cell("Fallbacks", (m.fallbacks || []).length ? m.fallbacks.map(function (f) {
          return esc(f.runtime_id) + (f.model_id ? "/" + esc(f.model_id) : "") + " (" + esc(f.permission || "unknown") + ")";
        }).join(", ") : "none") + "</tr>";
    }).join("") + "</tbody></table></div>";
}

/* The pipeline before it runs, in the session page's markup (flowHtml): the same
 * `.pipeline` list and `.pipe-step` rows, so a flow looks the same before and after. */
function flowPipelineHtml(pipeline) {
  if (!pipeline) return "";
  var edges = pipeline.edges || [];
  return '<section class="card flow" id="flow-pipeline"><h2>Pipeline ' + chip("resolution", pipeline.resolution || "unknown") + "</h2>" +
    '<ol class="pipeline steps">' + (pipeline.steps || []).map(function (step, index) {
      var passes = edges.filter(function (e) { return e.from === step.id && e.kind !== "sequence"; });
      return '<li class="pipe-step pipe-pending" data-step="' + esc(step.id) + '"><div class="pipe-head"><span class="pipe-no">' + (index + 1) +
        "</span><strong>" + esc(step.id) + '</strong> <span class="pill unknown">' + esc(step.kind === "parallel" ? "parallel group" : "role step") +
        "</span>" + (step.write_policy ? " " + chip("write policy", step.write_policy) : "") + "</div>" +
        flowMembersHtml(step.members) +
        '<dl class="pipe-detail">' +
        "<dt>Inputs</dt><dd>" + ((step.inputs || []).length ? step.inputs.map(function (i) { return esc(flowInputText(i)); }).join(", ") : "none") + "</dd>" +
        "<dt>Outputs</dt><dd>" + ((step.outputs || []).length ? step.outputs.map(esc).join(", ") : "none") + "</dd>" +
        (step.review ? "<dt>Review</dt><dd>by " + esc(step.review.by) + ", up to " + esc(step.review.max_rounds) + " more round" +
          (step.review.max_rounds === 1 ? "" : "s") + " · " + (step.review.independent ? "independent reviewer required" : "independence waived") + "</dd>" : "") +
        "<dt>Retries</dt><dd>" + esc(step.retries) + "</dd>" +
        "<dt>Limits</dt><dd>" + esc(flowLimitsText(step.limits)) + "</dd>" +
        (step.long_run ? "<dt>Long run</dt><dd>" + (step.long_run.applies === false ? "not used (only ACP role steps run long) · " :
          step.long_run.applies === null ? "unknown whether it applies · " : "") + esc("up to " + step.long_run.max_hours + " h, " +
          step.long_run.max_turns + " turns, a new session every " + step.long_run.session_turns + " turns or " + step.long_run.session_hours +
          " h, stop after " + step.long_run.no_progress_turns + " turns without progress (from the " + step.long_run.source + ")") + "</dd>" : "") +
        "<dt>Owned backstop</dt><dd>" + esc(flowBackstopText(step.owned_backstop)) + "</dd></dl>" +
        (passes.length ? '<div class="pipe-pass stat-sub">passes on: ' + passes.map(function (e) {
          return esc(e.kind === "review" ? "its work for review" : e.type + (e.member ? " (" + e.member + ")" : "")) + " → " + esc(e.to);
        }).join(", ") + "</div>" : "") + "</li>";
    }).join("") + "</ol>" +
    ((pipeline.unknown || []).length ? '<details class="flow-unknown"><summary>' + esc(pipeline.unknown.length) +
      " value" + (pipeline.unknown.length === 1 ? " is" : "s are") + ' unknown here</summary><ul>' + pipeline.unknown.map(function (u) {
        return "<li><code>" + esc(u.path) + "</code>: " + esc(u.reason) + "</li>";
      }).join("") + "</ul></details>" : "") + "</section>";
}

function flowLoginsHtml(logins) {
  var ids = Object.keys(logins || {});
  if (!ids.length) return "";
  return '<p class="flow-logins">' + ids.map(function (id) {
    return '<span class="flow-login" data-runtime="' + esc(id) + '">' + esc(id) + " " + loginPill(logins[id]) + "</span>";
  }).join(" · ") + "</p>";
}

function flowFormHtml() {
  return '<section class="card starter-form flow-run"><h2>Run this flow</h2>' +
    '<form id="flow-form" novalidate>' +
    '<label>Task * <textarea data-field="goal" rows="4" required placeholder="What should this flow do?"></textarea></label>' +
    '<label>Sources <textarea data-field="sources" rows="2" placeholder="One source per line: repo/path#section or session:NAME"></textarea></label>' +
    '<label>Explicit checks <textarea data-option="checks" rows="2" placeholder="One command per line; they join the configured checks"></textarea></label>' +
    '<div class="row"><button type="submit" class="btn btn-primary" id="flow-preview-button">Preview</button></div></form>' +
    '<div id="flow-preview" class="starter-preview" aria-live="polite"><p class="stat-sub">Preview starts nothing. Editing any field discards the preview.</p></div>' +
    '<div id="flow-operation" aria-live="polite"></div></section>';
}

function flowDetailView(name) {
  var workspace = flowWorkspaceIndex();
  var state = STATE.flowView = { name: name, workspace: workspace, revision: 0, preview: null, operation: null };
  render('<div class="loading">Loading flow…</div>');
  return api("/api/flows/" + encodeURIComponent(name) + "?workspace=" + encodeURIComponent(workspace)).then(function (data) {
    if (STATE.flowView !== state) return;
    var row = state.row = data.flow;
    state.host = data.host;
    render('<div class="page-head"><h1>Flow ' + esc(row.name) + "</h1>" + flowWorkspaceHtml(workspace) +
      '<a class="btn" href="#/flows?workspace=' + esc(workspace) + '">All flows</a></div>' +
      '<p class="chips flow-card-meta">' + flowSourceHtml(row) + (row.source_file ? ' <span class="stat-sub">' + esc(row.source_file) + "</span>" : "") + "</p>" +
      (row.description ? "<p>" + esc(row.description) + "</p>" : "") +
      flowHostNoticeHtml(data.host) +
      (row.error ? '<div class="notice err flow-error" data-error="' + esc(row.error.code) + '"><div class="notice-title">' + esc(row.error.code) +
        '</div><div class="notice-body">' + esc(row.error.message) + "<br>" + esc(row.error.fix) + "</div></div>" : "") +
      flowPipelineHtml(row.pipeline) +
      '<section class="card" id="flow-readiness"><h2>Readiness</h2>' +
      (row.readiness ? starterReadinessHtml(row.readiness) : '<p class="stat-sub">Not checked: the flow did not resolve.</p>') +
      flowLoginsHtml(data.logins) + flowStartLineHtml(row.start, data.host, flowCliCommand(row.name, data.workspace)) + "</section>" +
      flowFormHtml());
    wireFlowWorkspace("#/flows/" + encodeURIComponent(name));
    wireFlowForm(state);
    prefillFlowTask(state, routeParams().from);
  }).catch(function (err) {
    if (isSuperseded(err) || STATE.flowView !== state) return;
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    render(starterErrorHtml(err) + '<p><a class="btn" href="#/flows?workspace=' + esc(workspace) + '">All flows</a></p>');
  });
}

/* "Run again" carries the session it came from (?from=<id>): its recorded task, when the
 * starter recorded one for this flow, fills the Task field to edit. It never previews. */
function prefillFlowTask(state, sessionId) {
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(sessionId || "")) return;
  api("/api/scenario-runs/" + encodeURIComponent(sessionId) + "?workspace=" + encodeURIComponent(state.workspace)).then(function (row) {
    if (STATE.flowView !== state || !row || row.starter_id !== "run-flow" || !row.flow || row.flow.name !== state.name) return;
    if (typeof row.goal !== "string" || !row.goal) return;
    var field = el("flow-form") && el("flow-form").querySelector('[data-field="goal"]');
    if (!field || field.value) return;  // never over what the user already typed
    field.value = row.goal;
    var note = document.createElement("p");
    note.className = "stat-sub";
    note.id = "flow-prefilled";
    note.innerHTML = "Task filled from the run in session <code>" + esc(String(sessionId).slice(0, 8)) +
      "</code>. Edit it, then preview.";
    field.parentElement.insertAdjacentElement("afterend", note);
  }, function (err) {
    if (isSuperseded(err)) return;  // a missing record leaves the field empty
  });
}

function flowInputs() {
  var form = el("flow-form"), values = {};
  var goal = form.querySelector('[data-field="goal"]').value;
  var sources = form.querySelector('[data-field="sources"]').value.split(/\r?\n/).filter(function (v) { return /\S/.test(v); });
  var checks = form.querySelector('[data-option="checks"]').value.split(/\r?\n/).filter(function (v) { return /\S/.test(v); });
  if (goal) values.goal = goal;
  if (sources.length) values.sources = sources;
  if (checks.length) values.options = { checks: checks };
  return values;
}

function invalidateFlowPreview(state) {
  state.revision++;
  state.preview = null;
  state.operation = null;
  var target = el("flow-preview");
  if (target) target.innerHTML = '<p class="stat-sub" id="flow-preview-stale">Inputs changed. Preview again before copying the command or starting.</p>';
  var operation = el("flow-operation");
  if (operation) operation.innerHTML = "";
}

function wireFlowForm(state) {
  var form = el("flow-form");
  form.addEventListener("input", function () { invalidateFlowPreview(state); });
  form.addEventListener("change", function () { invalidateFlowPreview(state); });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    guardAction(el("flow-preview-button"), function () { return requestFlowPreview(state); }, { busy: "Previewing…" });
  });
}

function requestFlowPreview(state) {
  var revision = ++state.revision;
  state.preview = null;
  state.operation = null;
  var inputs = Object.assign({ flow: state.name }, flowInputs());
  var stillHere = viewGuard();
  var submitted = submittedViewInputs(el("flow-form"));
  el("flow-preview").innerHTML = '<p class="loading">Compiling preview…</p>';
  el("flow-operation").innerHTML = "";
  return api("/api/scenarios/preview", { method: "POST", body: { starter_id: "run-flow", inputs: inputs, workspace: state.workspace } })
    .then(function (data) {
      if (!stillHere() || STATE.flowView !== state || revision !== state.revision) return;
      submitted();
      state.preview = data;
      state.previewInputs = inputs;
      el("flow-preview").innerHTML = flowPreviewHtml(data, state);
      el("flow-copy").addEventListener("click", function () { copyStarterCommand(el("flow-command")); });
      var start = el("flow-start");
      if (start) start.addEventListener("click", function () {
        guardAction(start, function () { return requestFlowStart(state); }, { busy: "Starting…", errorTarget: "flow-operation" });
      });
    }, function (err) {
      if (isSuperseded(err) || !stillHere() || STATE.flowView !== state || revision !== state.revision) return;
      el("flow-preview").innerHTML = starterErrorHtml(err);
    });
}

/* Per step: who runs it, with configured and effective permission and the dashboard ceiling. */
function flowIdentitiesHtml(data) {
  var envelope = (data.authority || {}).envelope || null;
  var ceiling = envelope ? envelope.operator.max_permission : null;
  var roles = envelope ? envelope.flow.roles : {};
  var rows = [];
  ((data.pipeline || {}).steps || []).forEach(function (step) {
    (step.members || []).forEach(function (member) {
      var admitted = envelope ? (envelope.flow.steps || []).filter(function (s) { return s.index === step.index && s.role === member.role; })[0] : null;
      rows.push('<tr data-step="' + esc(step.id) + '" data-role="' + esc(member.role) + '">' +
        '<td data-th="Step">' + esc(step.id) + '</td><td data-th="Role">' + esc(member.role) +
        '</td><td data-th="Runtime / model">' + esc(((roles[member.role] || {}).runtime_id) || member.runtime_id || "unknown") + " / " +
        esc(((roles[member.role] || {}).model_id) || member.model_id || "harness default") + '</td><td data-th="Configured permission">' +
        esc((member.permission || {}).configured || "unset") + '</td><td data-th="Effective permission">' +
        esc(admitted ? admitted.permission : ((member.permission || {}).effective || "unknown")) + "</td></tr>");
    });
  });
  return '<h3>Who runs each step</h3><p class="stat-sub">' + (ceiling ? "Dashboard permission ceiling: <code>" + esc(ceiling) +
    "</code>. A step above it refuses the start." : "No ceiling applies to a copied command; the dashboard ceiling applies only to starts here.") + "</p>" +
    '<div class="table-wrap"><table class="starter-bindings table-stack" id="flow-identities"><thead><tr><th>Step</th><th>Role</th><th>Runtime / model</th>' +
    "<th>Configured permission</th><th>Effective permission</th></tr></thead><tbody>" + rows.join("") + "</tbody></table></div>";
}

function flowChecksHtml(plan) {
  var checks = plan.checks || [];
  if (!checks.length) return '<h3>Checks</h3><p class="stat-sub" id="flow-checks">No checks: verification stays unavailable. A review is not verification.</p>';
  return '<h3>Checks</h3><ul id="flow-checks">' + checks.map(function (c) {
    var def = c.definition || {};
    return "<li><code>" + esc(typeof def.run === "string" ? def.run : JSON.stringify(def.run)) + "</code> " +
      chip("authority", c.authority) + " " + chip("runs", c.execution) + "</li>";
  }).join("") + "</ul>";
}

function flowDisclosuresHtml(data) {
  var notes = [];
  var unbounded = ((data.pipeline || {}).steps || []).filter(function (s) {
    return s.limits && s.limits.deadline_min == null && s.owned_backstop && s.owned_backstop.timeout_min != null;
  });
  if (unbounded.length) {
    notes.push('<li id="flow-backstop">Started here, ' + unbounded.map(function (s) {
      return "step <strong>" + esc(s.id) + "</strong> has no <code>deadline_min</code> and is stopped by the supervisor after " +
        esc(s.owned_backstop.timeout_min) + " min";
    }).join("; ") + "; a stopped step quarantines the flow. Set <code>limits.deadline_min</code> for a step that needs longer.</li>");
  }
  var retrying = ((data.pipeline || {}).steps || []).filter(function (s) { return typeof s.retries === "number" && s.retries > 0; });
  var refused = ((data.authority || {}).diagnostics || []).some(function (d) { return d.code === FLOW_CHECKED_RETRIES; });
  if (retrying.length && !refused) {
    notes.push('<li id="flow-retries">Step retries run here as in <code>garuda flow run</code>: ' + retrying.map(function (s) {
      return "step <strong>" + esc(s.id) + "</strong> runs up to " + esc(s.retries) + " more time" + (s.retries === 1 ? "" : "s");
    }).join("; ") + " after an attempt that fails or finishes without a usable output. Each attempt is its own owned worker " +
      "with its own receipt, under the same approvals and permission ceiling, and the session page marks it as a retry. " +
      "Refusals, interruptions, deadlines, silence and usage limits are never retried.</li>");
  }
  notes.push('<li id="flow-resume">A flow started here that stops on a resumable stop (paused, interrupted, a usage limit ' +
    "after its reset) is resumed from its session page with <strong>Resume</strong>, by this dashboard in write mode, under " +
    "the plan and permissions its start admitted. <code>garuda flow resume</code> refuses it (<code>flow.operation_managed</code>). " +
    "If the dashboard's server is killed (SIGKILL, the OOM killer) or the machine reboots while a step runs, that step never " +
    "settles; the dashboard started again offers <strong>Resume</strong> once it proves that step's processes are gone, and runs the " +
    "step again. If it cannot prove that, it names what is uncertain (<code>flow.interrupted_unsettled</code>) and never replays " +
    "the step. A flow run from the copied command resumes with <code>garuda flow resume</code>.</li>");
  notes.push('<li id="flow-foreground">Stopping the dashboard (<code>garuda web</code>) interrupts a flow started here. Closing the browser tab does not; ' +
    "its approvals wait on any page of an open dashboard tab and in Approvals for up to 5 minutes (also when a step runs in its own process), " +
    "and are denied after that, about 90 seconds after no tab is watching the run, or at once if the dashboard stops.</li>");
  return '<h3>Before you start</h3><ul class="flow-disclosures">' + notes.join("") + "</ul>";
}

function flowCanStart(data, state) {
  var health = STATE.health || {};
  return !!(data.authority && data.authority.can_start && data.readiness && data.readiness.can_run &&
    health.capabilities && health.capabilities.starter_start && state.previewInputs);
}

function flowPreviewHtml(data, state) {
  var plan = data.plan;
  var canStart = flowCanStart(data, state);
  var blockers = ((data.authority || {}).diagnostics || []);
  return "<h3>Preview</h3>" + starterReadinessHtml(data.readiness) +
    (blockers.length ? '<ul class="starter-diagnostics" id="flow-authority">' + blockers.map(function (d) {
      return '<li data-diagnostic="' + esc(d.code) + '"><code>' + esc(d.code) + "</code> " + esc(d.message) +
        '<br><span class="stat-sub">' + flowReasonFixHtml(d, flowCommandWithoutChecks(plan.equivalent_command, d.code)) +
        "</span></li>";
    }).join("") + "</ul>" : "") +
    '<p class="chips">' + chip("verification", plan.provenance.verification) + " " + chip("role invocations ≤", plan.limits.max_role_invocations) + " " +
    chip("review pairs ≤", plan.limits.review_pairs) + " " + chip("cost", plan.limits.cost) + "</p>" +
    flowIdentitiesHtml(data) + flowChecksHtml(plan) + flowDisclosuresHtml(data) +
    '<label>Equivalent CLI command <textarea id="flow-command" readonly rows="6">' + esc(plan.equivalent_command) + "</textarea></label>" +
    '<button type="button" class="btn" id="flow-copy">Copy command</button> ' +
    (canStart ? '<button type="button" class="btn btn-primary" id="flow-start">Start flow</button>' :
      '<p class="stat-sub" id="flow-copy-only">Copy-only: ' + esc(flowCopyOnlyReason(data)) + "</p>") +
    '<p class="stat-sub">Plan digest: <code>' + esc(plan.digest) + "</code>. Sources and configuration can change after preview; preview again when they do.</p>";
}

function flowCopyOnlyReason(data) {
  var health = STATE.health || {};
  if (!health.capabilities || !health.capabilities.starter_start) {
    return health.mode === "read-only" ? "this dashboard is read-only." : "this host cannot own a flow start (it needs the Linux supervisor).";
  }
  var first = ((data.authority || {}).diagnostics || [])[0] ||
    ((data.readiness || {}).diagnostics || []).filter(function (d) { return d.level === "error"; })[0];
  if (first && first.code === FLOW_CHECKED_RETRIES) {
    return "a flow with checks cannot retry steps — remove the checks or start it from the CLI without checks.";
  }
  if (first && FLOW_CLI_ONLY.indexOf(first.code) !== -1) {
    return "parallel groups run only from the CLI: " + data.plan.equivalent_command;
  }
  return first ? first.code + ": " + first.fix : "this preview cannot start here.";
}

/* Start once per preview: the same operation id on every retry, so a repeated click or a
 * retried request returns the same operation rather than a second flow. */
function requestFlowStart(state) {
  if (!state.preview || !flowCanStart(state.preview, state)) return Promise.resolve();
  if (!state.operation) state.operation = { id: crypto.randomUUID() };
  var operation = state.operation;
  var stillHere = viewGuard();
  el("flow-operation").innerHTML = '<p class="loading">Durably admitting this operation…</p>';
  return api("/api/scenarios/start", { method: "POST", body: {
    operation_id: operation.id, starter_id: "run-flow", inputs: state.previewInputs, workspace: state.workspace,
    preview_digest: state.preview.plan.digest, authority_digest: state.preview.authority.digest
  }}).then(function (row) {
    // Remembered for this tab before anything else: its approvals now stay alive on any page.
    watchOperation(row.operation_id);
    if (!stillHere() || STATE.flowView !== state || state.operation !== operation) return;
    followFlowOperation(state, row);
  }, function (err) {
    if (!stillHere() || STATE.flowView !== state) throw err;
    if (err.code === "starter.preview_changed" || err.code === "starter.authority_changed") {
      state.preview = null;
      state.operation = null;
      el("flow-operation").innerHTML = starterErrorHtml(err) +
        '<button type="button" class="btn" id="flow-fresh-preview">Create a fresh preview</button>';
      el("flow-fresh-preview").addEventListener("click", function () {
        guardAction(this, function () { return requestFlowPreview(state); }, { busy: "Previewing…" });
      });
      return;
    }
    throw err;
  });
}

/* Admitted: go to the session page with the operation panel. Not yet (admission pending):
 * read the operation until it names its session or settles. */
function followFlowOperation(state, row) {
  if (row.session_id) {
    location.hash = "#/sessions/" + encodeURIComponent(row.session_id) + "?operation=" + encodeURIComponent(row.operation_id);
    return;
  }
  var host = el("flow-operation");
  host.innerHTML = starterOperationHtml(row, [], "flow");
  if (OPERATION_SETTLED.indexOf(row.status) !== -1) return;
  var stillHere = viewGuard();
  setTimeout(function () {
    if (!stillHere() || STATE.flowView !== state) return;
    api("/api/scenario-operations/" + encodeURIComponent(row.operation_id)).then(function (next) {
      if (STATE.flowView === state) followFlowOperation(state, next);
    }, function (err) {
      if (isSuperseded(err) || STATE.flowView !== state) return;
      host.innerHTML = starterErrorHtml(err);
    });
  }, 700);
}

/* --- the operation panel on a session page ----------------------------------
 * #/sessions/<id>?operation=<uuid> (where a Flows start lands): the operation's state and
 * its approvals, answered here. Polling it is the approval heartbeat; it runs while the
 * operation is unsettled, and keeps running in a hidden tab while an approval waits. */

var OPERATION_POLL_MS = 1500;

function startSessionOperation(page, operationId) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(operationId)) return;
  watchOperation(operationId);
  var panel = page.operation = { id: operationId, approvals: [], done: false, grace: null, timeout: null, poller: null };
  panel.poller = new Poller(function () { return pollSessionOperation(page, panel); }, OPERATION_POLL_MS, {
    hiddenInterval: 15000,
    keepAliveHidden: function () { return !panel.done && panel.approvals.length > 0; }
  }).start();
}

function pollSessionOperation(page, panel) {
  if (STATE.sessionPage !== page || page.operation !== panel || panel.done) return;
  return Promise.all([
    api("/api/scenario-operations/" + encodeURIComponent(panel.id)),
    api("/api/approvals?owner=" + encodeURIComponent(panel.id)).catch(function () { return { approvals: [] }; })
  ]).then(function (both) {
    if (STATE.sessionPage !== page || page.operation !== panel) return;
    var row = both[0];
    panel.approvals = both[1].approvals || [];
    panel.settled = both[1].settled || [];
    panel.grace = both[1].grace_seconds || panel.grace;
    panel.timeout = both[1].timeout_seconds || panel.timeout;
    var host = el("session-operation-host");
    if (!host) return;
    if (row.session_id && row.session_id !== page.row.session_id) {
      host.innerHTML = '<div class="notice err" id="session-operation"><div class="notice-title">Operation for another session</div>' +
        '<div class="notice-body">Operation <code>' + esc(panel.id) + "</code> belongs to session <code>" + esc(row.session_id) + "</code>.</div></div>";
      panel.done = true;
      panel.poller.stop();
      return;
    }
    if (OPERATION_SETTLED.indexOf(row.status) !== -1) {
      panel.done = true;
      panel.poller.stop();
      // A settled resume: a later Resume here is a new request with a new id.
      if (row.kind === "resume" && storedResumeId(page.row.session_id) === panel.id) storeResumeId(page.row.session_id, null);
      refreshSessionRow(page);
    }
    host.innerHTML = '<div id="session-operation">' + starterOperationHtml(row, panel.approvals, "session", panel.grace, panel.timeout, panel.settled) + "</div>";
    wireOperationApprovals(host, function () { if (panel.poller) panel.poller.now(); });
  }, function (err) {
    if (isSuperseded(err) || STATE.sessionPage !== page) return;
    var host = el("session-operation-host");
    if (host) host.innerHTML = starterErrorHtml(err);
    // A definitive client error (scope changed, not found) stops; anything else retries.
    if (err.status && err.status < 500) { panel.done = true; panel.poller.stop(); }
  });
}

function stopSessionOperation(page) {
  if (page && page.operation && page.operation.poller) page.operation.poller.stop();
}

/* "Run again": back to the flow's page, workspace preselected. The task is not carried. */
function flowRunAgainHtml(row) {
  if (row.starter_id !== "run-flow" || !row.flow || !row.flow.name) return "";
  var workspace = ((STATE.config && STATE.config.workspaces) || []).find(function (w) { return w.path === row.workspace; });
  if (!workspace) return "";
  return '<a class="btn" id="flow-run-again" href="' + flowHref(row.flow.name, workspace.index) +
    "&from=" + encodeURIComponent(row.session_id) + '">Run again</a>';
}

/* --- the app-level operation watcher ------------------------------------------
 * A flow or run started from this dashboard asks its approvals through the dashboard's
 * broker, owned by its operation id, and the broker denies them ("stopped watching") once
 * nobody has asked about that owner for its grace window (about 90 s). So every operation
 * this tab started or opened (a Flows or Starters start, a session page reached with
 * ?operation=, an ask listed on the Approvals page) is remembered for this tab
 * (sessionStorage) and polled by one poller that survives navigation between pages and
 * keeps running in a hidden tab, as a chat's heartbeat does. It stops for an operation once
 * that settles (or is gone), and sends nothing while the dashboard is unreachable; a
 * reconnect polls at once. Closing the tab stops it: its asks are then denied after the
 * grace window, recorded as "stopped watching", never as anyone's decision. */

var OPERATION_WATCH = { ids: null, poller: null, key: "garuda_operations" };
var OPERATION_WATCH_MS = 5000;
var OPERATION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function watchedOperations() {
  if (OPERATION_WATCH.ids === null) {
    var ids = [];
    try { ids = JSON.parse(sessionStorage.getItem(OPERATION_WATCH.key) || "[]"); } catch (e) { ids = []; }
    OPERATION_WATCH.ids = Array.isArray(ids) ? ids.filter(function (id) { return OPERATION_ID.test(id); }).slice(0, 50) : [];
  }
  return OPERATION_WATCH.ids;
}

function saveWatchedOperations() {
  try { sessionStorage.setItem(OPERATION_WATCH.key, JSON.stringify(watchedOperations())); } catch (e) { /* this page only */ }
}

function watchOperation(id) {
  if (!OPERATION_ID.test(id || "")) return;
  var ids = watchedOperations();
  if (ids.indexOf(id) === -1) { ids.push(id); saveWatchedOperations(); }
  resumeOperationWatch();
}

function unwatchOperation(id) {
  var ids = watchedOperations();
  var at = ids.indexOf(id);
  if (at !== -1) { ids.splice(at, 1); saveWatchedOperations(); }
}

function resumeOperationWatch() {
  if (OPERATION_WATCH.poller || !watchedOperations().length) return;
  OPERATION_WATCH.poller = new Poller(tickOperationWatch, OPERATION_WATCH_MS, {
    hiddenInterval: 15000,
    keepAliveHidden: function () { return watchedOperations().length > 0; }
  }).start();
}

function tickOperationWatch() {
  var ids = watchedOperations().slice();
  if (!ids.length) {
    if (OPERATION_WATCH.poller) { OPERATION_WATCH.poller.stop(); OPERATION_WATCH.poller = null; }
    return;
  }
  if (CONNECTION.down) return;
  var waiting = 0, grace = null;
  return Promise.all(ids.map(function (id) {
    // Both reads name the owner, so each refreshes its heartbeat ({keep: true}: they belong
    // to the app, not to the page on screen).
    return Promise.all([
      api("/api/scenario-operations/" + encodeURIComponent(id), { keep: true }),
      api("/api/approvals?owner=" + encodeURIComponent(id), { keep: true })
    ]).then(function (both) {
      waiting += (both[1].approvals || []).length;
      if (typeof both[1].grace_seconds === "number") grace = both[1].grace_seconds;
      if (OPERATION_SETTLED.indexOf(both[0].status) !== -1) unwatchOperation(id);
    }, function (err) {
      // Gone, out of scope or not this dashboard's: stop. A transient failure retries.
      if (err && err.status && err.status < 500 && err.status !== 429) unwatchOperation(id);
      if (err && err.code === "read_only") unwatchOperation(id);
    });
  })).then(function () {
    // Beat at least three times per grace window, whatever the broker publishes; hidden
    // tabs too, as far as the browser lets their timers run.
    var poller = OPERATION_WATCH.poller;
    if (poller && grace) {
      poller.interval = Math.max(500, Math.min(OPERATION_WATCH_MS, grace * 1000 / 3));
      poller.hiddenInterval = Math.max(poller.interval, Math.min(15000, grace * 1000 / 3));
    }
    if (waiting && typeof refreshInboxBadge === "function") refreshInboxBadge();
  });
}

onReconnect(function () { if (OPERATION_WATCH.poller) OPERATION_WATCH.poller.now(); });

/* --- resuming a dashboard-started flow --------------------------------------
 * A flow this dashboard started resumes only through its owned gateway:
 * POST /api/scenarios/resume {operation_id, session_id}. The read model says which flows
 * those are (`dashboard_operation`) and what the engine's state check says now
 * (`resume_check`); the gateway checks the start's record, plan and authority again when
 * asked. One operation id per flow is kept for this tab (sessionStorage) and reused on a
 * retry, so a repeated click or a lost answer returns the same resume. Its approvals are
 * keyed by that id, and the app-level watcher keeps them alive from any page. */

/* Every refusal of the owned resume: one plain sentence and the next step. `command` is
 * filled with the session id; `transient` refusals keep the operation id for a retry. */
var RESUME_REFUSALS = {
  "flow.resume_invalid": { text: "The resume request was malformed.", next: "Reload this page and try again." },
  "starter.operation_invalid": { text: "The resume request's operation id was not a fresh UUID.", next: "Reload this page and try again." },
  "flow.resume_unknown": { text: "This dashboard's session store has no such flow.", next: "Open the flow from Sessions in the dashboard that started it." },
  "starter.operation_scope_changed": { text: "The flow's workspace left this dashboard's allowlist or changed identity.", next: "Start the dashboard with that workspace allowed (--allow-workspace) and try again." },
  "flow.resume_not_owned": { text: "This flow was not started by a dashboard, so the dashboard cannot resume it.", next: "Resume it from a terminal:", command: "garuda flow resume {id}" },
  "flow.resume_record_mismatch": { text: "The flow's start record is missing or does not match the flow.", next: "Start a new run of the flow." },
  "flow.resume_start_unsettled": { text: "The flow's start operation has not settled yet.", next: "Wait until its start operation is terminal, then resume." },
  "flow.resume_definition_changed": { text: "The flow's definition, roles, checks or configuration changed since it started, so its plan is no longer the one admitted.", next: "Start a new run of the flow (the Flows page previews the current plan)." },
  "flow.resume_authority_changed": { text: "The dashboard's permission ceiling or a role's permissions changed since the flow started.", next: "Restore the ceiling and permissions it started with, or start a new run." },
  "flow.resume_completed": { text: "The flow already completed; there is nothing to resume.", next: "Run it again from the Flows page if you need another run." },
  "flow.recovery_required": { text: "The flow is still recorded running, and the dashboard that ran it is alive or cannot be proved gone.", next: "Stop that dashboard, or run this, then resume:", command: "garuda runtime recover --session {id}" },
  "flow.resume_in_progress": { text: "This flow is already running here, or another resume of it has not finished.", next: "Follow that operation; resume again once it has stopped." },
  "flow.resume_owner_unknown": { text: "Another resume or the start of this flow is unfinished, and its dashboard process can be proved neither alive nor gone.", next: "Make sure that process has exited (its pid is named below), then resume again." },
  "flow.quarantined": { text: "The flow is quarantined: Garuda could not prove a step's process tree closed or its evidence complete, so it is never resumed.", next: "Inspect the workspace (git status) and start a new run." },
  "flow.interrupted_unsettled": { text: "The dashboard stopped without settling a step, and Garuda cannot prove yet that the step's processes are gone, so it is not run again yet.", next: "Do what the engine's reason below names, wait until the crash proof holds (this page checks again), then Resume:", own: true },
  "flow.owner_host_unproved": { text: "The step that never settled ran on another host or pid namespace, or no record says where it ran, so its processes cannot be proved gone from here.", next: "The engine's own reason and next step:", own: true },
  "flow.stop_not_resumable": { text: "The flow stopped in a way a resume does not continue.", next: "Inspect what it did and start a new run." },
  "flow.resume_quota_wait": { text: "The flow is paused on a usage limit that has not reset yet.", next: "Resume it after the reset." },
  "flow.verification_reconciliation_required": { text: "The flow's check or step records are unreadable or disagree, so it is never resumed automatically.", next: "Inspect the session's records; start a new run." },
  "starter.operation_conflict": { text: "This tab's resume operation id already names another request.", next: "Reload this page and resume again (a new id is used)." },
  "starter.ownership_unavailable": { text: "This dashboard cannot own a resume: it runs off Linux, and an owned resume needs the Linux descendant supervisor.", next: "Resume it from a Linux dashboard over the same session store.", transient: true },
  "starter.gateway_closing": { text: "The dashboard is shutting down.", next: "Retry once it is running again.", transient: true },
  "starter.operation_unavailable": { text: "The dashboard's operation store is unavailable for a moment.", next: "Retry.", transient: true },
  "starter.gateway_unavailable": { text: "The dashboard's flow gateway did not answer in time.", next: "Retry; the same operation is reused, so nothing runs twice.", transient: true },
  "read_only": { text: "This dashboard is read-only.", next: "Start the dashboard without --read-only to resume flows.", transient: true }
};

function resumeKey(sessionId) { return "garuda_resume_" + sessionId; }

function storedResumeId(sessionId) {
  try { return sessionStorage.getItem(resumeKey(sessionId)) || null; } catch (e) { return null; }
}

function storeResumeId(sessionId, id) {
  try {
    if (id) sessionStorage.setItem(resumeKey(sessionId), id);
    else sessionStorage.removeItem(resumeKey(sessionId));
  } catch (e) { /* this page only */ }
}

function resumeOperationId(sessionId) {
  var id = storedResumeId(sessionId);
  if (!OPERATION_ID.test(id || "")) { id = crypto.randomUUID(); storeResumeId(sessionId, id); }
  return id;
}

/* A refusal as one sentence, its next step and the command to copy, when there is one. */
function resumeRefusalHtml(code, message, sessionId) {
  var known = RESUME_REFUSALS[code];
  var command = known && known.command ? known.command.replace("{id}", sessionId) : null;
  var pid = code === "flow.resume_owner_unknown" ? (String(message || "").match(/\bpid\s*:?\s*(\d+)/i) || [])[1] : null;
  return '<div class="notice ' + (known && known.transient ? "warn" : "err") + ' resume-refusal" data-code="' + esc(code || "error") + '">' +
    '<div class="notice-title">' + esc(known ? known.text : "The resume was refused.") + "</div>" +
    '<div class="notice-body">' + esc(known ? known.next : "") +
    // A refusal whose message is the answer (what is uncertain, what to do): said in full.
    (known && known.own && message ? '<p class="resume-reason">' + esc(message) + "</p>" : "") +
    (command ? ' <code class="resume-command">' + esc(command) + '</code> <button type="button" class="btn btn-small" data-copy-command="' +
      esc(command) + '">Copy</button>' : "") +
    (pid ? ' Process <code>' + esc(pid) + "</code>." : "") +
    '<br><span class="stat-sub">' + esc(code || "") + (message && !(known && known.own) ? ": " + esc(message) : "") + "</span></div></div>";
}

function resumeStepText(row) {
  var crash = dashboardCrash(row);
  if (crash && crash.step) return "step " + crash.step;
  var stop = (row.flow && row.flow.stop) || {};
  return stop.step ? "step " + stop.step : "the stopped step";
}

/* A dashboard flow whose dashboard died mid-step (the read model's `dashboard_died`), with
 * what the read-only crash proof found (`crash`: the unsettled step and attempt, and the
 * resume check's answer); null for every other row. */
function dashboardCrash(row) {
  var owned = row && row.dashboard_operation;
  return owned && owned.dashboard_died ? (owned.crash || { step: null, attempt: null, resume: null }) : null;
}

/* What a list or a header says for such a flow, never "crashed" or "running": the label
 * and its pill class (paused when the proof holds and Resume continues it). */
var DASHBOARD_DIED = "stopped (dashboard died)";

function dashboardCrashPill(row) {
  var crash = dashboardCrash(row);
  if (!crash) return null;
  var check = crash.resume;
  return { label: DASHBOARD_DIED, cls: check && check.available === true ? "paused" : check && check.available === false ? "failed" : "unknown" };
}

/* Whether that proof holds: the owned resume continues the flow. */
function dashboardCrashResumable(row) {
  var crash = dashboardCrash(row);
  return !!(crash && crash.resume && crash.resume.available === true);
}

/* The one line that says what happened to such a flow, and what the proof found. */
function dashboardCrashHtml(row) {
  var crash = dashboardCrash(row);
  if (!crash) return "";
  var step = crash.step ? "step " + crash.step + (crash.attempt ? " (attempt " + crash.attempt + ")" : "") : "a step";
  var check = crash.resume;
  var found = check && check.available === true ? "; its processes were proved gone, so Resume runs it again."
    : check && check.available === false ? ", and Garuda cannot prove its processes are gone (below)."
    : ", and the dashboard that ran it may still be running (not proved gone yet).";
  return '<p class="resume-crash" id="resume-crash" data-proof="' +
    (check && check.available === true ? "holds" : check && check.available === false ? "fails" : "unknown") + '">' +
    "The dashboard stopped without settling " + esc(step) + esc(found) + "</p>";
}

/* The Resume block of a dashboard-started flow's Result. */
function resumeBlockHtml(row) {
  var check = row.resume_check;
  if (!row.dashboard_operation) return "";
  if (!check) {
    // A flow whose dashboard died, while the read-only proof has no answer: the dashboard
    // that ran it may still be running, or the proof cannot run here. Said, never a button.
    return dashboardCrash(row) ? '<div class="resume-block" id="session-resume" data-available="unknown">' +
      "<h3>Resume from the dashboard</h3>" + dashboardCrashHtml(row) +
      '<p class="stat-sub" id="resume-checking">Checking: whether it can resume is not known yet. This page asks again ' +
      "every few seconds; Resume appears once the crash proof holds.</p></div>" : "";
  }
  var health = STATE.health || {};
  var canStart = !!(health.capabilities && health.capabilities.starter_start);
  var flowName = (row.flow && row.flow.name) || "this flow";
  var what = "Resume runs " + resumeStepText(row) + " of " + flowName + " again, and the steps after it, with the same flow definition, " +
    "roles and permissions its start admitted; completed steps are not run again.";
  var button = function (disabled, note) {
    return '<button type="button" class="btn btn-primary" data-resume-flow="' + esc(row.session_id) + '"' +
      ' data-step="' + esc(resumeStepText(row)) + '" data-flow="' + esc(flowName) + '"' + (disabled ? " disabled" : "") + ">Resume</button>" +
      (note ? ' <span class="stat-sub resume-note">' + esc(note) + "</span>" : "");
  };
  var body;
  var crashLine = dashboardCrashHtml(row);
  if (check.available === true) {
    body = "<p>" + esc(what) + "</p>" + (canStart ? button(false) :
      button(true, health.mode === "read-only" ? "needs a write-mode dashboard" : "needs this dashboard's Linux supervisor"));
  } else if (check.code === "flow.resume_quota_wait") {
    var at = typeof check.reset_at === "number" ? new Date(check.reset_at * 1000) : null;
    body = "<p>" + esc(what) + "</p>" + button(true, at ? "available after " +
      at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + " (the usage limit resets " + untilEpochText(check.reset_at) + ")"
      : "available after the usage limit resets");
  } else if (check.available === false) {
    body = resumeRefusalHtml(check.code, check.message, row.session_id);
  } else {
    body = '<p class="stat-sub">Whether it can resume is unknown here: ' + esc(check.message || "not checked") + "</p>" + button(!canStart);
  }
  var resumed = row.dashboard_operation.resume_operation_id;
  return '<div class="resume-block" id="session-resume" data-available="' + esc(String(check.available)) + '"' +
    (check.code ? ' data-code="' + esc(check.code) + '"' : "") + ">" +
    '<h3>Resume from the dashboard</h3>' + crashLine + body +
    (resumed ? '<p class="stat-sub">Last resumed by operation <a href="#/sessions/' + encodeURIComponent(row.session_id) +
      "?operation=" + encodeURIComponent(resumed) + '"><code>' + esc(resumed.slice(0, 8)) + "</code></a>.</p>" : "") +
    '<div id="session-resume-result" aria-live="polite"></div></div>';
}

/* A small link to that block, for list rows (Overview, Sessions); never a second button. */
function resumeLinkHtml(row) {
  var owned = row && row.dashboard_operation;
  // A flow whose dashboard died is still recorded running: the crash proof decides.
  if (!owned || !(dashboardCrashResumable(row) || (owned.resumable_stop && !isActive(row.state)))) return "";
  return ' <a class="resume-link" data-resume-link href="#/sessions/' + encodeURIComponent(row.session_id) +
    '?focus=resume">Resume</a>';
}

function requestFlowResume(button) {
  var sessionId = button.getAttribute("data-resume-flow");
  var page = STATE.sessionPage;
  var target = el("session-resume-result");
  var id = resumeOperationId(sessionId);
  return guardAction(button, function () {
    if (target) target.innerHTML = '<p class="loading">Durably admitting the resume…</p>';
    return api("/api/scenarios/resume", { method: "POST", body: { operation_id: id, session_id: sessionId } })
      .then(function (row) {
        watchOperation(row.operation_id);
        if (OPERATION_SETTLED.indexOf(row.status) !== -1) storeResumeId(sessionId, null);
        if (target) target.innerHTML = "";
        if (page && STATE.sessionPage === page) {
          setRouteParams({ operation: row.operation_id });
          stopSessionOperation(page);
          startSessionOperation(page, row.operation_id);
          refreshSessionRow(page);
        }
      }, function (err) {
        if (isSuperseded(err)) return;
        var known = RESUME_REFUSALS[err.code];
        var transient = !err.status || err.status >= 500 || (known && known.transient);
        if (!transient) storeResumeId(sessionId, null);  // a refusal: a later attempt is a new request
        var host = el("session-resume-result");
        if (host) host.innerHTML = resumeRefusalHtml(err.code || "network", err.message, sessionId) +
          (transient ? '<p class="stat-sub">Retrying reuses the same operation, so nothing runs twice.</p>' : "");
      });
  }, {
    confirm: "Resume " + button.getAttribute("data-flow") + "? It runs " + button.getAttribute("data-step") +
      " again, then the steps after it, with the same flow definition, roles and permissions its start admitted.",
    busy: "Resuming…"
  });
}

document.addEventListener("click", function (event) {
  var target = event.target && event.target.closest ? event.target : null;
  if (!target) return;
  var resume = target.closest("button[data-resume-flow]");
  if (resume) { requestFlowResume(resume); return; }
  var copy = target.closest(".resume-refusal button[data-copy-command]");
  if (copy) {
    var text = copy.getAttribute("data-copy-command");
    navigator.clipboard.writeText(text).then(function () { toast("Command copied. Run it in your terminal."); },
      function () { toast("Select and copy the command: " + text); });
  }
});
