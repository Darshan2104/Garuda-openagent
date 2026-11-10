"""Tests for garuda/tools/web.py: HTML extraction, truncation, URL validation and
source provenance. No external network calls are made: fetches go to a local
server on 127.0.0.1 and search providers are stubbed at ``_fetch_raw``."""

import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from garuda.tools import web
from garuda.tools.protocol import ToolContext
from garuda.tools.web import (
    WebFetchTool,
    WebSearchTool,
    extract_text_from_html,
    format_search_results,
    parse_duckduckgo_results,
    truncate_to_bytes,
    validate_http_url,
)
from garuda.workspace.local import LocalEnvironment
from tests.test_security_correctness_backlog import _allow_loopback


def test_extract_text_strips_scripts_styles_and_tags():
    html = """
    <html><head><title>Ignored</title><style>body { color: red; }</style></head>
    <body>
      <script>alert("evil");</script>
      <h1>Main Heading</h1>
      <p>First paragraph with <b>bold</b> text.</p>
      <div>Second block &amp; entities.</div>
      <noscript>no js</noscript>
    </body></html>
    """
    text = extract_text_from_html(html)
    assert "Main Heading" in text
    assert "First paragraph with bold text." in text
    assert "Second block & entities." in text
    assert "alert" not in text
    assert "color: red" not in text
    assert "no js" not in text
    assert "<" not in text


def test_extract_text_handles_malformed_html():
    text = extract_text_from_html("<p>hello <b>world</p></div></bogus")
    assert "hello" in text and "world" in text


def test_truncate_to_bytes_no_op_when_small():
    assert truncate_to_bytes("short", 100) == "short"


def test_truncate_to_bytes_clips_and_notes():
    text = "a" * 500
    result = truncate_to_bytes(text, 100)
    assert result.startswith("a" * 100)
    assert "[Output truncated to 100 bytes]" in result


def test_truncate_to_bytes_respects_multibyte_boundaries():
    text = "é" * 100  # 2 bytes each in UTF-8
    result = truncate_to_bytes(text, 101)  # would split a character
    truncated_part = result.split("\n\n")[0]
    assert truncated_part == "é" * 50  # partial char dropped, no mojibake


def test_validate_http_url():
    assert validate_http_url("https://example.com/page") is None
    assert validate_http_url("http://example.com") is None
    assert validate_http_url("ftp://example.com") is not None
    assert validate_http_url("file:///etc/passwd") is not None
    assert validate_http_url("not a url") is not None
    assert validate_http_url("") is not None
    assert validate_http_url("https://") is not None


async def test_web_fetch_rejects_bad_urls(tmp_path):
    tool = WebFetchTool()
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="t")
    for bad in ("ftp://example.com/file", "javascript:alert(1)", "", "example.com"):
        result = await tool.execute({"url": bad}, env, ctx)
        assert result.is_error, f"expected error for url: {bad!r}"


async def test_web_search_rejects_empty_query(tmp_path):
    tool = WebSearchTool()
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await tool.execute({"query": "   "}, env, ToolContext(session_id="t"))
    assert result.is_error


def test_parse_duckduckgo_results():
    html = """
    <div class="results">
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=abc">First Result</a>
        <a class="result__snippet" href="#">Snippet about the <b>first</b> result.</a>
      </div>
      <div class="result">
        <a class="result__a" href="https://example.org/two">Second Result</a>
        <a class="result__snippet" href="#">Second snippet.</a>
      </div>
      <div class="result">
        <a class="result__a" href="https://example.net/three">Third Result</a>
      </div>
    </div>
    """
    results = parse_duckduckgo_results(html, max_results=5)
    assert len(results) == 3
    assert results[0]["title"] == "First Result"
    assert results[0]["url"] == "https://example.com/one"
    assert "first" in results[0]["snippet"]
    assert results[1]["url"] == "https://example.org/two"
    assert results[2]["title"] == "Third Result"


def test_parse_duckduckgo_respects_max_results():
    html = "".join(
        f'<a class="result__a" href="https://example.com/{i}">R{i}</a>' for i in range(10)
    )
    results = parse_duckduckgo_results(html, max_results=3)
    assert len(results) == 3


