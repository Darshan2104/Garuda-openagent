/* Usage statistics (F.3): the usage ledger, summarized for a rolling UTC window.
 *
 * Units stay apart: native calls, ACP turns and snapshots are different measures, and a
 * percentage only ever compares native calls with native calls. Unknown cost is shown as
 * unknown, never as $0. The numbers are a report and never a routing input. */

"use strict";

var USAGE_RANGES = ["24h", "7d", "30d"];

/* A tile's figure: the number large, a " known + N …" qualifier under it in the same text. */
function tileFigureHtml(text) {
  var at = String(text).indexOf(" known + ");
  return at < 0 ? esc(text) : esc(text.slice(0, at)) + ' <span class="tile-qual">' + esc(text.slice(at + 1)) + "</span>";
}

function costText(cost) {
  if (!cost || cost.state === "none") return "—";
  if (cost.state === "unknown") return "unknown";
  return fmt.cost(cost.known_usd) + (cost.state === "partial" ? " known + " + cost.unpriced_records + " unpriced" : "");
}

/* Tokens as the ledger recorded them. The statistics sum missing counts as 0, so the server
 * says per row whether its records carried any (`tokens_state`); unknown is never 0. */
function tokensText(m) {
  if (m.tokens_state === "none") return "—";
  if (m.tokens_state === "unknown") return "unknown";
  if (m.tokens_state === "partial") return fmt.tokens(m.total_tokens) + " known + " + m.tokens_unknown + " unrecorded";
  return fmt.tokens(m.total_tokens);
}

/* ACP usage is recorded as adapter snapshots until an adapter version is proved to report
 * per turn, so a 0 here means "not metered", not "no turns ran". */
function acpTurnsTile(s) {
  var reports = s.acp_reports || { snapshots: 0, sessions: 0, per_turn_metered: false };
  var value = reports.per_turn_metered || !reports.snapshots ? String(s.measures.acp_turns) : "not metered";
  var sub = reports.snapshots ? reports.snapshots + " adapter report(s) from " + reports.sessions +
    " session(s): running figures, never summed" : "";
  return '<div class="tile"><div class="tile-label">ACP turns</div><div class="tile-value" id="tile-acp">' + esc(value) + "</div>" +
    (sub ? '<div class="stat-sub" id="tile-acp-sub">' + esc(sub) + "</div>" : "") + "</div>";
}

function usageRangeLinks(active) {
  return USAGE_RANGES.map(function (r) {
    return '<a class="btn' + (r === active ? " btn-primary" : "") + '" data-range="' + r + '" href="#/usage?range=' + r + '">' + r + "</a>";
  }).join(" ");
}

function measureRowHtml(label, m) {
  return "<tr><td>" + label + '</td><td class="num" data-th="Native calls">' + esc(m.native_calls) +
    '</td><td class="num" data-th="ACP turns">' + esc(m.acp_turns) + '</td><td class="num" data-th="Tokens">' +
    esc(tokensText(m)) + '</td><td class="num" data-th="Cost">' + esc(costText(m.cost)) + "</td></tr>";
}

