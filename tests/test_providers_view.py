"""Provider and limit cards (#169, plan task F.2): each fixture renders as specified."""

import json

import pytest

from garuda.interfaces.web import providers
from garuda.observability.ledger import Ledger
from garuda.observability.limits import LimitObservation, LimitStore, Window

NOW = 1_790_000_000.0


@pytest.fixture
def world(tmp_path):
    ledger = Ledger(tmp_path / "usage")
    return ledger, LimitStore(tmp_path / "limits", ledger=ledger)


def entry(rid="codex", version="codex-cli 0.159.3", available=True):
    return {"runtime_id": rid, "kind": "acp", "available": available, "version": version}


def observation(store, *, at=NOW - 10, reached=False, reset=None, account="x", version="0.159.3",
                used=0.2):
    obs = LimitObservation(
        harness="codex", harness_version=version, observed_at=at, source="codex.account.rateLimits.read",
        account_digest=store.account_digest(account) if account else None,
        windows=(Window("primary", used, NOW + 3600, 300), Window("secondary", 0.09, NOW + 86400, 10080)),
        limit_id="codex", reached=reached, reset_at=reset)
    store.record(obs)
    return obs


def card(world, entries=None, **kw):
    ledger, limits = world
    entries = entries or [entry()]
    result = providers.cards(entries, {}, ledger=ledger, limits=limits, now=NOW, **kw)
    return {c["id"]: c for c in result["harnesses"]}


def test_known_windows_show_their_source_and_observation_time(world):
    _ledger, limits = world
    observation(limits)
    block = card(world)["codex"]["limits"]
    assert block["status"] == "known" and block["source"] == "codex.account.rateLimits.read"
    assert block["age_seconds"] == 10.0 and block["observed_at"] == NOW - 10
    assert [(w["name"], w["used_fraction"], w["window_minutes"]) for w in block["windows"]] == [
        ("primary", 0.2, 300), ("secondary", 0.09, 10080)]


def test_a_harness_without_a_proved_source_has_unknown_windows(world):
    block = card(world, [entry("claude", "2.1.285 (Claude Code)")])["claude"]["limits"]
    assert block["status"] == "unknown" and block["windows"] == []
    assert "no documented" in block["reason"]


def test_a_proved_harness_never_refreshed_says_so(world):
    block = card(world)["codex"]["limits"]
    assert block["status"] == "unknown" and "no observation" in block["reason"]


def test_an_old_observation_is_stale_with_its_age(world):
    _ledger, limits = world
    observation(limits, at=NOW - 600)
    block = card(world)["codex"]["limits"]
    assert block["status"] == "stale" and block["age_seconds"] == 600.0


@pytest.mark.parametrize("reset, state", [(NOW + 900, "active"), (NOW - 900, "expired"),
                                          (None, "historical")])
def test_events_read_active_expired_or_historical(world, reset, state):
    _ledger, limits = world
    observation(limits, reached=True, reset=reset, used=1.0)
    (event,) = card(world)["codex"]["limits"]["events"]
    assert event["state"] == state and event["reset_at"] == reset


def test_a_version_other_than_the_captured_matrix_is_flagged(world):
    _ledger, limits = world
    observation(limits, version="0.159.3")
    notes = card(world, [entry(version="codex-cli 0.160.0")])["codex"]["limits"]["notes"]
    assert any("differs from the captured matrix" in n for n in notes)
    assert any("observed on 0.159.3, now 0.160.0" in n for n in notes)


def test_a_changed_account_is_called_out(world):
    _ledger, limits = world
    observation(limits, account="first")
    observation(limits, account="second")
    block = card(world)["codex"]["limits"]
    assert block["accounts_seen"] == 2
    assert any("more than one account" in n for n in block["notes"])


def test_a_source_with_no_account_id_cannot_bind_a_limit(world):
    _ledger, limits = world
    observation(limits, account=None)
    notes = card(world)["codex"]["limits"]["notes"]
    assert any("no account id" in n for n in notes)