def test_format_search_results_bounded():
    results = [
        {"title": "A  Title", "url": "https://a.example", "snippet": "s" * 1000},
        {"title": "", "url": "https://b.example", "snippet": ""},
    ]
    formatted = format_search_results(results)
    assert "1. A Title" in formatted
    assert "https://a.example" in formatted
    assert "2. (no title)" in formatted
    # Long snippets are clipped.
    assert "s" * 400 not in formatted


def test_tool_metadata_matches_protocol():
    for tool in (WebFetchTool(), WebSearchTool()):
        assert tool.name in ("web_fetch", "web_search")
        assert isinstance(tool.description, str) and tool.description
        assert tool.parameters["type"] == "object"
        assert "required" in tool.parameters


def test_inline_whitespace_between_elements_separates_words():
    """A whitespace-only text node between inline elements is a word break; dropping
    it fused ``<b>hello</b> <i>world</i>`` into one word."""
    assert extract_text_from_html("<p><b>hello</b> <i>world</i></p>") == "hello world"


def test_preformatted_code_keeps_its_indentation():
    html = (
        "<p>Example:</p><pre>\ndef f():\n    <span>return</span> 1\n\n    # done\n</pre>"
        "<p>after   the   block</p>"
    )
    text = extract_text_from_html(html)
    assert "def f():\n    return 1\n\n    # done" in text
    assert "after the block" in text


# --- fetch provenance over a local server ---------------------------------------
#
# Every fetch below goes through the real opener: pre-flight, redirect re-vetting
# and the pinned connect. Only ``_resolve_and_vet`` is widened, and only for the
# literal 127.0.0.1, exactly as the SSRF regression tests do.

_REAL_VET = web._resolve_and_vet
DOC = (b"<html><head><title>  Release   notes </title></head>"
       b"<body><h1>Version 2</h1><p>Adds provenance.</p></body></html>")
LATIN = "<html><head><meta charset=\"windows-1252\"></head><body><p>café</p></body></html>"

# path -> (status, headers, body); a callable body streams its own bytes.
ROUTES = {
    "/doc": (200, {"Content-Type": "text/html; charset=utf-8",
                   "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"}, DOC),
    "/hop": (302, {"Location": "/doc"}, b""),
    "/metadata": (302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b""),
    "/plain": (200, {"Content-Type": "text/plain"}, b"x" * 500),
    "/huge": (200, {"Content-Type": "text/plain"}, b"y" * 1_000_100),
    "/short": (200, {"Content-Type": "text/plain", "Content-Length": "1000"}, b"only this"),
    "/latin": (200, {"Content-Type": "text/html"}, LATIN.encode("cp1252")),
    "/bogus-charset": (200, {"Content-Type": "text/plain; charset=x-not-a-charset"}, b"hi"),
    # Real text codecs that refuse any body with a plain UnicodeError.
    "/undefined-charset": (200, {"Content-Type": "text/plain; charset=undefined"}, b"hi"),
    "/idna-charset": (200, {"Content-Type": "text/plain; charset=idna"}, b"hi \xff"),
    "/broken-utf8": (200, {"Content-Type": "text/plain; charset=utf-8"}, b"ok \xff\xfe end"),
    "/bad-date": (200, {"Content-Type": "text/plain", "Last-Modified": "yesterday"}, b"text"),
    "/slow": (200, {"Content-Type": "text/plain"}, "slow"),
}


