"""Talking to an agent from the browser: the two rules, the turn lifecycle, and the 410.

Read-only routes disclose information; these execute code as the invoking user. So the
security assertions here are about W1 (the workspace allowlist) and W2 (the permission
ceiling) — and specifically about them being *refusals*, not silent clamps, because a caller
who asks for `yolo` and quietly gets `smart` will blame the harness for the approval prompts
they did not expect.

Those two rules, not the ``--read-only`` switch, are what bounds the risk: the dashboard talks
to an agent by default, because a dashboard whose headline feature is off until you find a flag
is a dashboard whose headline feature does not work.
"""

import asyncio
import json

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.jobs import JobManager, JobState
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.interfaces.web.live import (
    DEFAULT_MAX_PERMISSION,
    PERMISSION_RANK,
    ChatSpec,
    LiveRuns,
    SpecError,
)
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request

#: `security.py` reads the header off the lowercased dict rather than naming a constant,
#: so this mirrors that rather than inventing an export.
ORIGIN_HEADER = "origin"

PORT = 8787
TOKEN = "test-token-value"


def request_for(path, method="GET", query="", body=None, origin=True):
    from urllib.parse import parse_qs

    headers = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}
    if origin and method != "GET":
        headers[ORIGIN_HEADER] = f"http://127.0.0.1:{PORT}"
    return Request(
        method=method,
        path=path,
        query=parse_qs(query),
        headers=headers,
        body=json.dumps(body).encode() if body is not None else b"",
    )


async def call(ctx, path, method="GET", query="", body=None, origin=True):
    """Dispatch on a worker thread, as the real server does.

    Not a convenience: write routes marshal onto the agent loop with
    `run_coroutine_threadsafe`, which deadlocks if the caller is already *on* that loop.
    Dispatching from a thread here means these tests exercise the same crossing production
    does — and a test that called dispatch inline would hang rather than fail, which is how
    this was found.
    """
    return await asyncio.to_thread(
        dispatch, request_for(path, method, query, body, origin), ctx
    )


def payload_of(response):
    return json.loads(response.body)


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
def readonly_ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store)


