"""Web tools: fetch a URL and search the web, using only the Python stdlib.

Not registered in garuda.tools.__init__ yet; registration is wired separately.
"""

import asyncio
import http.client
import ipaddress
import json
import logging
import os
import socket
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
# Hard ceiling on the whole fetch so a slow-drip server can't hold the turn open
# indefinitely (urlopen's timeout is only per-socket-operation).
TOTAL_FETCH_DEADLINE = 45.0
DEFAULT_MAX_BYTES = 100_000
# Absolute cap so a huge max_bytes can't trigger a multi-hundred-MB read.
MAX_FETCH_BYTES_CAP = 5_000_000
# Redirect hops allowed before the fetch is abandoned. Each hop is re-vetted by
# the SSRF guard; the cap stops a redirect loop from spinning the turn away.
MAX_REDIRECTS = 5
DEFAULT_MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 300


def _address_error(raw: str) -> str | None:
    """Vet one resolved address. Returns a reason string if it must not be reached."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        # An address family we cannot parse is an address we cannot vet.
        return "resolved to an unparseable address"
    # IPv4 tunnelled through IPv6 (::ffff:169.254.169.254, 64:ff9b::/96) is
    # public-looking as an IPv6 address but private once unwrapped.
    candidates = [ip]
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        candidates.append(sixtofour)
    teredo = getattr(ip, "teredo", None)
    if teredo:
        candidates.extend(teredo)
    for candidate in candidates:
        if (
            candidate.is_private or candidate.is_loopback or candidate.is_link_local
            or candidate.is_reserved or candidate.is_multicast or candidate.is_unspecified
        ):
            return (
                f"resolves to a non-public address ({candidate}); "
                "web_fetch is restricted to public internet endpoints"
            )
    return None


def _resolve_and_vet(host: str, port: int | None = None) -> tuple[str | None, str | None]:
    """Resolve ``host`` once and vet **every** address it answers with.

    Returns ``(error, pinned_ip)``. On success ``pinned_ip`` is the literal
    address the caller must connect to — resolving again would reopen the
    rebinding window this function exists to close.

    Fails **closed**: a host that cannot be resolved is refused rather than
    handed to ``urlopen``, because "could not resolve" and "resolved to
    something we would have rejected" are not distinguishable after the fact.
    """
    try:
        infos = socket.getaddrinfo(host, port)
    except socket.gaierror as exc:
        return (f"host {host} could not be resolved ({exc})", None)
    if not infos:
        return (f"host {host} resolved to no addresses", None)
    pinned: str | None = None
    for info in infos:
        raw = info[4][0]
        error = _address_error(raw)
        if error is not None:
            return (f"host {host} {error}", None)
        if pinned is None:
            pinned = raw
    return (None, pinned)


def _ssrf_error(url: str) -> str | None:
    """Pre-flight SSRF check: scheme plus a resolve-and-vet of the host.

    This is the *early rejection* path — it produces a clear refusal before any
    connection is attempted. It is not the authoritative check, because the
    address it vetted is not the address a later ``urlopen`` would connect to.
    Enforcement lives in :class:`_PinnedHTTPSConnection` / :class:`_PinnedHTTPConnection`,
    which vet and connect against a single resolution.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Refusing to fetch {url}: only http and https are allowed."
    host = parsed.hostname
    if not host:
        return "Invalid URL (missing host)."
    error, _ = _resolve_and_vet(host)
    if error is not None:
        return f"Refusing to fetch {url}: {error}."
    return None


class BlockedAddressError(OSError):
    """Raised at connect time when a host resolves somewhere we refuse to reach.

    Subclasses ``OSError`` so urllib wraps it in ``URLError`` and the reason
    text reaches the model instead of a bare traceback.
    """


