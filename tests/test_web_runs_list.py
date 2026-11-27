"""The Runs list covers the whole session store: true totals, store-wide search, paging.

The list used to read only the newest ``MAX_LIMIT`` (500) sessions and cap ``total`` there,
so with more sessions than that the page said "100 of 500" and a search found the matches
among the newest 500 only, reporting that as the total. The fixture sessions are written by
``SessionStore.begin`` once and cloned on disk (only the id, name, task and time differ), so
a store bigger than the old window stays quick to build.
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.web import reads
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request

PORT = 8787
TOKEN = "test-token-value"
COUNT = reads.MAX_LIMIT + 120
#: Every fifth session's task mentions "fibonacci", spread across old and new sessions.
FIB_EVERY = 5


@pytest.fixture
def store(tmp_path):
    store = SessionStore(root=tmp_path / "sessions")
    store.begin("template", task="template", model="m/x", agent="build", workspace="/tmp",
                name="template")
    template = json.loads((store.session_dir("template") / "meta.json").read_text())
    shutil.rmtree(store.session_dir("template"))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(COUNT):
        sid = f"00000000-0000-0000-0000-{i:012d}"
        stamp = (start + timedelta(minutes=i)).isoformat()
        topic = "fibonacci" if i % FIB_EVERY == 0 else "sorting"
        meta = {**template, "session_id": sid, "name": f"run-{i}",
                "task": f"Implement {topic} number {i}", "created_at": stamp, "updated_at": stamp,
                "agent": "plan" if i % 2 else "build"}
        directory = store.session_dir(sid)
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps(meta))
    return store


@pytest.fixture
def ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store)


def get(ctx, query=""):
    response = dispatch(Request(method="GET", path="/api/runs", query=parse_qs(query),
                                headers={"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}), ctx)
    return response.status, json.loads(response.body)


def test_the_total_counts_every_session_not_the_newest_window(ctx):
    status, payload = get(ctx)
    assert status == 200
    assert payload["total"] == COUNT
    assert len(payload["runs"]) == reads.DEFAULT_LIMIT


def test_a_search_finds_matches_older_than_the_newest_window(ctx):
    expected = len(range(0, COUNT, FIB_EVERY))
    _, payload = get(ctx, f"q=fibonacci&limit={reads.MAX_LIMIT}")
    assert payload["total"] == expected
    # The oldest session (index 0) is a match and sits outside the newest 500.
    _, rest = get(ctx, f"q=fibonacci&limit={reads.MAX_LIMIT}&offset={len(payload['runs'])}")
    ids = [row["session_id"] for row in payload["runs"] + rest["runs"]]
    assert len(ids) == expected == len(set(ids))
    assert "00000000-0000-0000-0000-000000000000" in ids


def test_pages_walk_the_whole_list_newest_first_without_gaps_or_repeats(ctx):
    seen = []
    offset = 0
    while True:
        _, payload = get(ctx, f"limit=200&offset={offset}")
        assert payload.get("offset") == offset
        if not payload["runs"]:
            break
        seen.extend(row["session_id"] for row in payload["runs"])
        offset += len(payload["runs"])
    assert len(seen) == COUNT == len(set(seen))
    assert seen[0].endswith(f"{COUNT - 1:012d}") and seen[-1].endswith("000000000000")


def test_a_filter_total_covers_the_whole_store(ctx):
    _, payload = get(ctx, "agent=plan")
    assert payload["total"] == COUNT // 2
    _, both = get(ctx, "agent=build&q=fibonacci")
    assert both["total"] == len(range(0, COUNT, 10))


def test_the_agent_options_cover_the_whole_store(ctx):
    _, payload = get(ctx, "agent=plan&limit=1")
    assert payload.get("agents") == ["build", "plan"]


def test_a_row_carries_the_session_name(ctx):
    _, payload = get(ctx, "limit=1")
    assert payload["runs"][0].get("name") == f"run-{COUNT - 1}"


def test_a_bad_offset_is_a_400(ctx):
    status, payload = get(ctx, "offset=-1")
    assert status == 400 and payload["error"]["code"] == "invalid_request"
