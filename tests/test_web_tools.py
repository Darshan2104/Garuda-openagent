"""Tests for garuda/tools/web.py: HTML extraction, truncation, URL validation and
source provenance. No external network calls are made: fetches use a local
server or an in-memory transport, and search providers are stubbed at ``_fetch_raw``."""

import hashlib
import io
import json
import re
import time
from difflib import SequenceMatcher
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from urllib.response import addinfourl

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


# --- fetch provenance over the real HTTP stack ---------------------------------
#
# Every fetch below goes through the real opener: pre-flight, redirect re-vetting
# and the pinned connect. Only ``_resolve_and_vet`` is widened, and only for the
# literal 127.0.0.1, exactly as the SSRF regression tests do. The socket itself is
# in memory so the tests also run where localhost listeners are unavailable.

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


class _Reply(io.BytesIO):
    def __init__(self, data, slow):
        super().__init__(data)
        self.slow = slow

    def read(self, *args):
        if self.slow:
            time.sleep(1.0)
        return super().read(*args)


class _FixtureSocket:
    def __init__(self):
        self.request = b""

    def sendall(self, data):
        self.request += data

    def makefile(self, *args):
        path = self.request.split(b" ")[1].decode()
        status, headers, body = ROUTES[path]
        headers = dict(headers)
        slow = body == "slow"
        body = b"late" if slow else body
        headers.setdefault("Content-Length", str(len(body)))
        raw = f"HTTP/1.1 {status} Fixture\r\n".encode()
        raw += "".join(f"{k}: {v}\r\n" for k, v in headers.items()).encode()
        return _Reply(raw + b"\r\n" + body, slow)

    def setsockopt(self, *args):
        pass

    def close(self):
        pass


@pytest.fixture
def site(monkeypatch):
    _allow_loopback(monkeypatch)
    monkeypatch.setattr(web.socket, "create_connection", lambda *a, **kw: _FixtureSocket())
    monkeypatch.setattr(web.urllib.request, "getproxies", lambda: {})
    return "http://127.0.0.1:8080"


async def _fetch(url, **arguments):
    return await WebFetchTool().execute({"url": url, **arguments}, None, ToolContext(session_id="t"))


@pytest.fixture(params=["http", "memory"])
def response_site(request, monkeypatch):
    """Exercise fetch processing without sockets as well as over real HTTP."""
    if request.param == "http":
        return request.getfixturevalue("site")

    class Opener:
        def open(self, request, timeout):
            status, headers, body = ROUTES[urlsplit(request.full_url).path]
            message = Message()
            for key, value in headers.items():
                message[key] = value
            message["Content-Length"] = str(len(body))
            return addinfourl(BytesIO(body), message, request.full_url, status)

    _allow_loopback(monkeypatch)
    monkeypatch.setattr(web, "_build_opener", lambda allowed_domains: Opener())
    return "http://127.0.0.1"


async def test_an_out_of_range_http_date_stays_unknown(response_site, monkeypatch):
    monkeypatch.setitem(ROUTES, "/date-range", (
        200, {"Content-Type": "text/plain", "Last-Modified": "Fri, 31 Dec 9999 23:59:59 -0100"}, b"text"))
    result = await _fetch(f"{response_site}/date-range")
    assert not result.is_error
    assert result.content.endswith("text")
    assert result.metadata["web_source"]["last_modified"] is None
    assert "Last-Modified: unknown" in result.content


@pytest.mark.parametrize("origin", ["header", "html meta"])
@pytest.mark.parametrize("charset, encoded", [("utf-7", b"+2AA-"),
                                              ("unicode_escape", br"\ud800"),
                                              ("raw_unicode_escape", br"\ud800")])