def _pinning_create_connection(address, *args, **kwargs):
    """``socket.create_connection`` that vets the host and connects to that exact IP.

    This is where the DNS-rebinding window closes. The guard used to resolve the
    host and then hand the *hostname* to ``urlopen``, which resolved it a second
    time — a short-TTL record could answer publicly for the check and
    ``169.254.169.254`` for the connect. Here one resolution is both vetted and
    dialled, so there is no second answer to substitute.
    """
    host, port = address[0], address[1]
    error, pinned = _resolve_and_vet(host, port)
    if error is not None or pinned is None:
        raise BlockedAddressError(f"blocked connection: {error or 'host could not be vetted'}")
    return socket.create_connection((pinned, port, *address[2:]), *args, **kwargs)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that dials a vetted, pinned IP.

    ``self.host`` is deliberately left as the hostname: it supplies the ``Host``
    header, so virtual hosting and the redirect chain keep working. Only the
    socket target is substituted.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _pinning_create_connection


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS variant. ``self.host`` also drives SNI and certificate validation,
    so pinning the socket target does not weaken TLS — the certificate is still
    checked against the hostname the caller asked for."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _pinning_create_connection


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):  # noqa: D102
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):  # noqa: D102
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF guard on every redirect hop.

    Validating only the URL the caller passed is no guard at all: an
    attacker-controlled page need only answer ``302 Location:
    http://169.254.169.254/…`` to have the *host-side* tool read cloud metadata
    and return it into the model's context. urllib follows redirects by default,
    so the check has to live here.

    The pinned connection classes below would refuse the hop anyway; this runs
    first so the refusal names the redirect rather than surfacing as a connect
    error several frames deeper.
    """

    max_repeats = MAX_REDIRECTS
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        error = _ssrf_error(newurl)
        if error is not None:
            raise urllib.error.HTTPError(newurl, code, f"blocked redirect: {error}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener that re-vets every redirect target and pins every connect.

    The pinned handlers are installed ahead of urllib's defaults so no request
    can reach an unvetted socket.

    Proxy handling is left at urllib's default. A proxy is operator
    configuration, not something the model can choose, so it is not an SSRF
    vector — and disabling it would break every deployment that requires one.
    Note that when a proxy *is* configured urllib connects to the proxy rather
    than the target host, so pinning applies to the proxy hostname; the proxy
    itself becomes the trust boundary for the target.
    """
    return urllib.request.build_opener(
        _PinnedHTTPHandler(),
        _PinnedHTTPSHandler(),
        _ValidatingRedirectHandler(),
    )

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
    ssrf = _ssrf_error(url)
    if ssrf:
        return (ssrf, "")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _build_opener().open(request, timeout=REQUEST_TIMEOUT) as response:
            content_type = response.headers.get_content_type()
            if not (content_type.startswith("text/") or content_type in _TEXTUAL_TYPES):
                return (f"Unsupported content type '{content_type}' at {url} (not text).", "")
            charset = response.headers.get_content_charset() or "utf-8"
            # Read a bounded amount: enough raw bytes to fill max_bytes of text
            # even after HTML tag stripping, without slurping huge responses.
            raw = response.read(min(max(max_bytes * 10, 1_000_000), MAX_FETCH_BYTES_CAP * 10))
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
        max_bytes = min(max(1, max_bytes), MAX_FETCH_BYTES_CAP)

        try:
            error, text = await asyncio.wait_for(
                asyncio.to_thread(_blocking_fetch, url.strip(), max_bytes),
                timeout=TOTAL_FETCH_DEADLINE,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return ToolResult(
                tool_call_id="",
                content=f"Timed out fetching {url} after {TOTAL_FETCH_DEADLINE:.0f}s (total deadline).",
                is_error=True,
            )
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
    """Fetch a URL body as text without HTML extraction. Returns (error, body).

    Search endpoints are ours, not the model's, but they still go through the
    validating opener: a redirect from a search host to an internal address would
    otherwise be followed unchecked.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _build_opener().open(request, timeout=REQUEST_TIMEOUT) as response:
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
