"""Tests for garuda/tools/web.py: pure HTML extraction, truncation, and URL
validation. No network calls are made."""

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
