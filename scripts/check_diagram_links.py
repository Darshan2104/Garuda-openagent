"""Check that clickable Mermaid diagram links resolve in the built docs site.

MkDocs validates Markdown links and anchors, but a Mermaid ``click NODE "url"``
target is opaque text to it. This stdlib-only check reads the built site, finds
each diagram's click targets, resolves them the way a browser would from the
page's URL, and fails when the target page or its anchor does not exist.
Remote ``http(s)`` targets are skipped, never fetched.

Usage:
    python scripts/check_diagram_links.py [SITE_DIR]
"""

from __future__ import annotations

import html
import pathlib
import posixpath
import re
import sys
from urllib.parse import urlsplit

MERMAID_BLOCK_RE = re.compile(
    r'<pre class="mermaid"><code>(?P<body>.*?)</code></pre>', re.DOTALL
)
CLICK_RE = re.compile(r'^\s*click\s+\S+\s+(?:href\s+)?"(?P<target>[^"]+)"', re.MULTILINE)


def _page_url(site: pathlib.Path, page: pathlib.Path) -> str:
    rel = page.relative_to(site).as_posix()
    if rel == "index.html":
        return "/"
    if rel.endswith("/index.html"):
        return "/" + rel[: -len("index.html")]
    return "/" + rel


def _target_file(site: pathlib.Path, url_path: str) -> pathlib.Path:
    rel = url_path.lstrip("/")
    if not rel or rel.endswith("/"):
        return site / rel / "index.html"
    if posixpath.splitext(rel)[1]:
        return site / rel
    return site / rel / "index.html"


def find_broken_diagram_links(site: pathlib.Path) -> list[str]:
    errors = []
    for page in sorted(site.rglob("*.html")):
        text = page.read_text(encoding="utf-8", errors="replace")
        page_url = _page_url(site, page)
        rel_page = page.relative_to(site).as_posix()
        for block in MERMAID_BLOCK_RE.finditer(text):
            body = html.unescape(block.group("body"))
            for match in CLICK_RE.finditer(body):
                target = match.group("target")
                parts = urlsplit(target)
                if parts.scheme or parts.netloc:
                    continue
                if parts.path:
                    base = page_url if page_url.endswith("/") else posixpath.dirname(page_url) + "/"
                    resolved = posixpath.normpath(posixpath.join(base, parts.path))
                    if parts.path.endswith("/"):
                        resolved += "/"
                    destination = _target_file(site, resolved)
                else:
                    destination = page
                if not destination.is_file():
                    errors.append(f"{rel_page}: diagram link {target!r} has no page")
                    continue
                if parts.fragment:
                    anchor = f'id="{parts.fragment}"'
                    if anchor not in destination.read_text(encoding="utf-8", errors="replace"):
                        errors.append(
                            f"{rel_page}: diagram link {target!r} has no anchor "
                            f"#{parts.fragment}"
                        )
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    site = pathlib.Path(args[0] if args else "site").resolve()
    if not site.is_dir():
        print(f"diagram link check: site directory not found: {site}", file=sys.stderr)
        return 2
    errors = find_broken_diagram_links(site)
    if errors:
        for error in errors:
            print(error)
        print(f"diagram links FAILED: {len(errors)} problem(s)")
        return 1
    print("diagram links OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
