"""Web tools: fetch a URL and search the web, using only the Python stdlib.

Not registered in garuda.tools.__init__ yet; registration is wired separately.
"""

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

USER_AGENT = "Garuda-agent/1.1"
REQUEST_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 100_000
DEFAULT_MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 300

# Content types (besides text/*) we are willing to return as text.
_TEXTUAL_TYPES = {
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/rss+xml",
    "application/atom+xml",
}

_HTML_TYPES = {"text/html", "application/xhtml+xml"}


class _TextExtractor(HTMLParser):
    """Extract readable text from HTML, dropping scripts, styles, and tags."""

    _SKIP_TAGS = {"script", "style", "noscript", "template", "head", "svg", "iframe"}
    _BLOCK_TAGS = {
        "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table",
        "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
        "footer", "nav", "main", "aside", "blockquote", "pre", "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        cleaned: list[str] = []
        blank = False
        for line in lines:
            if line:
                cleaned.append(line)
                blank = False
            elif not blank and cleaned:
                cleaned.append("")
                blank = True
        return "\n".join(cleaned).strip()


def extract_text_from_html(html: str) -> str:
    """Strip tags/scripts/styles from HTML and return readable text."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:
        # html.parser is tolerant, but never let a malformed page crash the tool.
        logger.debug("HTML parsing error; returning best-effort extraction", exc_info=True)
    return extractor.text()


def truncate_to_bytes(text: str, max_bytes: int) -> str:
    """Truncate text to at most max_bytes of UTF-8, appending a note when clipped."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return clipped + f"\n\n[Output truncated to {max_bytes} bytes]"


def validate_http_url(url: str) -> str | None:
    """Return an error message if the URL is not a valid http(s) URL, else None."""
    if not isinstance(url, str) or not url.strip():
        return "A non-empty url is required."
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return f"Unsupported URL scheme '{parsed.scheme or '(none)'}': only http and https are allowed."
    if not parsed.netloc:
        return f"Invalid URL (missing host): {url}"
    return None


def _blocking_fetch(url: str, max_bytes: int) -> tuple[str | None, str]:
    """GET the URL. Returns (error, text). Runs in a worker thread."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            content_type = response.headers.get_content_type()
            if not (content_type.startswith("text/") or content_type in _TEXTUAL_TYPES):
                return (f"Unsupported content type '{content_type}' at {url} (not text).", "")
            charset = response.headers.get_content_charset() or "utf-8"
            # Read a bounded amount: enough raw bytes to fill max_bytes of text
            # even after HTML tag stripping, without slurping huge responses.
            raw = response.read(max(max_bytes * 10, 1_000_000))
    except urllib.error.HTTPError as exc:
        return (f"HTTP error {exc.code} fetching {url}: {exc.reason}", "")
    except urllib.error.URLError as exc:
        return (f"Failed to fetch {url}: {exc.reason}", "")
    except TimeoutError:
        return (f"Timed out fetching {url} after {REQUEST_TIMEOUT:.0f}s.", "")
    except Exception as exc:  # e.g. socket errors, bad ports
        return (f"Failed to fetch {url}: {type(exc).__name__}: {exc}", "")

    text = raw.decode(charset, errors="replace")
    if content_type in _HTML_TYPES:
        text = extract_text_from_html(text)
    return (None, truncate_to_bytes(text, max_bytes))


class WebFetchTool:
    name = "web_fetch"
    description = (
        "Fetch a web page over HTTP(S) and return its readable text content. "
        "HTML is converted to plain text; output is truncated to max_bytes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The http:// or https:// URL to fetch.",
            },
            "max_bytes": {
                "type": "integer",
                "description": f"Maximum bytes of text to return (default {DEFAULT_MAX_BYTES}).",
            },
        },
        "required": ["url"],
    }

    async def execute(
        self,
        arguments: dict[str, Any],
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        url = arguments.get("url", "")
        error = validate_http_url(url)
        if error:
            return ToolResult(tool_call_id="", content=error, is_error=True)
        try:
            max_bytes = int(arguments.get("max_bytes") or DEFAULT_MAX_BYTES)
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_BYTES
        max_bytes = max(1, max_bytes)

        error, text = await asyncio.to_thread(_blocking_fetch, url.strip(), max_bytes)
        if error:
            return ToolResult(tool_call_id="", content=error, is_error=True)
        if not text.strip():
            return ToolResult(
                tool_call_id="",
                content=f"Fetched {url} but extracted no readable text.",
            )
        return ToolResult(tool_call_id="", content=text)


class _DuckDuckGoParser(HTMLParser):
    """Parse the DuckDuckGo HTML results page into (title, url, snippet) tuples."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._in_title_link = False
        self._in_snippet = False

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for key, value in attrs:
            if key == "class" and value:
                return set(value.split())
        return set()

    @staticmethod
    def _href(attrs: list[tuple[str, str | None]]) -> str:
        for key, value in attrs:
            if key == "href" and value:
                return value
        return ""

    @staticmethod
    def _resolve_url(href: str) -> str:
        # DDG links look like //duckduckgo.com/l/?uddg=<encoded-target>&rut=...
        if href.startswith("//"):
            href = "https:" + href
        parsed = urllib.parse.urlparse(href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            query = urllib.parse.parse_qs(parsed.query)
            target = query.get("uddg", [""])[0]
            if target:
                return target
        return href

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "a" and "result__a" in classes:
            if self._current is not None and self._current.get("title"):
                # Previous result had no snippet element; flush it.
                self.results.append(self._current)
            self._current = {"title": "", "url": self._resolve_url(self._href(attrs)), "snippet": ""}
            self._in_title_link = True
        elif "result__snippet" in classes and self._current is not None:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title_link:
            self._in_title_link = False
        elif self._in_snippet and tag in ("a", "div", "span", "td"):
            self._in_snippet = False
            if self._current is not None:
                self.results.append(self._current)
                self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        if self._in_title_link:
            self._current["title"] += data
        elif self._in_snippet:
            self._current["snippet"] += data


def parse_duckduckgo_results(html: str, max_results: int) -> list[dict[str, str]]:
    """Extract search results from DuckDuckGo's HTML endpoint markup."""
    parser = _DuckDuckGoParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.debug("DuckDuckGo HTML parsing error", exc_info=True)
    # Flush a trailing result whose snippet never closed.
    if parser._current is not None and parser._current.get("title"):
        parser.results.append(parser._current)
    return parser.results[:max_results]


def format_search_results(results: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for index, result in enumerate(results, start=1):
        title = " ".join((result.get("title") or "").split()) or "(no title)"
        url = (result.get("url") or "").strip()
        snippet = " ".join((result.get("snippet") or "").split())
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[:MAX_SNIPPET_CHARS] + "..."
        lines.append(f"{index}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else ""))
    return "\n\n".join(lines)


def _blocking_search(query: str, max_results: int) -> tuple[str | None, str]:
    """Run a web search. Returns (error, formatted results). Runs in a thread."""
    api_key = os.environ.get("SERPAPI_API_KEY")
    if api_key:
        params = urllib.parse.urlencode({"q": query, "api_key": api_key})
        url = f"https://serpapi.com/search.json?{params}"
        error, body = _fetch_raw(url)
        if error:
            return (error, "")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return ("SerpAPI returned invalid JSON.", "")
        organic = data.get("organic_results") or []
        results = [
            {
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "snippet": entry.get("snippet", ""),
            }
            for entry in organic[:max_results]
        ]
    else:
        params = urllib.parse.urlencode({"q": query})
        url = f"https://html.duckduckgo.com/html/?{params}"
        error, body = _fetch_raw(url)
        if error:
            return (error, "")
        results = parse_duckduckgo_results(body, max_results)

    if not results:
        return (None, f"No results found for: {query}")
    return (None, format_search_results(results))


def _fetch_raw(url: str) -> tuple[str | None, str]:
    """Fetch a URL body as text without HTML extraction. Returns (error, body)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        return (f"HTTP error {exc.code} from {urllib.parse.urlparse(url).netloc}: {exc.reason}", "")
    except urllib.error.URLError as exc:
        return (f"Search request failed: {exc.reason}", "")
    except TimeoutError:
        return (f"Search request timed out after {REQUEST_TIMEOUT:.0f}s.", "")
    except Exception as exc:
        return (f"Search request failed: {type(exc).__name__}: {exc}", "")
    return (None, raw.decode(charset, errors="replace"))


class WebSearchTool:
    name = "web_search"
    description = (
        "Search the web and return the top results (title, URL, snippet). "
        "Uses SerpAPI when SERPAPI_API_KEY is set, otherwise DuckDuckGo."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "max_results": {
                "type": "integer",
                "description": f"Maximum number of results to return (default {DEFAULT_MAX_RESULTS}).",
            },
        },
        "required": ["query"],
    }

    async def execute(
        self,
        arguments: dict[str, Any],
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        query = (arguments.get("query") or "").strip()
        if not query:
            return ToolResult(
                tool_call_id="", content="A non-empty query is required.", is_error=True
            )
        try:
            max_results = int(arguments.get("max_results") or DEFAULT_MAX_RESULTS)
        except (TypeError, ValueError):
            max_results = DEFAULT_MAX_RESULTS
        max_results = min(max(1, max_results), 20)

        error, text = await asyncio.to_thread(_blocking_search, query, max_results)
        if error:
            return ToolResult(tool_call_id="", content=error, is_error=True)
        return ToolResult(tool_call_id="", content=text)