@pytest.fixture
async def live(store, tmp_path):
    """A LiveRuns bound to the loop the test actually runs on.

    An `async def` fixture on purpose. A sync fixture calling `get_event_loop()` gets a
    *different* loop from the one pytest-asyncio runs the test body on, and then every
    write route marshals onto a loop nobody is driving — so each one waits out the full
    30-second `call_on_loop` timeout instead of failing. Twenty tests of that is ten
    minutes of silence, which is exactly how it presented.
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    return LiveRuns(
        loop=asyncio.get_running_loop(),
        store=store,
        workspaces=(workspace,),
        max_permission=DEFAULT_MAX_PERMISSION,
    )


@pytest.fixture
def ctx(store, live):
    return DashboardContext(
        port=PORT, token=TOKEN, store=store, allow_run=True,
        loop=live.loop, live=live,
    )


# --- --read-only turns the whole surface off ---------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/api/chat"), ("GET", "/api/chat"), ("GET", "/api/jobs/abc123"),
     ("POST", "/api/jobs/abc123/cancel"), ("POST", "/api/chat/abc123/turn"),
     ("POST", "/api/chat/abc123/sources"), ("POST", "/api/chat/abc123/stop")],
)
async def test_every_write_route_is_a_503_without_allow_run(readonly_ctx, method, path):
    response = await call(readonly_ctx, path, method=method, body={"task": "x"})
    assert response.status == 503
    assert payload_of(response)["error"]["code"] == "read_only"
    # The message names the flag that caused it, since the default is the other way round.
    assert "--read-only" in payload_of(response)["error"]["message"]


async def test_allow_run_without_a_loop_still_refuses(store):
    """A read-write dashboard with no agent loop attached must not advertise a chat that can
    never take a turn. The capability is off and the route 503s."""
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=True)
    assert ctx.capabilities["chat"] is False
    assert (await call(ctx, "/api/chat", method="POST", body={})).status == 503


async def test_write_mode_advertises_itself_when_it_is_really_available(ctx):
    health = payload_of(await call(ctx, "/api/health"))
    assert health["mode"] == "read-write"
    assert health["capabilities"]["chat"] is True


async def test_the_default_dashboard_can_talk_to_an_agent(tmp_path):
    """The default changed deliberately, so it is pinned. `garuda web` with no flags opens a
    dashboard that can hold a conversation — at the `smart` ceiling, so every destructive tool
    call still waits for a click."""
    config = DashboardConfig(sessions_dir=tmp_path / "s", token=TOKEN)
    assert config.allow_run is True
    ctx = build_context(config, loop=asyncio.get_running_loop())
    assert ctx.capabilities["chat"] is True
    assert ctx.live.max_permission == DEFAULT_MAX_PERMISSION


def test_the_cli_default_is_read_write_and_read_only_is_the_opt_out():
    from garuda.interfaces.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["web"])
    assert args.read_only is False
    assert parser.parse_args(["web", "--read-only"]).read_only is True


# --- the transport gate still applies ----------------------------------------


async def test_a_post_without_an_origin_is_refused(ctx):
    """Absent-Origin GETs are allowed because the Host allowlist already screens plain
    navigation, but a cross-site form POST sends no Origin — so state-changing methods
    require one. This is the check that makes these routes un-CSRF-able."""
    response = await call(ctx, "/api/chat", method="POST", body={}, origin=False)
    assert response.status == 403


async def test_a_post_from_another_origin_is_refused(ctx):
    from urllib.parse import parse_qs

    response = await asyncio.to_thread(
        dispatch,
        Request(method="POST", path="/api/chat", query=parse_qs(""),
                headers={"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN,
                         ORIGIN_HEADER: "http://evil.test"},
                body=b"{}"),
        ctx,
    )
    assert response.status == 403


async def test_a_post_without_the_token_is_refused(ctx):
    from urllib.parse import parse_qs

    response = await asyncio.to_thread(
        dispatch,
        Request(method="POST", path="/api/chat", query=parse_qs(""),
                headers={"host": f"127.0.0.1:{PORT}",
                         ORIGIN_HEADER: f"http://127.0.0.1:{PORT}"},
                body=b"{}"),
        ctx,
    )
    assert response.status == 401


# --- W1: the workspace allowlist ---------------------------------------------


async def test_a_workspace_path_is_refused_in_favour_of_an_index(ctx, tmp_path):
    """A free-form workspace is arbitrary-filesystem-write by design — the agent's whole
    job is to edit files where it is pointed. The error names the actual problem rather
    than reading as a type complaint."""
    response = await call(ctx, "/api/chat", method="POST",
                          body={"workspace": str(tmp_path)})
    assert response.status == 400
    message = payload_of(response)["error"]["message"]
    assert "not a path" in message
    assert "/api/config" in message


async def test_an_out_of_range_workspace_index_is_refused(ctx):
    response = await call(ctx, "/api/chat", method="POST", body={"workspace": 7})
    assert response.status == 400
    assert "0–0" in payload_of(response)["error"]["message"]


async def test_the_configured_workspaces_are_published_by_index(ctx, live):
    config = payload_of(await call(ctx, "/api/config"))
    assert config["workspaces"] == [{"index": 0, "path": str(live.workspaces[0])}]


async def test_a_dashboard_with_no_workspaces_cannot_run_anything(store):
    empty = LiveRuns(loop=asyncio.get_running_loop(), store=store, workspaces=())
    with pytest.raises(SpecError, match="No workspaces"):
        empty.resolve_workspace(0)


async def test_build_context_defaults_the_workspace_to_the_cwd(tmp_path):
    """The default allowlist is exactly one directory — where the dashboard was started —
    so a dashboard with no flags cannot reach outside it."""
    loop = asyncio.get_running_loop()
    ctx = build_context(
        DashboardConfig(allow_run=True, sessions_dir=tmp_path / "s", token=TOKEN), loop=loop
    )
    assert len(ctx.live.workspaces) == 1
    assert ctx.live.max_permission == DEFAULT_MAX_PERMISSION


# --- W2: the permission ceiling ----------------------------------------------


async def test_a_looser_permission_mode_than_the_ceiling_is_refused(ctx):
    response = await call(ctx, "/api/chat", method="POST", body={"permission_mode": "yolo"})
    assert response.status == 400
    message = payload_of(response)["error"]["message"]
    assert "ceiling" in message
    # And it says exactly how to allow it, rather than leaving the operator guessing.
    assert "--max-permission yolo" in message


@pytest.mark.parametrize("mode", ["readonly", "smart"])
def test_a_mode_at_or_below_the_ceiling_is_accepted(live, mode):
    assert live.clamp_permission(mode) == mode


def test_the_ceiling_is_a_maximum_not_a_target(live):
    """An unspecified mode runs at the operator's ceiling, and a stricter request stays
    strict — the ceiling never loosens anything."""
    assert live.clamp_permission(None) == DEFAULT_MAX_PERMISSION
    assert live.clamp_permission("readonly") == "readonly"


async def test_raising_the_ceiling_allows_the_looser_mode(store, tmp_path):
    loose = LiveRuns(loop=asyncio.get_running_loop(), store=store,
                     workspaces=(tmp_path,), max_permission="yolo")
    assert loose.clamp_permission("yolo") == "yolo"
    assert loose.clamp_permission("readonly") == "readonly"


def test_an_unknown_permission_mode_is_refused(live):
    with pytest.raises(SpecError, match="must be one of"):
        live.clamp_permission("wide-open")


def test_the_ranking_covers_every_cli_choice():
    """The CLI's `--permission-mode` choices and the ranking here must agree, or a mode the
    CLI accepts would be rejected as unknown by the ceiling check."""
    from garuda.interfaces.main import build_parser

    parser = build_parser()
    choices = None
    for action in parser._subparsers._group_actions[0].choices["run"]._actions:
        if action.dest == "permission_mode":
            choices = set(action.choices)
    assert choices == set(PERMISSION_RANK)


async def test_config_offers_only_the_modes_that_will_be_accepted(ctx):
    """The composer is built from this, so the user never picks something that 400s."""
    config = payload_of(await call(ctx, "/api/config"))
    assert config["permission_modes"] == ["readonly", "smart"]
    assert config["max_permission"] == "smart"


# --- the spec parser ---------------------------------------------------------


async def test_malformed_json_is_a_400_not_a_500(ctx):
    from urllib.parse import parse_qs

    response = await asyncio.to_thread(
        dispatch,
        Request(method="POST", path="/api/chat", query=parse_qs(""),
                headers={"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN,
                         ORIGIN_HEADER: f"http://127.0.0.1:{PORT}"},
                body=b"{ not json"),
        ctx,
    )
    assert response.status == 400


def test_the_spec_keeps_only_strings_for_the_optional_fields():
    with pytest.raises(SpecError, match="`agent` must be a string"):
        ChatSpec.parse({"agent": 5})
    spec = ChatSpec.parse({"agent": "", "model": None})
    assert (spec.agent, spec.model) == (None, None)


def test_the_spec_needs_no_task():
    """A chat opens empty. It used to require one because the same type validated a one-off
    run launcher, and the chat route passed a placeholder that reached nothing."""
    assert ChatSpec.parse({}).workspace == 0
    assert ChatSpec.parse(None).workspace == 0
    with pytest.raises(SpecError):
        ChatSpec.parse([])


# --- chat lifecycle ----------------------------------------------------------


class _FakeSession:
    """Stands in for AgentSession. The contract under test is the chat plumbing — busy,
    turn indexing, teardown, the reaper — not the agent, so the agent is the part replaced."""

    def __init__(self):
        self.events = __import__("garuda.core.events", fromlist=["EventStore"]).EventStore()
        self.tools = []
        self.config = type("C", (), {"workspace_kind": "local", "docker_image": None})()
        self.model = object()
        self.permissions = object()
        self.agents_dir = None
        self.profile = type("P", (), {"name": "build"})()
        self.closed = False
        self.contexts = []
        self.release = asyncio.Event()
        self.agent = self
        self.runs = 0
        self.tasks = []
        self.run_kwargs = []

    def prepare_context(self, task):
        self.contexts.append(task)
        return object()

    async def run(self, **kwargs):
        self.runs += 1
        self.tasks.append(kwargs.get("task"))
        self.run_kwargs.append(kwargs)
        await self.release.wait()
        from garuda.types import AgentResult

        return AgentResult(success=True, final_message="ok", messages=[], turns=1)

    async def close(self):
        self.closed = True


@pytest.fixture
async def chatting(store, tmp_path, monkeypatch):
    """A LiveRuns whose `start_chat` builds a fake session but real everything else."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    live = LiveRuns(loop=asyncio.get_running_loop(), store=store, workspaces=(workspace,))
    session = _FakeSession()

    async def fake_create(**kwargs):
        return session

    async def fake_env(*args, **kwargs):
        return object(), None

    monkeypatch.setattr("garuda.interfaces.session.AgentSession.create", fake_create)
    monkeypatch.setattr("garuda.interfaces.runner.resolve_environment", fake_env)
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=True,
                           loop=live.loop, live=live)
    return live, session, ctx


