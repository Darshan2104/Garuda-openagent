/* Usage statistics (F.3): the usage ledger, summarized for a rolling UTC window.
 *
 * Units stay apart: native calls, ACP turns and snapshots are different measures, and a
 * percentage only ever compares native calls with native calls. Unknown cost is shown as
 * unknown, never as $0. The numbers are a report and never a routing input. */

"use strict";

var USAGE_RANGES = ["24h", "7d", "30d"];

function costText(cost) {
  if (!cost || cost.state === "none") return "—";
  if (cost.state === "unknown") return "unknown";
  return fmt.cost(cost.known_usd) + (cost.state === "partial" ? " known + " + cost.unpriced_records + " unpriced" : "");
}

function usageRangeLinks(active) {
  return USAGE_RANGES.map(function (r) {
    return '<a class="btn' + (r === active ? " btn-primary" : "") + '" data-range="' + r + '" href="#/usage?range=' + r + '">' + r + "</a>";
  }).join(" ");
}

function measureRowHtml(label, m) {
  return "<tr><td>" + label + '</td><td class="num">' + esc(m.native_calls) + '</td><td class="num">' + esc(m.acp_turns) +
    '</td><td class="num">' + esc(fmt.tokens(m.total_tokens)) + '</td><td class="num">' + esc(costText(m.cost)) + "</td></tr>";
}

function usageView(range) {
  range = USAGE_RANGES.indexOf(range) >= 0 ? range : "7d";
  render('<div class="loading">Loading usage…</div>');
  return api("/api/usage?range=" + range).then(function (s) {
    var share = s.work_type_share;
    var tableHead = "<thead><tr><th>Name</th><th>Native calls</th><th>ACP turns</th><th>Tokens</th><th>Cost</th></tr></thead>";
    function table(id, rows, nameOf) {
      return '<div class="table-wrap"><table class="table" id="' + id + '">' + tableHead + "<tbody>" +
        rows.map(function (r) { return measureRowHtml(esc(nameOf(r)), r); }).join("") + "</tbody></table></div>";
    }
    render(
      '<div class="page-head"><h1>Usage</h1>' + usageRangeLinks(range) +
      '<button type="button" class="btn" id="export-csv">Export CSV</button>' +
      '<button type="button" class="btn" id="export-json">Export JSON</button></div>' +
      '<p class="stat-sub" id="usage-note">' + esc(s.note) + " Rolling UTC window of " + esc(range) + ".</p>" +
      '<div class="grid cols-4" id="usage-tiles">' +
      '<div class="tile"><div class="tile-label">Native calls</div><div class="tile-value" id="tile-native">' + esc(s.measures.native_calls) + "</div></div>" +
      '<div class="tile"><div class="tile-label">ACP turns</div><div class="tile-value" id="tile-acp">' + esc(s.measures.acp_turns) + "</div></div>" +
      '<div class="tile"><div class="tile-label">Tokens</div><div class="tile-value">' + esc(fmt.tokens(s.measures.total_tokens)) + "</div></div>" +
      '<div class="tile"><div class="tile-label">Cost</div><div class="tile-value" id="tile-cost">' + esc(costText(s.measures.cost)) + "</div></div></div>" +
      '<p class="stat-sub" id="usage-excluded">Left out: ' + esc(s.excluded.snapshots) + " snapshot(s) — " + esc(s.excluded.reason) +
      ". Median per native call: " + esc(s.median.tokens_per_native_call === null ? "—" : fmt.tokens(s.median.tokens_per_native_call)) + " tokens, " +
      esc(s.median.native_call_duration_ms === null ? "—" : fmt.duration(s.median.native_call_duration_ms)) + ".</p>" +
      '<div class="card"><h2>Work type share</h2><p class="stat-sub">Share of ' + esc(share.unit) + " (" + esc(share.denominator) + "); " +
      esc(share.excluded_acp_turns) + " ACP turn(s) are a different unit and not in this denominator.</p>" +
      (share.rows.length ? '<table class="table" id="share-table"><tbody>' + share.rows.map(function (r) {
        return "<tr><td>" + esc(r.work_type) + '</td><td class="num">' + esc(r.native_calls) + '</td><td class="num">' +
               (r.share === null ? "—" : esc(Math.round(r.share * 1000) / 10) + "%") + "</td></tr>";
      }).join("") + "</tbody></table>" : '<p class="stat-sub">No native calls in this range.</p>') + "</div>" +
      '<div class="card"><h2>Harness × model</h2><div class="table-wrap"><table class="table" id="harness-model-table"><thead><tr><th>Harness</th><th>Model</th><th>Native calls</th><th>ACP turns</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>' +
      s.harness_model.map(function (r) {
        return "<tr><td>" + esc(r.harness) + "</td><td>" + esc(r.model) + '</td><td class="num">' + esc(r.native_calls) + '</td><td class="num">' + esc(r.acp_turns) +
               '</td><td class="num">' + esc(fmt.tokens(r.total_tokens)) + '</td><td class="num">' + esc(costText(r.cost)) + "</td></tr>";
      }).join("") + "</tbody></table></div></div>" +
      '<div class="card" id="daily-card">' + bars(s.daily.map(function (d) { return { value: d.total_tokens, label: d.date + ": " + d.total_tokens + " tokens" }; }),
        { title: "Tokens per day (UTC)", emptyNote: "No usage in this range." }) + "</div>" +
      '<div class="card"><h2>By role</h2>' + table("role-table", s.by_role, function (r) { return r.role; }) + "</div>" +
      '<div class="card"><h2>By project</h2><p class="stat-sub">Projects are opaque ids.</p>' + table("project-table", s.by_project, function (r) { return r.project_id; }) + "</div>"
    );
    el("export-csv").addEventListener("click", function () { downloadUsage(range, "csv"); });
    el("export-json").addEventListener("click", function () { downloadUsage(range, "json"); });
  }).catch(function (err) {
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
    }).catch(function (err) { toast(err.message); });
}