class _Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        status, headers, body = ROUTES[self.path]
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if body == "slow":
            self.end_headers()
            self.wfile.flush()
            time.sleep(1.0)
            self.wfile.write(b"late")
            return
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def site(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    _allow_loopback(monkeypatch)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


async def _fetch(url, **arguments):
    return await WebFetchTool().execute({"url": url, **arguments}, None, ToolContext(session_id="t"))


async def test_a_fetch_carries_the_source_identity_of_the_delivered_response(site):
    result = await _fetch(f"{site}/doc")
    assert not result.is_error
    source = result.metadata["web_source"]
    assert source["final_url"] == source["requested_url"] == f"{site}/doc"
    assert source["redirected"] is False
    assert source["status"] == 200
    assert source["content_type"] == "text/html"
    assert (source["charset"], source["charset_source"]) == ("utf-8", "header")
    assert source["title"] == "Release notes"
    assert source["last_modified"] == "2026-10-21T07:28:00Z"
    assert source["retrieved_at"].endswith("Z")
    assert source["body_bytes"] == len(DOC)
    assert source["body_sha256"] == hashlib.sha256(DOC).hexdigest()
    assert source["completeness"] == "complete" and source["omitted_text_bytes"] == 0
    # The header is what reaches the transcript; the page text follows unchanged.
    header, _, body = result.content.partition("\n\n")
    assert header.startswith(f"Source: {site}/doc\n")
    assert f"sha256 {hashlib.sha256(DOC).hexdigest()}" in header
    assert "Completeness: complete" in header
    assert body == "Version 2\n\nAdds provenance."
    # The artifact read model learns the same identity and completeness.
    capture = result.metadata["artifact_source"]
    assert capture["identity"] == f"{site}/doc"
    assert capture["completeness"] == "complete"


async def test_a_redirect_is_recorded_as_requested_versus_final(site):
    result = await _fetch(f"{site}/hop")
    source = result.metadata["web_source"]
    assert source["requested_url"] == f"{site}/hop"
    assert source["final_url"] == f"{site}/doc"
    assert source["redirected"] is True
    assert f"Requested: {site}/hop (redirected)" in result.content


async def test_the_provenance_path_still_refuses_a_redirect_to_metadata(site):
    result = await _fetch(f"{site}/metadata")
    assert result.is_error
    assert "blocked redirect" in result.content and "169.254.169.254" in result.content


async def test_the_provenance_path_still_pins_the_connect(site, monkeypatch):
    """A rubber-stamped pre-flight grants nothing: the connect vets what it dials."""
    monkeypatch.setattr(web, "_resolve_and_vet", _REAL_VET)
    monkeypatch.setattr(web, "_ssrf_error", lambda url: None)
    result = await _fetch(f"{site}/doc")
    assert result.is_error
    assert "blocked connection" in result.content


async def test_a_missing_or_unparseable_date_stays_unknown(site):
    for path in ("/plain", "/bad-date"):
        result = await _fetch(f"{site}{path}")
        assert result.metadata["web_source"]["last_modified"] is None
        assert "Last-Modified: unknown" in result.content


async def test_max_bytes_clipping_is_counted_exactly(site):
    result = await _fetch(f"{site}/plain", max_bytes=100)
    source = result.metadata["web_source"]
    assert source["completeness"] == "partial"
    assert source["incomplete_reasons"] == ["text_max_bytes"]
    assert (source["returned_text_bytes"], source["omitted_text_bytes"]) == (100, 400)
    assert "[Output truncated to 100 bytes]" in result.content
    assert "Completeness: partial (text_max_bytes); 100 text bytes returned, 400 omitted" \
        in result.content
    capture = result.metadata["artifact_source"]
    assert capture["completeness"] == "partial"
    assert capture["omitted_source_bytes"] == 400
    assert capture["reason"] == "tool_output_option_clipped"


async def test_a_body_past_the_read_limit_is_marked_not_dropped_silently(site):
    """The read cap used to discard the rest of the body with no marker at all."""
    # max_bytes of 100_000 sets the smallest read limit, 1_000_000 raw bytes.
    error, text = web._blocking_fetch(f"{site}/huge", 100_000)
    assert error is None
    assert text.endswith("content after that point was not fetched]")

    result = await _fetch(f"{site}/huge", max_bytes=100_000)
    source = result.metadata["web_source"]
    assert source["body_complete"] is False
    assert source["body_bytes"] == source["read_limit_bytes"] == 1_000_000
    assert source["completeness"] == "partial"
    assert source["incomplete_reasons"] == ["body_read_limit", "text_max_bytes"]
    # What lies past the cut was never read, so its size is unknown, not zero.
    assert source["omitted_text_bytes"] is None
    assert "remainder unknown" in result.content
    assert result.metadata["artifact_source"]["omitted_source_bytes"] is None


async def test_a_body_shorter_than_its_content_length_is_partial(site):
    result = await _fetch(f"{site}/short")
    source = result.metadata["web_source"]
    assert source["incomplete_reasons"] == ["body_shorter_than_declared"]
    assert source["declared_length"] == 1000 and source["body_bytes"] == 9
    assert "ended after 9 of the 1000 bytes" in result.content


async def test_an_html_meta_charset_is_honoured_without_a_header_charset(site):
    result = await _fetch(f"{site}/latin")
    assert result.content.endswith("café")
    source = result.metadata["web_source"]
    assert (source["charset"], source["charset_source"]) == ("cp1252", "html meta")


@pytest.mark.parametrize("path, name", [("/bogus-charset", "x-not-a-charset"),
                                        ("/undefined-charset", "undefined"),
                                        ("/idna-charset", "idna")])
async def test_an_unsupported_declared_charset_falls_back_and_says_so(site, path, name):
    """It used to raise LookupError or UnicodeError out of the worker thread."""
    result = await _fetch(f"{site}{path}")
    assert not result.is_error
    source = result.metadata["web_source"]
    assert source["unsupported_charset"] == name
    assert source["charset"] == "utf-8"
    assert f"'{name}' is unsupported" in result.content


async def test_undecodable_bytes_are_flagged_as_lossy(site):
    result = await _fetch(f"{site}/broken-utf8")
    assert result.metadata["web_source"]["lossy"] is True
    assert "invalid byte sequences replaced" in result.content


async def test_a_slow_body_is_bounded_by_the_total_deadline(site, monkeypatch):
    monkeypatch.setattr(web, "TOTAL_FETCH_DEADLINE", 0.2)
    result = await _fetch(f"{site}/slow")
    assert result.is_error and "total deadline" in result.content


def test_the_grounding_fetcher_still_returns_bare_text(site):
    error, text = web._blocking_fetch(f"{site}/doc", 10_000)
    assert error is None
    assert text == "Version 2\n\nAdds provenance."


def test_userinfo_is_not_echoed_into_provenance():
    assert web._source_url("https://user:secret@example.com/a?b=1") == "https://example.com/a?b=1"
    long = "https://example.com/" + "a" * 5000
    assert web._source_url(long).endswith("...[url clipped]")
    assert len(web._source_url(long)) < 2100


# --- search provenance -----------------------------------------------------------

DDG = ('<a class="result__a" href="https://example.com/one">One</a>'
       '<a class="result__snippet" href="#">First.</a>')


async def test_search_results_name_their_provider_and_retrieval_time(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setattr(web, "_fetch_raw", lambda url: (None, DDG))
    result = await WebSearchTool().execute({"query": "q"}, None, ToolContext(session_id="t"))
    info = result.metadata["web_search"]
    assert (info["provider"], info["fallback"], info["status"]) == ("duckduckgo", None, "ok")
    assert info["result_count"] == 1 and info["retrieved_at"].endswith("Z")
    assert result.content.startswith("Search provider: DuckDuckGo HTML (no fallback)")
    assert "1. One\n   https://example.com/one\n   First." in result.content


async def test_a_failing_provider_is_unavailable_not_silently_replaced(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    calls = []

    def fail(url):
        calls.append(url)
        return ("HTTP error 503 from serpapi.com: down", "")

    monkeypatch.setattr(web, "_fetch_raw", fail)
    result = await WebSearchTool().execute({"query": "q"}, None, ToolContext(session_id="t"))
    assert result.is_error
    assert result.metadata["web_search"]["provider"] == "serpapi"
    assert result.metadata["web_search"]["status"] == "unavailable"
    assert len(calls) == 1 and "duckduckgo" not in calls[0]


async def test_a_malformed_serpapi_body_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    monkeypatch.setattr(web, "_fetch_raw", lambda url: (None, "[1, 2]"))
    result = await WebSearchTool().execute({"query": "q"}, None, ToolContext(session_id="t"))
    assert result.is_error and "unexpected response shape" in result.content