async def test_a_chat_opens_with_a_session_and_a_workspace(chatting):
    live, session, ctx = chatting
    payload = payload_of(await call(ctx, "/api/chat", method="POST", body={}))
    assert payload["chat_id"]
    assert payload["session_id"] == session.events.session_id
    assert payload["permission_mode"] == "smart"
    assert payload["busy"] is False
    # The session directory exists, so the trace view can open it immediately.
    assert (store_dir := live.store.session_dir(payload["session_id"])).is_dir(), store_dir


async def test_dashboard_refuses_chat_when_baseline_cannot_be_recorded(chatting, monkeypatch):
    """The held dashboard path must not bypass session-start attribution."""
    live, session, _ctx = chatting
    import garuda.workspace.evidence as evidence
    from garuda.workspace.diff import BaselineError

    def fail_baseline(*_args, **_kwargs):
        raise BaselineError("session metadata unavailable")

    monkeypatch.setattr(evidence, "record_session_baseline", fail_baseline)
    with pytest.raises(BaselineError, match="metadata unavailable"):
        await live.start_chat(ChatSpec())
    assert session.closed is True
    assert live.store.load_meta(session.events.session_id)["status"] == "failed"


async def test_each_turn_verifies_against_the_baseline_recorded_at_open(chatting):
    """A turn's verifier gets the loader bound to the chat's recorded baseline."""
    live, session, ctx = chatting
    payload = payload_of(await call(ctx, "/api/chat", method="POST", body={}))
    meta = live.store.load_meta(payload["session_id"])
    assert meta["baseline_state"] == "unsupported_nonrepo"
    await call(ctx, f"/api/chat/{payload['chat_id']}/turn", method="POST", body={"task": "one"})
    await asyncio.sleep(0.05)
    loader = session.run_kwargs[-1]["workspace_delta_loader"]
    assert callable(loader)
    delta = loader()
    assert delta.attributable is False
    assert delta.to_evidence() == {"attribution": "unsupported_nonrepo"}
    session.release.set()


