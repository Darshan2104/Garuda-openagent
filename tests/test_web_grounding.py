"""Grounding a conversation: an uploaded file or a fetched page becomes a workspace file.

Three properties carry this feature, and each has a way of being quietly wrong:

* the **filename is rebuilt**, so no input produces a path with a separator in it;
* a **URL is fetched through ``tools/web.py``'s vetted fetcher**, so the dashboard cannot
  reach anything the agent's own ``web_fetch`` is forbidden from reaching;
* the sources are **announced on the next turn, once**, so the agent is told to read them
  exactly when there is a question to answer with them.
"""

import asyncio
import base64
import json

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.web import grounding
from garuda.interfaces.web.grounding import GROUNDING_DIR, Source, SourceError
from garuda.interfaces.web.live import LiveRuns
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request
from tests.test_web_live_runs import (
    PORT,
    TOKEN,
    _FakeSession,
    call,
    payload_of,
)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
async def grounded(store, tmp_path, monkeypatch):
    """An open chat with a fake agent, so a turn can be inspected without a model."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    live = LiveRuns(loop=asyncio.get_running_loop(), store=store, workspaces=(workspace,))
    session = _FakeSession()
    session.release.set()

    async def fake_create(**kwargs):
        return session

    async def fake_env(*args, **kwargs):
        return object(), None

    monkeypatch.setattr("garuda.interfaces.session.AgentSession.create", fake_create)
    monkeypatch.setattr("garuda.interfaces.runner.resolve_environment", fake_env)
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=True,
                           loop=live.loop, live=live)
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    return ctx, live, session, chat_id, workspace


# --- the filename is rebuilt, not validated ----------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("notes.txt", "notes.txt"),
        ("../../etc/passwd", "passwd"),
        ("/etc/shadow", "shadow"),
        ("C:\\Users\\me\\report.pdf", "report.pdf"),
        ("my report (final).pdf", "my_report_final_.pdf"),
        ("..", "source"),
        ("", "source"),
        (None, "source"),
        ("....//..//x", "x"),
    ],
)
def test_a_filename_is_rebuilt_into_one_safe_component(raw, expected):
    """Rebuilt rather than rejected: every run of unacceptable characters collapses, so there
    is no input for which this returns something containing a separator. A browser sending a
    full path is not an attack either — that is what `<input type=file>` does on some
    platforms."""
    name = grounding.safe_name(raw)
    assert name == expected
    assert "/" not in name and "\\" not in name and ".." not in name


def test_a_long_filename_is_clipped_but_keeps_its_shape():
    name = grounding.safe_name("a" * 400 + ".txt")
    assert len(name) <= 120
    assert name.startswith("aaa")


def test_a_url_becomes_a_readable_filename():
    assert grounding.url_filename("https://example.com/docs/guide") == "example.com_docs_guide.md"
    # Called `.md` because the body is extracted text, not the HTML that was served.
    assert grounding.url_filename("https://example.com/").endswith(".md")


def test_a_reader_hint_is_only_given_for_tools_that_exist():
    assert grounding.read_hint("grounding/x.pdf") == "read_pdf"
    assert grounding.read_hint("grounding/x.xlsx") == "read_spreadsheet"
    assert grounding.read_hint("grounding/x.csv") == "read_spreadsheet"
    # No hint means plain `read_file`, which every profile has.
    assert grounding.read_hint("grounding/x.txt") is None


# --- decoding ----------------------------------------------------------------


def test_a_non_base64_body_is_a_named_refusal():
    with pytest.raises(SourceError, match="not valid base64"):
        grounding.decode_upload("this is not base64!!")
    with pytest.raises(SourceError, match="required"):
        grounding.decode_upload(None)
    # base64 of no bytes is the empty string, so an empty file arrives as an absent payload.
    with pytest.raises(SourceError, match="required"):
        grounding.decode_upload(b64(b""))


def test_an_oversized_upload_is_refused_by_its_decoded_size():
    """The HTTP layer caps the request body; this caps what the base64 is allowed to expand
    into, which is the number a user thinks in."""
    with pytest.raises(SourceError, match="over the"):
        grounding.decode_upload(b64(b"x" * (grounding.MAX_SOURCE_BYTES + 1)))


# --- writing into the workspace ----------------------------------------------


async def test_an_uploaded_file_lands_in_the_workspace(tmp_path):
    source = await grounding.store_file(tmp_path, "notes.txt", b64(b"hello there"))
    assert source.path == f"{GROUNDING_DIR}/notes.txt"
    assert (tmp_path / GROUNDING_DIR / "notes.txt").read_bytes() == b"hello there"
    assert source.bytes == 11
    assert source.kind == "file"


async def test_binary_survives_the_round_trip(tmp_path):
    """Written as bytes, not through ``Environment.write_file``, whose signature is ``str``.
    Routing a PDF through it would corrupt exactly the documents this feature exists for."""
    blob = bytes(range(256))
    source = await grounding.store_file(tmp_path, "doc.pdf", b64(blob))
    assert (tmp_path / source.path).read_bytes() == blob
    assert source.reader == "read_pdf"


async def test_a_repeated_name_is_suffixed_not_overwritten(tmp_path):
    """The agent may already have read the first one and be quoting it. Silently swapping the
    bytes under a path it has cited would make its citations wrong with no trace."""
    first = await grounding.store_file(tmp_path, "notes.txt", b64(b"one"))
    second = await grounding.store_file(tmp_path, "notes.txt", b64(b"two"))
    assert first.path != second.path
    assert second.path == f"{GROUNDING_DIR}/notes-2.txt"
    assert (tmp_path / first.path).read_bytes() == b"one"


# --- URLs go through the vetted fetcher --------------------------------------


async def test_a_url_source_uses_the_web_tools_fetcher(tmp_path, monkeypatch):
    """Not a second ``urlopen`` here: ``tools/web.py``'s fetcher resolves the host, refuses
    private addresses and pins the connection to the vetted IP. A separate network path would
    be a proxy into everything ``web_fetch`` is not allowed to reach."""
    seen = {}

    def fake_fetch(url, max_bytes):
        seen["url"] = url
        seen["max_bytes"] = max_bytes
        return (None, "The page body.")

    monkeypatch.setattr("garuda.tools.web._blocking_fetch", fake_fetch)
    source = await grounding.store_url(tmp_path, "https://example.com/guide")
    assert seen["url"] == "https://example.com/guide"
    assert seen["max_bytes"] == grounding.MAX_URL_TEXT_BYTES
    body = (tmp_path / source.path).read_text(encoding="utf-8")
    assert "The page body." in body
    # The provenance is in the file as well as the record: a slugged filename alone cannot
    # answer "where did this claim come from".
    assert "https://example.com/guide" in body
    assert source.origin == "https://example.com/guide"


async def test_a_refused_address_surfaces_the_fetchers_own_message(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "garuda.tools.web._blocking_fetch",
        lambda url, max_bytes: ("Refusing to fetch a private address.", ""),
    )
    with pytest.raises(SourceError, match="private address"):
        await grounding.store_url(tmp_path, "http://169.254.169.254/latest/meta-data/")
    assert not (tmp_path / GROUNDING_DIR).exists(), "a failed fetch must write nothing"


@pytest.mark.parametrize("url", ["", None, "ftp://example.com/x", "not a url", "http://"])
async def test_a_non_http_url_is_refused_before_any_network_call(tmp_path, url, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the fetcher must not be reached for an invalid URL")

    monkeypatch.setattr("garuda.tools.web._blocking_fetch", explode)
    with pytest.raises(SourceError):
        await grounding.store_url(tmp_path, url)


async def test_an_empty_page_is_refused_rather_than_saved(tmp_path, monkeypatch):
    monkeypatch.setattr("garuda.tools.web._blocking_fetch", lambda url, n: (None, "   \n "))
    with pytest.raises(SourceError, match="no readable text"):
        await grounding.store_url(tmp_path, "https://example.com/empty")


# --- the announcement --------------------------------------------------------


def test_the_announcement_names_the_path_and_the_reader():
    text = grounding.announce([
        Source(kind="file", path="grounding/report.pdf", bytes=2048, reader="read_pdf"),
        Source(kind="url", path="grounding/example.com.md", bytes=900,
               origin="https://example.com"),
    ])
    assert "grounding/report.pdf" in text
    assert "read_pdf" in text
    assert "https://example.com" in text
    # Phrased as an instruction: a list of paths with no verb gets ignored about as often as
    # it gets read, and grounding the answer is the entire point.
    assert "Read them before answering" in text


def test_no_sources_means_no_preamble():
    assert grounding.announce([]) == ""


async def test_a_source_is_announced_on_the_next_turn_and_only_once(grounded):
    ctx, live, session, chat_id, workspace = grounded

    added = payload_of(await call(
        ctx, f"/api/chat/{chat_id}/sources", method="POST",
        body={"kind": "file", "name": "spec.txt", "content_b64": b64(b"the spec")},
    ))
    assert added["source"]["path"] == "grounding/spec.txt"
    assert added["pending"] == 1

    # Still pending until a turn carries it.
    listed = payload_of(await call(ctx, f"/api/chat/{chat_id}/sources"))
    assert listed["pending"] == ["grounding/spec.txt"]

    await call(ctx, f"/api/chat/{chat_id}/turn", method="POST",
               body={"task": "What does it say?"})
    prompt = session.tasks[0]
    assert "grounding/spec.txt" in prompt
    assert prompt.endswith("What does it say?")

    assert payload_of(await call(ctx, f"/api/chat/{chat_id}/sources"))["pending"] == []
    await asyncio.sleep(0.05)
    await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "And again?"})
    # The second turn must not re-announce it: the file is already in the conversation, and
    # repeating the preamble every turn is how a context fills up with its own boilerplate.
    assert "grounding/spec.txt" not in session.tasks[1]


async def test_a_url_source_is_reachable_through_the_route(grounded, monkeypatch):
    ctx, live, session, chat_id, workspace = grounded
    monkeypatch.setattr("garuda.tools.web._blocking_fetch", lambda url, n: (None, "Fetched."))
    response = await call(ctx, f"/api/chat/{chat_id}/sources", method="POST",
                          body={"kind": "url", "url": "https://example.com/a"})
    assert response.status == 201
    source = payload_of(response)["source"]
    assert source["kind"] == "url"
    assert (workspace / source["path"]).is_file()


async def test_a_failed_fetch_is_a_502_naming_the_failure(grounded, monkeypatch):
    """The request was well-formed and the upstream failed, which is neither a 400 nor a 500.
    The message is the fetcher's own, so it says *which* address was refused."""
    ctx, live, session, chat_id, workspace = grounded
    monkeypatch.setattr(
        "garuda.tools.web._blocking_fetch",
        lambda url, n: ("HTTP error 404 fetching https://example.com/x: Not Found", ""),
    )
    response = await call(ctx, f"/api/chat/{chat_id}/sources", method="POST",
                          body={"kind": "url", "url": "https://example.com/x"})
    assert response.status == 502
    error = payload_of(response)["error"]
    assert error["code"] == "source_unavailable"
    assert "404" in error["message"]


