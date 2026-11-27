"""Static dashboard assets ship as package data and are served without traversal.

Two failure modes, both silent. A missing package-data glob installs a server that
serves a blank page — the exact bug class `tests/test_harness_robustness.py` already
guards for skills, using the technique reused here. And a renamed JS file leaves the
HTML referencing something that 404s, which also renders blank; nothing else in the
suite would notice, because the frontend has no unit tests by design.
"""

import fnmatch
import json
import re
import shutil
import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from garuda.interfaces.web.http import CONTENT_TYPES, STATIC_DIR, serve_static

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_static_directory_ships_as_package_data():
    """Every file under interfaces/web/static must match a package-data glob."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    globs = pyproject["tool"]["setuptools"]["package-data"]["garuda"]
    package_root = REPO_ROOT / "garuda"
    unmatched = []
    for asset in sorted(STATIC_DIR.iterdir()):
        if not asset.is_file():
            continue
        relative = asset.relative_to(package_root).as_posix()
        if not any(fnmatch.fnmatch(relative, pattern) for pattern in globs):
            unmatched.append(relative)
    assert not unmatched, f"not covered by package-data: {unmatched}"


def test_the_static_directory_is_resolvable_as_installed_package_data():
    """`Path(__file__).parent` works in-tree; this is the check that fails on a wheel."""
    resolved = files("garuda.interfaces.web") / "static"
    assert resolved.is_dir()
    assert any(entry.name == "index.html" for entry in resolved.iterdir())


def test_every_asset_index_html_references_exists():
    """A renamed JS file otherwise produces a silent blank page."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    referenced = re.findall(r'(?:src|href)="/static/([^"]+)"', html)
    assert referenced, "index.html should reference its assets"
    missing = [name for name in referenced if not (STATIC_DIR / name).is_file()]
    assert not missing, f"index.html references missing assets: {missing}"


def test_index_html_carries_no_inline_script_or_style():
    """The CSP forbids both, so an inline block would be silently dropped by the
    browser and the page would half-work in a way no test would catch."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html), "inline <script> is blocked by CSP"
    assert "<style" not in html, "inline <style> is blocked by CSP"
    assert not re.search(r'\sstyle="', html), 'inline style="..." is blocked by CSP'


def test_no_static_file_uses_an_inline_style_attribute():
    """Charts must use presentation attributes and CSS classes, per the CSP."""
    offenders = []
    for asset in STATIC_DIR.glob("*.js"):
        text = asset.read_text(encoding="utf-8")
        if re.search(r'\sstyle\s*=\s*[\'"]', text):
            offenders.append(asset.name)
    assert not offenders, f"inline style attributes are blocked by CSP: {offenders}"


def test_assets_are_served_with_an_explicit_content_type():
    """Not mimetypes.guess_type, whose answer comes from the OS registry and returns
    text/plain for .js on some Windows installs."""
    response = serve_static("core.js", STATIC_DIR)
    assert response.status == 200
    assert response.content_type == CONTENT_TYPES[".js"] == "text/javascript; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert serve_static("app.css", STATIC_DIR).content_type == CONTENT_TYPES[".css"]


@pytest.mark.parametrize(
    "name",
    [
        "../pyproject.toml",
        "..%2fpyproject.toml",
        "../../etc/passwd",
        "a/b.js",
        "/etc/passwd",
        "..",
        ".",
        "",
        "core.js\x00.txt",
    ],
)
def test_traversal_attempts_never_serve_a_file(name):
    response = serve_static(name, STATIC_DIR)
    assert response.status in (400, 404), (name, response.status)


def test_a_symlink_escaping_the_static_dir_is_refused(tmp_path):
    """The name regex cannot express traversal, so this is what the resolved-prefix
    check is actually for: a symlink planted inside the install tree."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve me")
    fake_static = tmp_path / "static"
    fake_static.mkdir()
    (fake_static / "escape.js").symlink_to(secret)

    response = serve_static("escape.js", fake_static)
    assert response.status == 404
    assert b"do not serve me" not in response.body


def test_an_unknown_asset_is_a_404():
    assert serve_static("nope.js", STATIC_DIR).status == 404


def test_no_two_scripts_define_the_same_global_function():
    """The dashboard has no modules: every script shares one global scope, so a function
    defined twice silently replaces the earlier one in whichever file loads last."""
    import re
    from collections import defaultdict

    defined = defaultdict(list)
    for script in sorted(STATIC_DIR.glob("*.js")):
        for name in re.findall(r"^function ([A-Za-z0-9_$]+)\(", script.read_text(), re.M):
            defined[name].append(script.name)
    clashes = {name: files for name, files in defined.items() if len(files) > 1}
    assert not clashes, clashes


def test_session_row_renders_spend_chips_as_escaped_text():
    """Exercise the shipped renderer, with hostile wire values, without adding exports."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the shipped session row renderer")
    script = r"""
const fs = require("fs"), vm = require("vm");
const context = vm.createContext({sessionStorage: {getItem: () => ""}, location: {hash: ""}});
for (const name of ["core.js", "views_sessions.js"]) {
  vm.runInContext(fs.readFileSync(process.argv[1] + "/" + name, "utf8"), context);
}
const hostile = '<img src=x onerror="window.__pwned=1">';
const spend = {state: "recorded", scope: hostile, max_tokens: 400000, max_cost_usd: 2,
  settled_tokens: 80, reserved_tokens: 50, settled_cost_usd: 0.03, cost_status: "unknown",
  cost_unknown_calls: 1, prior_spend_unknown: true, interrupted_calls: 0, unknown_outcome_calls: 0,
  unreported_failed_attempts: 0, estimated_token_calls: 0, refused: 0,
  inherited: {source: hostile, inherited_tokens: null}};
const row = {session_id: "s", label: "completed", state: {}, workspace: null,
  usage: "unknown", cost: "unknown", spend: spend, self_check: {status: "no_evidence"}};
const html = context.sessionRowHtml(row);
const detailHtml = context.sessionResultHtml(row) + context.sessionUsageHtml(row, true);
// Every projected value goes through the same escaping boundary, including counters/caps.
for (const key of Object.keys(spend)) {
  if (!["state", "cost_status", "inherited"].includes(key)) spend[key] = hostile;
}
spend.inherited.inherited_tokens = hostile;
row.task = hostile;
row.name = hostile;
const hostileHtml = context.sessionRowHtml(row);
const hostileDetail = context.sessionUsageHtml(row, true) + context.sessionOverviewHtml(row);
console.log(JSON.stringify({html, hostileHtml, detailHtml, hostileDetail}));
"""
    result = subprocess.run([node, "-e", script, str(STATIC_DIR)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    html, detail = rendered["html"], rendered["detailHtml"]
    assert 'data-chip="self-check"><span class="chip-k">self-check:</span> no_evidence' in html
    assert 'data-chip="self-check"><span class="chip-k">self-check:</span> no_evidence' in detail
    # The list carries the spend gate as one line; every chip stays on the session page.
    assert "spend 80 / 400.0k tokens · ≥ $0.0300 (lower bound) of $2.0000 cap" in html
    assert "data-chip=\"token cap\"" not in html
    assert 'data-chip="token cap"><span class="chip-k">token cap:</span> 400000' in detail
    assert 'data-chip="scope"><span class="chip-k">scope:</span> &lt;img' in detail
    assert 'data-chip="cost"><span class="chip-k">cost:</span> unknown' in detail
    # The chip key is its own span (styled quieter); the text still reads "key: value".
    assert 'known settled USD (lower bound):</span> 0.03' in detail
    assert 'inherited tokens:</span> unknown' in detail
    for hostile in (rendered["hostileHtml"], rendered["hostileDetail"]):
        assert "&lt;img" in hostile
        assert "<img" not in hostile
    assert "<img" not in html and "<img" not in detail


def test_usage_ledger_renders_when_session_snapshots_are_unavailable():
    """A pending or failed sessions request must not hide a successful ledger."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the shipped Usage view")
    script = r"""
const fs = require("fs"), vm = require("vm");
const nodes = {};
const document = {getElementById: id => nodes[id] || (nodes[id] = {
  innerHTML: "", addEventListener: function (event) { this[event] = true; }
})};
const ledger = {note: "ledger loaded", measures: {native_calls: 7, acp_turns: 0,
  total_tokens: 123, cost: {state: "reported", known_usd: 0.5}},
  excluded: {snapshots: 0, reason: "none"},
  median: {tokens_per_native_call: null, native_call_duration_ms: null},
  work_type_share: {unit: "native calls", denominator: 7, excluded_acp_turns: 0, rows: []},
  harness_model: [], daily: [], by_role: [], by_origin: [], by_project: []};
const hostile = '<img src=x onerror="window.__pwned=1">';
let finishSessions;
const sessions = new Promise(resolve => { finishSessions = resolve; });
const response = (ok, status, payload) => ({ok, status, text: () => Promise.resolve(JSON.stringify(payload))});
const context = vm.createContext({sessionStorage: {getItem: () => ""}, location: {hash: ""},
  document, fetch: path => {
    if (path === "/api/usage?range=7d") return Promise.resolve(response(true, 200, ledger));
    if (path === "/api/sessions?limit=200") return sessions;
    throw new Error("unexpected request " + path);
  }});
for (const name of ["core.js", "charts.js", "views_sessions.js", "views_usage.js"]) {
  vm.runInContext(fs.readFileSync(process.argv[1] + "/" + name, "utf8"), context);
}
(async () => {
  const loading = context.usageView("7d");
  await new Promise(setImmediate);
  const whilePending = nodes.view.innerHTML;
  finishSessions(response(false, 503, {error: {message: hostile}}));
  await loading;
  console.log(JSON.stringify({whilePending, html: nodes.view.innerHTML, nodes}));
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, str(STATIC_DIR)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert 'id="tile-native">7' in rendered["whilePending"]
    assert 'id="tile-native">7' in rendered["html"]
    assert "Could not load usage" not in rendered["html"]
    snapshot_html = "".join(node["innerHTML"] for node in rendered["nodes"].values())
    assert "spend snapshots unavailable" in snapshot_html
    assert "No recorded spend snapshots." not in snapshot_html
    assert "&lt;img" in snapshot_html
    assert "<img" not in snapshot_html
    assert rendered["nodes"]["export-csv"]["click"] is True
    assert rendered["nodes"]["export-json"]["click"] is True