async def test_closing_a_chat_persists_its_delta_as_finished(chatting):
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    await call(ctx, f"/api/chat/{chat_id}", method="DELETE")
    meta = live.store.load_meta(session.events.session_id)
    assert meta["status"] == "finished"
    assert meta["delta_attribution"] == "unsupported_nonrepo"


async def test_closing_a_chat_with_an_unreadable_delta_is_failed(chatting, monkeypatch):
    """Closing cannot turn missing evidence into a finished chat."""
    live, session, ctx = chatting
    import garuda.workspace.evidence as evidence
    from garuda.workspace.diff import DiffError

    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]

    def broken(*_args, **_kwargs):
        raise DiffError("baseline record disappeared")

    monkeypatch.setattr(evidence, "load_session_delta", broken)
    response = await call(ctx, f"/api/chat/{chat_id}", method="DELETE")
    assert response.status == 200
    meta = live.store.load_meta(session.events.session_id)
    assert meta["status"] == "failed"
    assert meta["workspace_delta_error"] == "DiffError"


async def test_a_turn_is_a_job_readable_by_id(chatting):
    """The turn's job id is returned so the client can watch and cancel it — which is what
    the stop button is, and the reason the job routes outlived the run launcher."""
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    body = payload_of(
        await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "one"})
    )
    assert body["state"] in ("queued", "running")

    job = payload_of(await call(ctx, f"/api/jobs/{body['job_id']}"))
    assert job["job_id"] == body["job_id"]
    assert job["done"] is False

    session.release.set()
    await asyncio.sleep(0.05)
    finished = payload_of(await call(ctx, f"/api/jobs/{body['job_id']}"))
    assert finished["state"] == "succeeded"
    assert (finished["success"], finished["turns"]) == (True, 1)


