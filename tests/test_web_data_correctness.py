"""Dashboard numbers and states that must not contradict each other or invent zeros.

Each fixture is written by the production writers (``SessionStore``, the flow engine's
receipt layout, the usage ledger's observers, the login probe), then read through the
dashboard's dispatch table, the way the pages read it.
"""

import json

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.sessions import SessionStore
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request
from garuda.runtime.session_state import finished
from garuda.types import AgentResult

PORT = 8787
TOKEN = "test-token-value"


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
def ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store)


def get(ctx, path, query=""):
    from urllib.parse import parse_qs

    response = dispatch(Request(method="GET", path=path, query=parse_qs(query),
                                headers={"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}), ctx)
    assert response.status == 200, response.body
    return json.loads(response.body)


def native_run(store, sid, *, success=True):
    store.begin(sid, task="native", model="openrouter/x/y", agent="build", workspace="/tmp")
    store.finish(sid, AgentResult(success=success, final_message="ok", messages=[], turns=3, metadata={
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}))


def acp_run(store, sid, *, success=True):
    """What ``run_acp_task`` records when an external harness settles (runtime_cli settle):
    ``completed``/``failed``, no turns, no usage, and no ``updated_at`` stamp."""
    store.begin(sid, task="external", model="acp:claude", agent="claude", workspace="/tmp")
    store.update_meta(sid, {"status": "completed" if success else "failed", "verified": False,
                            "state": finished(success=success)})


def by_id(payload):
    return {row["session_id"]: row for row in payload["runs"]}


# --- item 1/2: the run list --------------------------------------------------------------


def test_an_external_harness_run_records_no_turns_tokens_or_duration(ctx, store):
    native_run(store, "n1")
    acp_run(store, "a1")
    rows = by_id(get(ctx, "/api/runs"))
    assert rows["n1"]["turns"] == 3 and rows["n1"]["total_tokens"] == 120
    acp = rows["a1"]
    # begin() wrote turns 0 and created_at == updated_at; neither is a measurement.
    assert acp["turns"] is None
    assert acp["prompt_tokens"] is None and acp["completion_tokens"] is None
    assert acp["total_tokens"] is None
    assert acp["duration_ms"] is None
    assert acp["state"]["outcome"] == "completed"


def test_status_filter_matches_the_outcome_across_writers(ctx, store):
    native_run(store, "n1")
    native_run(store, "n2", success=False)
    acp_run(store, "a1")
    completed = {r["session_id"] for r in get(ctx, "/api/runs", "status=completed")["runs"]}
    assert completed == {"n1", "a1"}
    legacy = {r["session_id"] for r in get(ctx, "/api/runs", "status=success")["runs"]}
    assert legacy == {"n1", "a1"}
    assert {r["session_id"] for r in get(ctx, "/api/runs", "status=failed")["runs"]} == {"n2"}


def test_a_flow_duration_comes_from_its_step_receipts(ctx, store):
    store.begin("f1", task="flow", model="m", agent="build", workspace="/tmp")
    created = store.load_meta("f1")["created_at"]
    # A pre-#763 flow: its own updated_at never advanced past begin().
    store.update_meta("f1", {"kind": "flow", "flow": {"name": "review-loop", "steps": ["code"]},
                             "flow_state": "completed", "status": "completed",
                             "state": finished(success=True), "updated_at": created})
    receipts = store.session_dir("f1") / "flow" / "receipts"
    receipts.mkdir(parents=True)
    from datetime import datetime, timedelta

    start = datetime.fromisoformat(created)
    (receipts / "code-1.json").write_text(json.dumps({
        "step": "code", "attempt": 1, "started_at": (start + timedelta(seconds=1)).isoformat(),
        "finished_at": (start + timedelta(minutes=4)).isoformat()}), encoding="utf-8")
    row = by_id(get(ctx, "/api/runs"))["f1"]
    assert row["duration_ms"] == 4 * 60 * 1000
    assert row["turns"] is None and row["total_tokens"] is None


def test_runs_page_counts_outcomes_not_the_legacy_word():
    """The Completed tile and meter read the recorded outcome; `success` is one writer's word."""
    from pathlib import Path

    import garuda.interfaces.web as web

    source = (Path(web.__file__).parent / "static" / "views_runs.js").read_text(encoding="utf-8")
    assert 'r.status === "success"' not in source
    assert "Pass rate" not in source
    assert "runOutcome(r)" in source


# --- item 4: usage -----------------------------------------------------------------------


def test_usage_says_unknown_tokens_and_unmetered_acp_turns(ctx, store):
    from garuda.observability import usage as usage_ledger

    store.begin("n1", task="native", model="m/x", agent="build", workspace="/tmp")
    events = EventStore(session_id="n1", persist_path=store.events_path("n1"))
    usage_ledger.attach(events, store)
    events.append(EventType.MODEL_RESPONSE, {"model": "m/x"})  # a call that reported no usage
    acp_run(store, "a1")
    usage_ledger.record_acp_usage(store, "a1", runtime_id="claude", adapter_version="0.1.0",
                                  segment="seg-a1", usage={"cost_usd": 0.02, "context_used": 10,
                                                           "context_size": 100})
    stats = get(ctx, "/api/usage", "range=30d")
    assert stats["measures"]["native_calls"] == 1
    assert stats["measures"]["total_tokens"] == 0  # the core statistic zero-fills ...
    assert stats["measures"]["tokens_state"] == "unknown"  # ... so the page is told it is unknown
    assert stats["measures"]["acp_turns"] == 0
    assert stats["acp_reports"]["snapshots"] == 1 and stats["acp_reports"]["sessions"] == 1
    assert stats["acp_reports"]["per_turn_metered"] is False
    (row,) = [r for r in stats["harness_model"] if r["harness"] == "native"]
    assert row["tokens_state"] == "unknown"


def test_usage_tokens_known_and_partial(ctx, store):
    from garuda.observability import usage as usage_ledger

    store.begin("n1", task="native", model="m/x", agent="build", workspace="/tmp")
    events = EventStore(session_id="n1", persist_path=store.events_path("n1"))
    usage_ledger.attach(events, store)
    events.append(EventType.MODEL_RESPONSE, {"model": "m/x", "usage": {"prompt_tokens": 7,
                                                                       "completion_tokens": 3}})
    assert get(ctx, "/api/usage", "range=7d")["measures"]["tokens_state"] == "known"
    events.append(EventType.MODEL_RESPONSE, {"model": "m/x"})
    measures = get(ctx, "/api/usage", "range=7d")["measures"]
    assert measures["tokens_state"] == "partial" and measures["tokens_unknown"] == 1


def test_provider_observed_use_does_not_zero_fill_tokens():
    from garuda.interfaces.web.providers import observed_use

    now = 1_790_000_000.0
    records = [{"kind": "native_model_call", "time": now - 5, "session_id": "s", "calls": 1},
               {"kind": "acp_usage_snapshot", "time": now - 5, "session_id": "a", "harness": "claude"}]
    window = observed_use(records, now)["windows"]["5h"]
    assert window["total_tokens"] is None and window["tokens_unknown_records"] == 1
    assert window["acp_reports"] == 1 and window["sessions"] == 2


# --- item 3: one login reading, with its age, on every page -------------------------------


def _manifests():
    from garuda.interfaces.web.runtimes import registry_from_context

    return {m.runtime_id: m for m in registry_from_context({}, ".").manifests}


def _seed_logins():
    import time

    from garuda.acp import login_probe

    manifests = _manifests()
    login_probe.probe_login(manifests["claude"], cache_ttl=0, now=time.time() - 3 * 86400,
                            run=lambda argv, timeout: (1, '{"loggedIn": false}'))
    login_probe.probe_login(manifests["codex"], cache_ttl=0,
                            run=lambda argv, timeout: (0, "Logged in using ChatGPT"))
    return login_probe._cache_path()


def _forbid_login_probes(monkeypatch) -> list:
    """Record every attempt to run the login status command or renew the record (a raise
    could be swallowed by a caller); the returned list must stay empty."""
    from garuda.acp import login_probe

    attempts: list = []
    real_probe = login_probe.probe_login

    def probe(manifest, **kwargs):
        attempts.append(("probe_login", manifest.runtime_id))
        return real_probe(manifest, **{**kwargs, "run": run})

    def run(argv, timeout):
        attempts.append(("status command", tuple(argv)))
        return "timeout"

    monkeypatch.setattr(login_probe, "_run", run)
    monkeypatch.setattr(login_probe, "probe_login", probe)
    return attempts


def test_runtimes_providers_and_setup_tell_one_login_story(ctx, monkeypatch):
    _seed_logins()
    attempts = _forbid_login_probes(monkeypatch)
    runtimes = {r["runtime_id"]: r for r in get(ctx, "/api/runtimes")}
    providers = {c["id"]: c for c in get(ctx, "/api/providers")["harnesses"]}
    setup = {r["runtime_id"]: r for r in get(ctx, "/api/setup")["logins"]}
    for rid in ("claude", "codex"):
        page_states = {runtimes[rid]["login_check"]["state"], providers[rid]["login"]["state"],
                       setup[rid]["state"]}
        assert len(page_states) == 1, (rid, page_states)
        for reading in (runtimes[rid]["login_check"], providers[rid]["login"], setup[rid]):
            assert isinstance(reading["checked_at"], float)
            assert isinstance(reading["age_seconds"], float)
    # The 3-day-old reading is shown as what it is, with its age, on the Runtimes page too.
    assert runtimes["claude"]["login_check"]["state"] == "logged_out"
    assert runtimes["claude"]["login_check"]["age_seconds"] >= 3 * 86400 - 5
    assert runtimes["codex"]["login_check"]["state"] == "authenticated"
    assert attempts == []


@pytest.mark.parametrize("write_mode", [False, True])
def test_runtime_reads_never_run_or_write_the_login_check(store, monkeypatch, write_mode):
    """GET /api/runtimes and its detail are pure reads, in read-only and write mode: the
    login record stays byte-identical and no status command runs."""
    ctx = DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=write_mode)
    record = _seed_logins()
    before = record.read_bytes()
    attempts = _forbid_login_probes(monkeypatch)
    get(ctx, "/api/runtimes")
    get(ctx, "/api/runtimes/claude")
    get(ctx, "/api/runtimes/codex")
    assert attempts == []
    assert record.read_bytes() == before


def test_providers_and_setup_show_a_stale_reading_with_its_age(ctx):
    import time

    from garuda.acp import login_probe

    manifests = _manifests()
    login_probe.probe_login(manifests["claude"], cache_ttl=0, now=time.time() - 3 * 86400,
                            run=lambda argv, timeout: (1, '{"loggedIn": false}'))
    card = {c["id"]: c for c in get(ctx, "/api/providers")["harnesses"]}["claude"]
    assert card["login"]["state"] == "logged_out"
    assert card["login"]["age_seconds"] >= 3 * 86400 - 5
    setup = {r["runtime_id"]: r for r in get(ctx, "/api/setup")["logins"]}["claude"]
    assert setup["state"] == "logged_out" and setup["age_seconds"] >= 3 * 86400 - 5


def test_runtime_detail_guidance_follows_the_recorded_check(ctx):
    from garuda.acp import login_probe

    login_probe.probe_login(_manifests()["codex"], cache_ttl=0,
                            run=lambda argv, timeout: (1, "Not logged in"))
    detail = get(ctx, "/api/runtimes/codex")
    assert detail["login_check"]["state"] == "logged_out"
    assert detail["auth_guidance"][0] == "codex: logged out (recorded check)"


def test_setup_login_diagnostics_carry_their_age():
    from garuda.interfaces.web.providers import annotate_setup_logins

    payload = {"diagnostics": [
        {"code": "harness.ok", "level": "info", "message": "claude 2.1.0; logged in (last check)"},
        {"code": "harness.logged_out", "level": "error", "message": "codex 0.1; logged out"},
        {"code": "config.x", "level": "info", "message": "claude is mentioned"}]}
    checks = {"claude": {"state": "authenticated", "checked_at": 1.0, "age_seconds": 259200.0},
              "codex": {"state": "logged_out", "checked_at": 2.0, "age_seconds": 5.0}}
    out = annotate_setup_logins(payload, checks)
    ok_diag, out_diag, other = out["diagnostics"]
    assert ok_diag["runtime"] == "claude" and ok_diag["age_seconds"] == 259200.0
    assert out_diag["runtime"] == "codex" and out_diag["checked_at"] == 2.0
    assert "checked_at" not in other
    assert [r["runtime_id"] for r in out["logins"]] == ["claude", "codex"]


# --- item 6: a flow's session detail ------------------------------------------------------


def test_session_detail_says_which_event_logs_exist(ctx, store):
    store.begin("f1", task="flow", model="m", agent="build", workspace="/tmp")
    store.update_meta("f1", {"kind": "flow", "flow": {"name": "review-loop", "steps": ["code"]},
                             "flow_state": "completed", "state": finished(success=True)})
    native_run(store, "n1")
    EventStore(session_id="n1", persist_path=store.events_path("n1")).append(
        EventType.SESSION_END, {"success": True})
    flow = get(ctx, "/api/sessions/f1")
    assert flow["event_log"] == {"native": False, "external": False}
    assert flow["step_reported_usage"] is not None  # rolled up by the read model
    assert get(ctx, "/api/sessions/n1")["event_log"]["native"] is True