async def test_an_unknown_source_kind_is_a_400(grounded):
    ctx, live, session, chat_id, workspace = grounded
    response = await call(ctx, f"/api/chat/{chat_id}/sources", method="POST",
                          body={"kind": "telepathy"})
    assert response.status == 400


async def test_sources_for_an_unknown_chat_are_a_404(grounded):
    ctx, live, session, chat_id, workspace = grounded
    assert (await call(ctx, "/api/chat/deadbeef/sources")).status == 404
    response = await call(ctx, "/api/chat/deadbeef/sources", method="POST",
                          body={"kind": "url", "url": "https://example.com"})
    assert response.status == 404


async def test_the_body_cap_clears_an_encoded_max_size_upload():
    """The cap was 1 MiB while the largest legitimate body was a chat message. It has to clear
    ``MAX_SOURCE_BYTES`` plus base64's 4/3 overhead, or the feature refuses exactly the files
    it exists for."""
    from garuda.interfaces.web.security import MAX_REQUEST_BODY_BYTES

    encoded = (grounding.MAX_SOURCE_BYTES + 2) // 3 * 4
    envelope = len(json.dumps({"kind": "file", "name": "x" * 120, "content_b64": ""}))
    assert MAX_REQUEST_BODY_BYTES > encoded + envelope


async def test_an_oversized_body_is_refused_before_it_is_parsed():
    from urllib.parse import parse_qs

    from garuda.interfaces.web.security import MAX_REQUEST_BODY_BYTES, check_request

    request = Request(
        method="POST", path="/api/chat/abc/sources", query=parse_qs(""),
        headers={"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN,
                 "origin": f"http://127.0.0.1:{PORT}"},
        body=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
    )
    refusal = check_request(request, port=PORT, token=TOKEN)
    assert refusal is not None and refusal.status == 413


def test_dispatch_is_not_reachable_without_the_route_being_registered():
    """A guard against the routes table silently losing the sources endpoint in a refactor:
    both methods must be registered, or uploads 404 with no test noticing."""
    from garuda.interfaces.web.routes import _ROUTES

    registered = {(method, pattern.pattern) for method, pattern, _ in _ROUTES}
    sources = r"^/api/chat/(?P<chat_id>[A-Za-z0-9]+)/sources$"
    assert ("GET", sources) in registered
    assert ("POST", sources) in registered
    assert dispatch is not None