async def test_a_second_turn_while_one_is_in_flight_is_a_409(chatting):
    """One ContextManager spans the chat and is not concurrency-safe: two turns interleave
    appends and can split a tool_calls/tool_result pair, which providers reject with a 400.
    A 409 rather than a 400 because the request is fine and will work in a moment."""
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]

    first = await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "one"})
    assert first.status == 202
    assert payload_of(first)["turn_index"] == 1

    second = await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "two"})
    assert second.status == 409
    assert payload_of(second)["error"]["code"] == "turn_in_flight"
    assert session.contexts == ["one"], "the refused turn must not have touched the context"

    session.release.set()
    await asyncio.sleep(0.05)
    # And once it finishes, the next turn is accepted.
    third = await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "three"})
    assert third.status == 202
    assert payload_of(third)["turn_index"] == 2


async def test_stopping_a_turn_keeps_the_conversation_open(chatting):
    """The difference from DELETE, and the reason a stop button can exist: the workspace, the
    environment and the history all survive, so the next turn continues where it left off."""
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "long"})

    response = await call(ctx, f"/api/chat/{chat_id}/stop", method="POST")
    assert response.status == 200
    assert payload_of(response)["stopped"] is True

    await asyncio.sleep(0.05)
    assert session.closed is False
    assert (await call(ctx, f"/api/chat/{chat_id}")).status == 200
    # And the chat takes a new turn afterwards.
    session.release.set()
    again = await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "next"})
    assert again.status == 202


async def test_stopping_with_nothing_running_says_so(chatting):
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    payload = payload_of(await call(ctx, f"/api/chat/{chat_id}/stop", method="POST"))
    assert payload["stopped"] is False
    assert "nothing in flight" in payload["reason"]


async def test_stopping_an_unknown_chat_is_a_404(chatting):
    live, session, ctx = chatting
    assert (await call(ctx, "/api/chat/deadbeef/stop", method="POST")).status == 404


async def test_an_empty_turn_is_a_400(chatting):
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    for body in ({}, {"task": ""}, {"task": "   "}):
        response = await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body=body)
        assert response.status == 400


async def test_an_over_long_turn_is_refused(chatting):
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    response = await call(ctx, f"/api/chat/{chat_id}/turn", method="POST",
                          body={"task": "x" * 20_001})
    assert response.status == 400


