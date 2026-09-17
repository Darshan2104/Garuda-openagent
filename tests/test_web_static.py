"""Static dashboard assets ship as package data and are served without traversal.

Two failure modes, both silent. A missing package-data glob installs a server that
serves a blank page — the exact bug class `tests/test_harness_robustness.py` already
guards for skills, using the technique reused here. And a renamed JS file leaves the
HTML referencing something that 404s, which also renders blank; nothing else in the
suite would notice, because the frontend has no unit tests by design.
"""

import fnmatch
import re
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