def test_a_logged_out_harness_shows_it(world, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from garuda.acp import login_probe

    manifest = SimpleNamespace(runtime_id="codex", auth_probe=SimpleNamespace(
        argv=("codex", "status"), authenticated_pattern="Logged in",
        unauthenticated_pattern="Not logged in"))
    login_probe.probe_login(manifest, cache_ttl=0, run=lambda argv, timeout: (1, "Not logged in"))
    ledger, limits = world
    result = providers.cards([entry()], {"codex": manifest}, ledger=ledger, limits=limits, now=NOW)
    assert result["harnesses"][0]["login"]["state"] == "logged_out"
    # and a harness never checked says so rather than guessing
    other = SimpleNamespace(runtime_id="x", auth_probe=SimpleNamespace(
        argv=("x",), authenticated_pattern="a", unauthenticated_pattern="b"))
    again = providers.cards([entry("x")], {"x": other}, ledger=ledger, limits=limits, now=NOW)
    assert again["harnesses"][0]["login"]["state"] == "not_checked"


def _rec(ledger, key, when, **kw):
    ledger.append({"kind": "native_model_call", "key": key, "time": when, "session_id": key,
                   "model": "openrouter/x/y", "input_tokens": 10, "output_tokens": 5,
                   "total_tokens": 15, **kw})


def test_observed_use_is_per_window_and_labelled(world):
    ledger, limits = world
    day = 86400
    _rec(ledger, "a", NOW - 3600, cost_usd=0.5)  # within 5h
    _rec(ledger, "b", NOW - 2 * day, cost_usd=0.25)  # within 7d
    _rec(ledger, "c", NOW - 20 * day)  # within 30d, unpriced
    _rec(ledger, "d", NOW - 40 * day, cost_usd=9.0)  # outside every window
    result = providers.cards([], {}, ledger=ledger, limits=limits, now=NOW)
    (provider,) = result["providers"]
    assert provider["id"] == "openrouter" and provider["observed_use"]["label"] == "through Garuda only"
    windows = provider["observed_use"]["windows"]
    assert (windows["5h"]["sessions"], windows["5h"]["total_tokens"], windows["5h"]["cost_usd"]) == (1, 15, 0.5)
    assert (windows["7d"]["sessions"], windows["7d"]["cost_usd"]) == (2, 0.75)
    assert (windows["30d"]["sessions"], windows["30d"]["cost_usd"],
            windows["30d"]["cost_unknown_records"]) == (3, 0.75, 1)
    assert provider["rate_limits"]["status"] == "unknown"


def test_the_route_serves_cards_and_refresh_is_write_mode_only(world, tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.web.routes import DashboardContext
    from tests.test_web_routes import PORT, TOKEN, body, call

    ctx = DashboardContext(port=PORT, token=TOKEN, store=SessionStore(tmp_path / "s"),
                           workspace=tmp_path)
    ctx.allow_run = False
    response = call(ctx, "/api/providers")
    assert response.status == 200 and {"harnesses", "providers"} <= set(body(response))
    json.dumps(body(response))
    refused = call(ctx, "/api/providers/refresh", method="POST")
    assert refused.status in (403, 503)


def test_refresh_runs_the_status_reads_and_nothing_else(tmp_path, monkeypatch):
    from types import SimpleNamespace

    manifest = SimpleNamespace(runtime_id="codex", auth_probe=SimpleNamespace(
        argv=("codex-bin", "login"), authenticated_pattern="Logged in",
        unauthenticated_pattern="Not logged in"))
    monkeypatch.setattr("garuda.interfaces.web.runtimes.registry_from_context",
                        lambda extra, workspace: SimpleNamespace(manifests=[manifest]))
    ran, read = [], []
    done = providers.refresh(str(tmp_path), login_run=lambda argv, timeout: ran.append(argv) or (0, "Logged in"),
                             limit_refresh=lambda exe: read.append(exe) or object())
    assert ran == [("codex-bin", "login")] and read == ["codex-bin"]
    assert done == {"login": ["codex"], "limits": ["codex"]}