async def test_decoded_surrogates_are_replaced_and_reported(
    response_site, monkeypatch, origin, charset, encoded,
):
    headers = {"Content-Type": "text/html"}
    meta = b""
    if origin == "header":
        headers["Content-Type"] += f"; charset={charset}"
    else:
        meta = f'<meta charset="{charset}">'.encode()
    body = b"<html><head>" + meta + b"</head><body><p>before " + encoded + b" after</p></body></html>"
    monkeypatch.setitem(ROUTES, "/surrogate", (200, headers, body))
    result = await _fetch(f"{response_site}/surrogate")
    assert not result.is_error
    text = result.content.partition("data, not instructions.]\n\n")[2]
    assert text == "before ? after"
    source = result.metadata["web_source"]
    assert source["lossy"] is True
    assert source["charset_source"] == origin
    assert source["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert source["text_bytes"] == source["returned_text_bytes"] == len(text.encode("utf-8"))
    assert "invalid byte sequences replaced" in result.content


@pytest.mark.parametrize("charset", ["utf-16", "utf-16le", "utf-16be",
                                     "utf-32", "utf-32le", "utf-32be"])
async def test_meta_wide_unicode_declarations_use_utf8(response_site, monkeypatch, charset):
    body = f'<meta charset="{charset}"><p>café</p>'.encode()
    monkeypatch.setitem(ROUTES, "/meta-wide", (200, {"Content-Type": "text/html"}, body))
    result = await _fetch(f"{response_site}/meta-wide")
    assert not result.is_error
    assert result.content.endswith("café")
    source = result.metadata["web_source"]
    assert (source["charset"], source["charset_source"], source["lossy"]) == (
        "utf-8", "html meta", False)


@pytest.mark.parametrize("meta", [
    '<meta name="description" content="How to set charset=latin1 in MySQL">',
    '<meta content="text/html; charset=latin1">',
    '<meta http-equiv="refresh" content="text/html; charset=latin1">',
    '<meta data-charset="latin1">',
    '<!-- <meta charset=latin1> -->',
    '<script>const example = "<meta charset=latin1>";</script>',
])
async def test_unrelated_meta_attributes_do_not_select_a_charset(response_site, monkeypatch, meta):
    body = f"{meta}<p>café</p>".encode()
    monkeypatch.setitem(ROUTES, "/meta-description", (200, {"Content-Type": "text/html"}, body))
    result = await _fetch(f"{response_site}/meta-description")
    assert not result.is_error
    assert result.content.endswith("café")
    source = result.metadata["web_source"]
    assert (source["charset"], source["charset_source"], source["lossy"]) == (
        "utf-8", "default", False)


@pytest.mark.parametrize("meta, encoding", [
    ('<META content="text/html; charset=windows-1252" HTTP-EQUIV="Content-Type">', "cp1252"),
    ('<meta http-equiv="content-type" content="text/html; charset=utf-16">', "utf-8"),
    ('<meta name="description" content="charset=latin1"><meta charset=windows-1252>', "cp1252"),
])
async def test_valid_meta_declarations_select_the_charset(response_site, monkeypatch, meta, encoding):
    body = f"{meta}<p>café</p>".encode(encoding)
    monkeypatch.setitem(ROUTES, "/meta-content-type", (200, {"Content-Type": "text/html"}, body))
    result = await _fetch(f"{response_site}/meta-content-type")
    assert not result.is_error
    assert result.content.endswith("café")
    source = result.metadata["web_source"]
    assert (source["charset"], source["charset_source"], source["lossy"]) == (
        encoding, "html meta", False)


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


CORPUS = Path(__file__).parent / "fixtures" / "web"
EXPECTED = json.loads((CORPUS / "expected.json").read_text())


@pytest.mark.parametrize("name", EXPECTED)
async def test_fixed_corpus_extraction_fidelity(site, monkeypatch, name):
    expected = EXPECTED[name]
    monkeypatch.setitem(ROUTES, "/corpus", (200, {"Content-Type": "text/html; charset=utf-8"},
                                         (CORPUS / f"{name}.html").read_bytes()))
    result = await _fetch(f"{site}/corpus")
    assert not result.is_error
    body = result.content.partition("\n\n")[2]
    fidelity = SequenceMatcher(None, expected["text"], body, autojunk=False).ratio()
    assert fidelity == 1.0, f"{name}: character fidelity {fidelity:.3f}"
    sections = result.metadata["web_source"]["sections"]
    assert len(sections) == len(expected["sections"])
    offset = 0
    for actual, target in zip(sections, expected["sections"], strict=True):
        end = offset + len(target["text"])
        assert (actual["title"], actual["heading_level"]) == (target["title"], target["heading_level"])
        assert (actual["start_char"], actual["end_char"]) == (offset, end)
        assert body[offset:end] == target["text"]
        assert actual["omitted_chars"] == 0 and not actual["truncated"]
        offset = end
    assert offset == len(body)


async def test_sections_report_clipped_omitted_and_unknown_content(site, monkeypatch):
    html = "<h1>One</h1><p>café</p><h2>Two</h2><p>later</p>".encode()
    monkeypatch.setitem(ROUTES, "/sections", (200, {"Content-Type": "text/html"}, html))
    result = await _fetch(f"{site}/sections", max_bytes=8)
    source = result.metadata["web_source"]
    assert source["returned_char_range"] == [0, 8]
    first, second = source["sections"]
    assert first["returned_chars"] == 8 and first["omitted_chars"] == 3
    assert first["truncated"] and not first["omitted"]
    assert second["omitted"] and second["omitted_chars"] == len("Two\n\nlater")
    monkeypatch.setitem(ROUTES, "/sections", (200, {"Content-Type": "text/html",
                                                 "Content-Length": "1000"}, html))
    partial = await _fetch(f"{site}/sections")
    first, second = partial.metadata["web_source"]["sections"]
    assert first["end_known"] and first["omitted_chars"] == 0
    assert second["end_known"] is False and second["omitted_chars"] is None
    assert second["truncated"]


async def test_section_titles_and_inventory_are_bounded_without_clipping_the_body(site, monkeypatch):
    html = "<h1>" + "é" * 500 + "</h1>" + "<h2>More</h2><p>body</p>" * 140
    monkeypatch.setitem(ROUTES, "/many", (200, {"Content-Type": "text/html"}, html.encode()))
    result = await _fetch(f"{site}/many", retain=True)
    source = result.metadata["web_source"]
    assert len(source["sections"]) == 128 and source["sections_omitted"] == 13
    assert len(source["sections"][0]["title"]) == 300
    assert source["sections"][0]["title_clipped"]
    assert "é" * 80 + "..." in result.content
    assert source["completeness"] == "complete"
    assert result.content.endswith("More\n\nbody")


async def test_retained_section_inventory_is_discoverable_in_content(site, monkeypatch):
    html = "".join(f"<h2>Topic {i}</h2><p>Details {i}.</p>" for i in range(40))
    monkeypatch.setitem(ROUTES, "/inventory", (200, {"Content-Type": "text/html"}, html.encode()))
    ctx = ToolContext(session_id="inventory")
    tool = WebFetchTool()
    result = await tool.execute({"url": f"{site}/inventory", "retain": True, "max_bytes": 1}, None, ctx)
    assert not result.is_error
    ref = re.search(r"Rendered reference: (web:[0-9a-f]{64})", result.content)[1]

    def no_fetch(*args, **kwargs):
        raise AssertionError("inventory and section reads must not refetch")

    monkeypatch.setattr(web.socket, "create_connection", no_fetch)
    discovered = []
    windows = []
    for _ in range(3):
        assert not result.is_error
        inventory = next(line for line in result.content.splitlines() if line.startswith("Sections ("))
        entries = re.findall(r"(\d+) \[(\d+), (\d+)\) '([^']+)'", inventory)
        assert 0 < len(entries) <= 16
        windows.append(len(entries))
        discovered.extend((int(i), int(start), int(end), title) for i, start, end, title in entries)
        cursor = re.search(r"next sections_from (\d+|None)\.", result.content)
        assert cursor is not None, "the content must expose how to discover the next inventory window"
        if cursor[1] == "None":
            break
        result = await tool.execute({"ref": ref, "sections_from": int(cursor[1]), "max_bytes": 1}, None, ctx)
    else:
        pytest.fail("the inventory must end after all 40 sections have been listed")

    assert windows == [16, 16, 8]
    start = 0
    for i, actual in enumerate(discovered):
        text = f"Topic {i}\n\nDetails {i}." + ("\n\n" if i < 39 else "")
        end = start + len(text)
        assert actual == (i, start, end, f"Topic {i}")
        start = end
    assert len(discovered) == 40
    selected = await tool.execute({"ref": ref, "section": discovered[-1][0], "sections_from": 32}, None, ctx)
    assert not selected.is_error
    assert selected.content.partition("\n\n")[2] == "Topic 39\n\nDetails 39."

    for invalid in (-1, 40, True, 1.5, "16", None):
        denied = await tool.execute({"ref": ref, "sections_from": invalid}, None, ctx)
        assert denied.is_error and "sections_from" in denied.content
    denied = await tool.execute({"url": f"{site}/inventory", "sections_from": 0}, None, ctx)
    assert denied.is_error and "require a retained ref" in denied.content


async def test_retained_body_can_be_paged_by_section_without_fetching_again(site, monkeypatch):
    monkeypatch.setitem(ROUTES, "/article", (200, {"Content-Type": "text/html"},
                                          (CORPUS / "article.html").read_bytes()))
    ctx = ToolContext(session_id="reader")
    tool = WebFetchTool()
    initial = await tool.execute({"url": f"{site}/article", "retain": True, "max_bytes": 4}, None, ctx)
    source = initial.metadata["web_source"]
    ref = source["retention"]["ref"]
    assert ref in initial.content and source["completeness"] == "partial"

    def no_fetch(*args):
        raise AssertionError("a reference must never refetch")

    monkeypatch.setattr(web, "_fetch_page", no_fetch)
    # A fresh tool/context in a later turn still sees this session's snapshot.
    later = WebFetchTool()
    arguments = {"ref": ref, "section": 2, "max_bytes": 5}
    pieces = []
    while True:
        result = await later.execute(arguments, None, ToolContext(session_id="reader"))
        assert not result.is_error
        observed = result.metadata["web_source"]
        start, stop = observed["returned_char_range"]
        pieces.append(EXPECTED["article"]["text"][start:stop])
        assert result.content.partition("\n\n")[2].startswith(pieces[-1])
        if observed["next_offset"] is None:
            break
        arguments["offset"] = observed["next_offset"]
    assert "".join(pieces) == EXPECTED["article"]["sections"][2]["text"]
    whole = await later.execute({"ref": ref}, None, ctx)
    assert whole.content.partition("\n\n")[2] == EXPECTED["article"]["text"]
    assert whole.metadata["web_source"]["completeness"] == "complete"
    ended = await later.execute({"ref": ref, "offset": len(EXPECTED["article"]["text"])}, None, ctx)
    assert ended.content.partition("\n\n")[2] == ""
    assert ended.metadata["web_source"]["next_offset"] is None
    for args, context, message in [
        ({"ref": None}, ctx, "Invalid web reference"),
        ({"ref": ref}, ToolContext(session_id="other"), "missing or expired"),
        ({"ref": "web:" + "0" * 64}, ctx, "missing or expired"),
        ({"ref": ref, "url": f"{site}/doc"}, ctx, "exactly one"),
        ({"ref": ref, "section": 99}, ctx, "section"),
        ({"ref": ref, "offset": -1}, ctx, "offset"),
        ({"ref": ref, "offset": 10000}, ctx, "beyond"),
        ({"ref": ref, "section": 2, "max_bytes": 1}, ctx, "too small"),
        ({"ref": ref}, ToolContext(session_id="reader", tool_options={
            "web_fetch": {"allowed_domains": ["example.com"]}}), "allowed domains"),
    ]:
        denied = await later.execute(args, None, context)
        assert denied.is_error and message in denied.content


@pytest.mark.parametrize("limit", ["max_entries", "max_bytes", "session_entries", "session_bytes"])
async def test_retention_eviction_is_bounded_and_reported(site, monkeypatch, limit):
    from garuda.tools.web_store import WebPageStore

    store = WebPageStore(**{limit: 1 if limit.endswith("entries") else 3500})
    monkeypatch.setattr(web, "_PAGE_STORE", store, raising=False)
    monkeypatch.setitem(ROUTES, "/cache-a", (200, {"Content-Type": "text/plain"}, b"a" * 1500))
    monkeypatch.setitem(ROUTES, "/cache-b", (200, {"Content-Type": "text/plain"}, b"b" * 1500))
    first = await _fetch(f"{site}/cache-a", retain=True)
    ref = first.metadata["web_source"]["retention"]["ref"]
    second = await _fetch(f"{site}/cache-b", retain=True)
    retention = second.metadata["web_source"]["retention"]
    assert retention["status"] == "retained" and retention["ref"] != ref
    assert retention["evicted_refs"] == [ref]
    assert f"Evicted references: {ref}" in second.content
    missing = await WebFetchTool().execute({"ref": ref}, None, ToolContext(session_id="t"))
    assert missing.is_error and "evicted" in missing.content and "no refetch" in missing.content


async def test_oversized_snapshot_is_not_advertised_as_retrievable(site, monkeypatch):
    from garuda.tools.web_store import WebPageStore

    monkeypatch.setattr(web, "_PAGE_STORE", WebPageStore(max_entry_bytes=100), raising=False)
    ordinary = await _fetch(f"{site}/doc")
    assert "retention" not in ordinary.metadata["web_source"]
    result = await _fetch(f"{site}/doc", retain=True)
    assert not result.is_error
    assert result.metadata["web_source"]["retention"]["status"] == "unavailable"
    assert "Rendered reference unavailable" in result.content
    assert result.content.endswith("Version 2\n\nAdds provenance.")


async def test_retained_snapshots_keep_their_body_and_provenance_when_the_url_changes(site, monkeypatch):
    ctx = ToolContext(session_id="snapshot")
    first = await WebFetchTool().execute({"url": f"{site}/doc", "retain": True}, None, ctx)
    original = first.metadata["web_source"]
    ref = original["retention"]["ref"]
    monkeypatch.setitem(ROUTES, "/doc", (200, {"Content-Type": "text/html", "Content-Length": "1000"},
                                      b"<h1>Updated</h1><p>incomplete"))
    changed = await WebFetchTool().execute({"url": f"{site}/doc", "retain": True}, None, ctx)
    new_ref = changed.metadata["web_source"]["retention"]["ref"]
    assert new_ref != ref
    # Mutating a delivered result must not rewrite an already retained snapshot.
    original["title"] = "tampered"
    original["sections"][0]["title"] = "tampered"
    old = await WebFetchTool().execute({"ref": ref}, None, ctx)
    assert old.metadata["web_source"]["title"] == "Release notes"
    assert old.metadata["web_source"]["sections"][0]["title"] == "Version 2"
    assert old.content.endswith("Version 2\n\nAdds provenance.")
    partial = await WebFetchTool().execute({"ref": new_ref}, None, ctx)
    source = partial.metadata["web_source"]
    assert source["completeness"] == "partial" and source["omitted_text_bytes"] is None
    assert source["sections"][0]["omitted_chars"] is None
    assert "unfetched content is unavailable" in partial.content


async def test_global_lru_eviction_preserves_session_isolation(site, monkeypatch):
    from garuda.tools.web_store import WebPageStore

    monkeypatch.setattr(web, "_PAGE_STORE", WebPageStore(max_entries=2), raising=False)
    tool = WebFetchTool()
    alice, bob = ToolContext(session_id="alice"), ToolContext(session_id="bob")
    first = await tool.execute({"url": f"{site}/doc", "retain": True}, None, alice)
    second = await tool.execute({"url": f"{site}/plain", "retain": True}, None, bob)
    a = first.metadata["web_source"]["retention"]["ref"]
    b = second.metadata["web_source"]["retention"]["ref"]
    assert not (await tool.execute({"ref": a}, None, alice)).is_error
    third = await tool.execute({"url": f"{site}/latin", "retain": True}, None, alice)
    assert third.metadata["web_source"]["retention"]["evicted_refs"] == []
    assert not (await tool.execute({"ref": a}, None, alice)).is_error
    evicted = await tool.execute({"ref": b}, None, bob)
    assert evicted.is_error and "evicted" in evicted.content
    foreign = await tool.execute({"ref": b}, None, alice)
    assert foreign.is_error and "missing or expired" in foreign.content


async def test_retained_reference_rechecks_intermediate_redirect_domains(site, monkeypatch):
    hosts = {"source.test", "middle.test", "final.test"}
    monkeypatch.setattr(web, "_resolve_and_vet", lambda host, port=None:
                        (None, "127.0.0.1") if host in hosts else ("unknown fixture host", None))
    monkeypatch.setitem(ROUTES, "/start", (302, {"Location": "http://middle.test/middle"}, b""))
    monkeypatch.setitem(ROUTES, "/middle", (307, {"Location": "http://final.test/doc"}, b""))
    tool = WebFetchTool()
    fetched = await tool.execute({"url": "http://source.test/start", "retain": True}, None,
                                 ToolContext(session_id="chain"))
    ref = fetched.metadata["web_source"]["retention"]["ref"]
    ctx = ToolContext(session_id="chain", tool_options={
        "web_fetch": {"allowed_domains": ["source.test", "final.test"]}})
    denied = await tool.execute({"ref": ref}, None, ctx)
    assert denied.is_error and "current allowed domains" in denied.content


async def test_redirect_chain_records_each_status_and_keeps_validation_and_hop_limit(site, monkeypatch):
    monkeypatch.setitem(ROUTES, "/start", (301, {"Location": "/hop"}, b""))
    result = await _fetch(f"{site}/start")
    assert result.metadata["web_source"]["redirect_chain"] == [
        {"url": f"{site}/start", "status": 301, "next_url": f"{site}/hop"},
        {"url": f"{site}/hop", "status": 302, "next_url": f"{site}/doc"},
        {"url": f"{site}/doc", "status": 200},
    ]
    assert f"{site}/hop (HTTP 302)" in result.content
    monkeypatch.setitem(ROUTES, "/bounce", (302, {"Location": "/finish"}, b""))
    monkeypatch.setitem(ROUTES, "/finish", (303, {"Location": "/bounce"}, b""))
    makefile = _FixtureSocket.makefile

    def finish_at_the_original_url(sock, *args):
        response = makefile(sock, *args)
        if sock.request.startswith(b"GET /finish "):
            monkeypatch.setitem(ROUTES, "/bounce", (200, {"Content-Type": "text/html"}, DOC))
        return response

    monkeypatch.setattr(_FixtureSocket, "makefile", finish_at_the_original_url)
    bounced = await _fetch(f"{site}/bounce")
    source = bounced.metadata["web_source"]
    assert source["requested_url"] == source["final_url"] == f"{site}/bounce"
    assert source["redirected"] is True
    assert [hop["status"] for hop in source["redirect_chain"]] == [302, 303, 200]
    assert "Redirect chain:" in bounced.content
    monkeypatch.setitem(ROUTES, "/outside", (302, {"Location": "https://example.org/doc"}, b""))
    ctx = ToolContext(session_id="t", tool_options={"web_fetch": {"allowed_domains": ["127.0.0.1"]}})
    denied = await WebFetchTool().execute({"url": f"{site}/outside"}, None, ctx)
    assert denied.is_error and "blocked redirect: outside the allowed domains" in denied.content
    for n in range(6):
        monkeypatch.setitem(ROUTES, f"/hop{n}", (302, {"Location": f"/hop{n + 1}"}, b""))
    limited = await _fetch(f"{site}/hop0")
    assert limited.is_error and "redirect hop limit" in limited.content