async def test_closing_a_chat_tears_it_down(chatting):
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]

    response = await call(ctx, f"/api/chat/{chat_id}", method="DELETE")
    assert response.status == 200
    assert payload_of(response)["closed"] is True
    assert session.closed is True
    assert payload_of(await call(ctx, "/api/chat"))["chats"] == []
    # Closing twice is a 404, not a crash.
    assert (await call(ctx, f"/api/chat/{chat_id}", method="DELETE")).status == 404


async def test_an_idle_chat_is_reaped(chatting):
    """A closed tab never sends DELETE, and an abandoned chat holds a container, an MCP
    subprocess and a persistent shell."""
    live, session, ctx = chatting
    live.chat_idle_seconds = 0.05
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]

    await asyncio.sleep(0.1)
    result = await live.reap()
    assert result["closed_chats"] == 1
    assert session.closed is True
    assert (await call(ctx, f"/api/chat/{chat_id}")).status == 404


async def test_a_busy_chat_is_never_reaped(chatting):
    """A turn that is genuinely running is not idle, however long it takes."""
    live, session, ctx = chatting
    live.chat_idle_seconds = 0.05
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "long"})

    await asyncio.sleep(0.1)
    assert (await live.reap())["closed_chats"] == 0
    assert session.closed is False
    session.release.set()
    await asyncio.sleep(0.05)


async def test_touching_a_chat_keeps_it_alive(chatting):
    live, session, ctx = chatting
    live.chat_idle_seconds = 0.25
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    for _ in range(4):
        await asyncio.sleep(0.08)
        assert (await call(ctx, f"/api/chat/{chat_id}")).status == 200
        assert (await live.reap())["closed_chats"] == 0
    assert session.closed is False


async def test_aclose_closes_every_chat(chatting):
    live, session, ctx = chatting
    await call(ctx, "/api/chat", method="POST", body={})
    await live.aclose()
    assert session.closed is True
    assert live.chats == {}


async def test_aclose_cancels_a_turn_still_running(chatting):
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "forever"})

    await live.aclose()
    assert {job.state for job in live.jobs.list()} == {JobState.CANCELLED}


async def test_a_chat_honours_the_permission_ceiling(chatting):
    live, session, ctx = chatting
    response = await call(ctx, "/api/chat", method="POST", body={"permission_mode": "yolo"})
    assert response.status == 400
    assert "ceiling" in payload_of(response)["error"]["message"]


async def test_a_chat_workspace_is_an_index_not_a_path(chatting):
    live, session, ctx = chatting
    response = await call(ctx, "/api/chat", method="POST", body={"workspace": "/etc"})
    assert response.status == 400
    assert "not a path" in payload_of(response)["error"]["message"]