function usageView(range) {
  range = USAGE_RANGES.indexOf(range) >= 0 ? range : "7d";
  render('<div class="loading">Loading usage…</div>');
  return api("/api/usage?range=" + range).then(function (s) {
    var share = s.work_type_share;
    var tableHead = "<thead><tr><th>Name</th><th class=\"num\">Native calls</th><th class=\"num\">ACP turns</th><th class=\"num\">Tokens</th><th class=\"num\">Cost</th></tr></thead>";
    function table(id, rows, nameOf) {
      return '<div class="table-wrap"><table class="table table-stack" id="' + id + '">' + tableHead + "<tbody>" +
        rows.map(function (r) { return measureRowHtml(esc(nameOf(r)), r); }).join("") + "</tbody></table></div>";
    }
    render(
      '<div class="page-head"><h1>Usage</h1>' + usageRangeLinks(range) +
      '<button type="button" class="btn" id="export-csv">Export CSV</button>' +
      '<button type="button" class="btn" id="export-json">Export JSON</button></div>' +
      '<p class="stat-sub" id="usage-note">' + esc(s.note) + " Rolling UTC window of " + esc(range) + ".</p>" +
      '<div class="grid cols-4" id="usage-tiles">' +
      '<div class="tile"><div class="tile-label">Native calls</div><div class="tile-value" id="tile-native">' + esc(s.measures.native_calls) + "</div></div>" +
      acpTurnsTile(s) +
      '<div class="tile"><div class="tile-label">Tokens</div><div class="tile-value" id="tile-tokens">' + tileFigureHtml(tokensText(s.measures)) + "</div></div>" +
      '<div class="tile"><div class="tile-label">Cost</div><div class="tile-value" id="tile-cost">' + tileFigureHtml(costText(s.measures.cost)) + "</div></div></div>" +
      '<p class="stat-sub" id="usage-excluded">Left out: ' + esc(s.excluded.snapshots) + " snapshot(s) — " + esc(s.excluded.reason) +
      ". Median per native call: " + esc(s.median.tokens_per_native_call === null ? "—" : (s.measures.tokens_state === "known" ? fmt.tokens(s.median.tokens_per_native_call) : "unknown (not every call recorded tokens)")) + " tokens, " +
      esc(s.median.native_call_duration_ms === null ? "—" : fmt.duration(s.median.native_call_duration_ms)) + ".</p>" +
      '<div class="card"><h2>Work type share</h2><p class="stat-sub">Share of ' + esc(share.unit) + " (" + esc(share.denominator) + "); " +
      esc(share.excluded_acp_turns) + " ACP turn(s) are a different unit and not in this denominator.</p>" +
      (share.rows.length ? '<table class="table" id="share-table"><tbody>' + share.rows.map(function (r) {
        return "<tr><td>" + esc(r.work_type) + '</td><td class="num">' + esc(r.native_calls) + '</td><td class="num">' +
               (r.share === null ? "—" : esc(Math.round(r.share * 1000) / 10) + "%") + "</td></tr>";
      }).join("") + "</tbody></table>" : '<p class="stat-sub">No native calls in this range.</p>') + "</div>" +
      '<div class="card"><h2>Harness × model</h2><div class="table-wrap"><table class="table table-stack" id="harness-model-table"><thead><tr><th>Harness</th><th>Model</th><th class="num">Native calls</th><th class="num">ACP turns</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead><tbody>' +
      s.harness_model.map(function (r) {
        return '<tr><td data-th="Harness">' + esc(r.harness) + '</td><td data-th="Model">' + esc(r.model) +
               '</td><td class="num" data-th="Native calls">' + esc(r.native_calls) + '</td><td class="num" data-th="ACP turns">' + esc(r.acp_turns) +
               '</td><td class="num" data-th="Tokens">' + esc(tokensText(r)) + '</td><td class="num" data-th="Cost">' + esc(costText(r.cost)) + "</td></tr>";
      }).join("") + "</tbody></table></div></div>" +
      '<div class="card" id="daily-card">' + bars(s.daily.map(function (d) { return { value: d.total_tokens, label: d.date + ": " + tokensText(d) + " tokens" }; }),
        { title: "Tokens per day (UTC)", emptyNote: s.measures.tokens_state === "unknown" ? "Calls in this range recorded no token counts." : "No usage in this range." }) + "</div>" +
      '<div class="card"><h2>By role</h2>' + table("role-table", s.by_role, function (r) { return r.role; }) + "</div>" +
      '<div class="card"><h2>By origin</h2><p class="stat-sub">Who made the calls: the run itself, a subagent, a flow step or a consulted role. These are the original events, so the rows add up to the total.</p>' + table("origin-table", s.by_origin, function (r) { return r.origin; }) + "</div>" +
      '<div class="card"><h2>By project</h2><p class="stat-sub">Projects are opaque ids.</p>' + table("project-table", s.by_project, function (r) { return r.project_id; }) + "</div>" +
      '<div class="card" id="usage-spend"><h2>Session spend limits</h2>' +
      '<p class="stat-sub">Recorded native run-tree snapshots from up to 200 recent sessions, independent of the UTC window. Resumed trees overlap; these rows are not additive. Dollar caps stop on reported spend.</p>' +
      '<div id="usage-spend-snapshots"><p class="stat-sub">Loading spend snapshots…</p></div></div>'
    );
    el("export-csv").addEventListener("click", function () { downloadUsage(range, "csv"); });
    el("export-json").addEventListener("click", function () { downloadUsage(range, "json"); });
    var snapshots = el("usage-spend-snapshots");
    return api("/api/sessions?limit=200").then(function (payload) {
      var sessions = payload.sessions.filter(function (row) { return row.spend !== null; });
      snapshots.innerHTML = sessions.length ? sessions.map(function (row) {
        return '<div><a href="#/sessions/' + encodeURIComponent(row.session_id) + '">' + esc(row.name || row.session_id) +
          '</a><div class="chips">' + spendHtml(row.spend) + "</div></div>";
      }).join("") : '<p class="stat-sub">No recorded spend snapshots.</p>';
    }).catch(function (err) {
      if (isSuperseded(err)) return;
      snapshots.innerHTML = '<p class="stat-sub">' + esc("spend snapshots unavailable") + ": " + esc(err.message) + "</p>";
    });
  }).catch(function (err) {
    if (isSuperseded(err)) return;
    if (err.status === 401) { render(tokenRequiredPanel()); return; }
    render('<div class="notice err"><div class="notice-title">Could not load usage</div><div class="notice-body">' + esc(err.message) + "</div></div>");
  });
}

/* The export is a download, so it goes through fetch (the token travels in a header, which a
 * plain link cannot send) and is handed to the browser as a file. */
function downloadUsage(range, format) {
  return fetch("/api/usage/export?range=" + range + "&format=" + format, { headers: { "X-Garuda-Token": TOKEN } })
    .then(function (response) {
      if (!response.ok) throw new Error("export failed (" + response.status + ")");
      return response.blob();
    }).then(function (blob) {
      var link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "garuda-usage-" + range + "." + format;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
    }).catch(function (err) { if (isSuperseded(err)) return; toast(err.message); });
}