async def test_an_evicted_job_is_a_410_and_the_run_is_still_readable(store, tmp_path,
                                                                    monkeypatch):
    """The elegant part. `JobManager` cannot tell an evicted job from one that never
    existed — but the browser still holds the session_id, and the on-disk session lives
    forever. So the UI's response to a 410 is not an error: it switches to
    /api/runs/<session_id>. Eviction becomes invisible."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    live = LiveRuns(
        loop=asyncio.get_running_loop(),
        store=store,
        workspaces=(workspace,),
        # A tiny positive TTL, not 0.0: `_expired` short-circuits on a falsy retain_seconds,
        # so zero means "never expire" rather than "expire immediately".
        jobs=JobManager(retain_seconds=0.01),
    )
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
    body = payload_of(
        await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "quick"})
    )
    await asyncio.sleep(0.1)   # past the TTL, so the finished job is evictable

    response = await call(ctx, f"/api/jobs/{body['job_id']}")
    assert response.status == 410
    error = payload_of(response)["error"]
    assert error["code"] == "expired"
    # The message points at the fallback rather than just saying "gone".
    assert "/api/runs/" in error["message"]
    # Cancelling an evicted job is the same answer, not a crash.
    assert (await call(ctx, f"/api/jobs/{body['job_id']}/cancel", method="POST")).status == 410
    # And the session it belonged to is still in the list the dashboard serves.
    runs = payload_of(await call(ctx, "/api/runs"))["runs"]
    assert body["session_id"] in [run["session_id"] for run in runs]


async def test_a_chat_persists_to_the_dashboards_own_sessions_root(chatting, store):
    """Without this a browser conversation would write to the default sessions root while
    the dashboard reads from `--sessions-dir` — so it would be invisible in the list beside
    it. A silent divergence, not an error, which is why it is asserted rather than assumed."""
    live, session, ctx = chatting
    payload = payload_of(await call(ctx, "/api/chat", method="POST", body={}))
    assert (store.session_dir(payload["session_id"]) / "meta.json").is_file()
    runs = payload_of(await call(ctx, "/api/runs"))["runs"]
    assert payload["session_id"] in [run["session_id"] for run in runs]


async def test_the_permission_ceiling_reaches_the_engine_not_just_the_config(tmp_path):
    """`PermissionEngine` takes its mode at construction and has no setter, so a caller
    that assigned `config.permission_mode` afterwards would change what the run *reports*
    while leaving what it *enforces* untouched. This asserts they agree."""
    from garuda.agents.setup import prepare_agent_run

    _, config, permissions, _, _, mcp = await prepare_agent_run(
        "build", workspace=str(tmp_path), permission_mode="readonly"
    )
    assert config.permission_mode == "readonly"
    assert permissions.mode == "readonly"
    if mcp is not None:
        await mcp.close()


async def test_run_agent_task_can_be_pointed_at_a_chosen_store(tmp_path):
    """``run_agent_task(store=...)`` exists so a caller with its own ``SessionStore`` — a
    dashboard reading from ``--sessions-dir``, a test, an eval driver — has its run land
    where it will be looked for, rather than in the default root."""
    from garuda.agents.setup import prepare_agent_run
    from garuda.core.events import EventStore
    from garuda.interfaces.runner import run_agent_task
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel

    workspace = tmp_path / "project"
    workspace.mkdir()
    store = SessionStore(root=tmp_path / "chosen")
    profile, config, permissions, tools, agent, mcp = await prepare_agent_run(
        "build", workspace=str(workspace)
    )
    config.max_turns = 1
    result = await run_agent_task(
        task="say nothing",
        model=ScriptModel([ModelResponse(content="Nothing to do here.", tool_calls=[])]),
        agent=agent, tools=tools, config=config, permissions=permissions,
        workspace=str(workspace), events=EventStore(), mcp_manager=mcp, store=store,
    )
    sessions = list((tmp_path / "chosen").glob("*/meta.json"))
    assert len(sessions) == 1, sessions
    assert result.final_message


async def test_the_first_message_names_the_session_in_the_run_list(chatting, store):
    """A chat opens before there is anything to call it, so `store.begin` writes a placeholder.
    Leaving it there gives the run list a column of identical `(dashboard chat)` rows — a list
    you cannot use, which is the opposite of the point."""
    live, session, ctx = chatting
    chat_id = payload_of(await call(ctx, "/api/chat", method="POST", body={}))["chat_id"]
    await call(ctx, f"/api/chat/{chat_id}/turn", method="POST",
               body={"task": "why does the parser drop the last token?"})

    info = payload_of(await call(ctx, f"/api/chat/{chat_id}"))
    assert info["task"] == "why does the parser drop the last token?"
    row = next(r for r in payload_of(await call(ctx, "/api/runs"))["runs"]
               if r["session_id"] == info["session_id"])
    assert row["task"] == "why does the parser drop the last token?"

    # Only the first turn renames it: later messages must not rewrite the run's identity.
    session.release.set()
    await asyncio.sleep(0.05)
    await call(ctx, f"/api/chat/{chat_id}/turn", method="POST", body={"task": "and now?"})
    assert payload_of(await call(ctx, f"/api/chat/{chat_id}"))["task"] == (
        "why does the parser drop the last token?"
    )
