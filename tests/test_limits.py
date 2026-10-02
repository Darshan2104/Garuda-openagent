"""Account-bound limit observations and when they may trigger fallback (#168, E.2)."""

import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from garuda.acp.limit_probe import refresh_codex
from garuda.observability.ledger import Ledger
from garuda.observability.limits import (
    LimitObservation,
    LimitStore,
    event_state,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_790_000_000.0
RAW_ACCOUNT = "acct-RAW-12345"
FAKE = '''#!{python}
import json, os, sys
if sys.argv[1:] == ["--version"]:
    print(os.environ["FAKE_VERSION"]); raise SystemExit(0)
log = open(os.environ["FAKE_LOG"], "a")
state = json.load(open(os.environ["FAKE_STATE"]))
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    log.write(method + "\\n"); log.flush()
    if "id" not in message:
        continue
    if method == "initialize":
        reply = {"id": message["id"], "result": {}}
    elif method == "account/read":
        reply = {"id": message["id"], "result": state["account"]}
    elif method == "account/rateLimits/read":
        reply = ({"id": message["id"], "result": state["limits"]} if state["limits"] is not None
                 else {"id": message["id"], "error": {"message": "not logged in"}})
    else:
        reply = {"id": message["id"], "error": {"message": "unknown"}}
    print(json.dumps(reply), flush=True)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "codex"
    script.write_text(FAKE.replace("{python}", sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log, state = tmp_path / "methods.log", tmp_path / "state.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("FAKE_VERSION", "codex-cli 0.159.3")

    def configure(*, used=0.2, reset=NOW + 3600, reached=False, account=RAW_ACCOUNT, limits=True):
        windows = {"primary": {"usedPercent": used * 100, "windowDurationMins": 300,
                               "resetsAt": reset},
                   "secondary": {"usedPercent": 9, "windowDurationMins": 10080,
                                 "resetsAt": NOW + 86400}}
        body = {"rateLimits": {**windows, "limitId": "codex",
                               "rateLimitReachedType": "primary" if reached else None},
                "accountId": account, "ordinaryUsageAllowed": not reached}
        state.write_text(json.dumps({"account": {"account": {"type": "chatgpt"}},
                                     "limits": body if limits else None}))

    configure()
    return type("Fake", (), {"exe": str(script), "log": log, "configure": staticmethod(configure)})


@pytest.fixture
def store(tmp_path):
    return LimitStore(tmp_path / "limits", ledger=Ledger(tmp_path / "usage"))


def read(store, fake, *, now=NOW):
    return refresh_codex(fake.exe, store, now=lambda: now)


def test_known_windows_are_recorded_with_their_source_and_time(store, fake):
    observation = read(store, fake)
    assert observation.source == "codex.account.rateLimits.read" and observation.observed_at == NOW
    assert [(w.name, w.used_fraction, w.window_minutes) for w in observation.windows] == [
        ("primary", 0.2, 300), ("secondary", 0.09, 10080)]
    assert observation.harness_version == "0.159.3" and observation.reached is False
    assert store.latest("codex") == observation
    assert list(store.ledger.records()) == []  # no limit was reached: no event


def test_a_reached_limit_is_one_event_however_often_it_is_observed(store, fake):
    fake.configure(used=1.0, reached=True, reset=NOW + 1800)
    for moment in (NOW, NOW + 5, NOW + 10):
        read(store, fake, now=moment)
    (event,) = store.ledger.records(kind="limit_event")
    assert event["harness"] == "codex" and event["reset_at"] == NOW + 1800
    assert event["account_digest"].startswith("acct:") and RAW_ACCOUNT not in json.dumps(event)


def test_only_a_fresh_account_bound_exhausted_reading_with_a_future_reset_may_trigger(store, fake):
    fake.configure(used=1.0, reached=True, reset=NOW + 1800)
    first = store.assess("codex", lambda: refresh_codex(fake.exe, store, now=lambda: NOW), now=NOW)
    assert first.eligible_exhausted is True  # nothing to contradict it
    again = store.assess("codex", lambda: refresh_codex(fake.exe, store, now=lambda: NOW + 30),
                         now=NOW + 30)
    assert again.eligible_exhausted is True  # the same account confirmed on the next refresh


def _prior(store, fake, **kwargs):
    """Record an earlier reading (the state of the world before the refresh)."""
    fake.configure(used=1.0, reached=True, reset=NOW + 1800, **kwargs)
    return read(store, fake)


@pytest.mark.parametrize("case, reason", [
    ("stale", "stale"), ("expired", "reset time has passed"), ("no_reset", "historical"),
    ("other_account", "account changed"), ("no_account", "no account id"),
    ("other_version", "no source is proved"), ("unproved_version", "no source is proved"),
    ("prior_other_version", "version changed"), ("recovered", "not reached"),
    ("unreadable", "could not be refreshed"),
])
def test_everything_else_leaves_the_provider_eligible(store, fake, case, reason):
    _prior(store, fake)
    now = NOW + 30
    refresh = lambda: refresh_codex(fake.exe, store, now=lambda: NOW + 30)  # noqa: E731
    if case == "stale":
        now = NOW + 91  # the reading was made at NOW+30
    elif case == "expired":
        fake.configure(used=1.0, reached=True, reset=NOW + 10)
    elif case == "no_reset":
        fake.configure(used=0.5, reached=True)  # reached, but no window is exhausted: no reset
    elif case == "other_account":
        fake.configure(used=1.0, reached=True, reset=NOW + 1800, account="someone-else")
    elif case == "no_account":
        fake.configure(used=1.0, reached=True, reset=NOW + 1800, account=None)
    elif case == "other_version":
        os.environ["FAKE_VERSION"] = "codex-cli 0.159.4"
    elif case == "unproved_version":
        os.environ["FAKE_VERSION"] = "codex-cli 0.160.0"
    elif case == "prior_other_version":
        store.record(LimitObservation(**{**store.latest("codex").to_dict(),
                                         "harness_version": "0.159.2",
                                         "windows": store.latest("codex").windows}))
        os.environ["FAKE_VERSION"] = "codex-cli 0.159.3"
    elif case == "recovered":
        fake.configure(used=0.1, reached=False)
    else:
        refresh = lambda: None  # noqa: E731
    decision = store.assess("codex", refresh, now=now)
    assert decision.eligible_exhausted is False and reason in decision.reason, decision


def test_event_display_states(store):
    assert event_state({"reset_at": NOW + 10}, NOW) == "active"
    assert event_state({"reset_at": NOW - 10}, NOW) == "expired"
    assert event_state({}, NOW) == "historical"  # no reset time: shown as history, not a ban


def test_a_logged_out_harness_reads_unknown_and_stays_eligible(store, fake):
    fake.configure(limits=False)
    assert read(store, fake) is None
    assert store.decide("codex", None, now=NOW).eligible_exhausted is False
    assert "could not be refreshed" in store.decide("codex", None, now=NOW).reason


def test_a_refresh_calls_only_read_only_status_methods_and_never_sends_a_prompt(store, fake):
    read(store, fake)
    called = fake.log.read_text().split()
    assert called == ["initialize", "initialized", "account/read", "account/rateLimits/read"]
    assert not any(m in called for m in ("getAuthStatus", "turn/start", "thread/start"))


def test_the_probe_refuses_any_other_method_even_if_asked(store, fake):
    from garuda.acp import limit_probe

    assert "getAuthStatus" not in limit_probe.ALLOWED_METHODS


def test_the_raw_account_id_is_never_stored_and_the_digest_is_local(store, fake, tmp_path):
    fake.configure(used=1.0, reached=True, reset=NOW + 1800)
    first = read(store, fake)
    for path in list((tmp_path / "limits").rglob("*")) + list((tmp_path / "usage").rglob("*")):
        if path.is_file():
            assert RAW_ACCOUNT.encode() not in path.read_bytes(), path
    again = LimitStore(tmp_path / "limits", ledger=Ledger(tmp_path / "usage"))
    assert again.account_digest(RAW_ACCOUNT) == first.account_digest  # stable on this machine
    elsewhere = LimitStore(tmp_path / "elsewhere", ledger=Ledger(tmp_path / "u2"))
    assert elsewhere.account_digest(RAW_ACCOUNT) != first.account_digest  # not linkable
    assert oct((tmp_path / "limits" / ".salt").stat().st_mode & 0o777) == "0o600"


WRITER = """
import sys
from garuda.observability.ledger import Ledger
from garuda.observability.limits import LimitObservation, LimitStore, Window
root, usage, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
store = LimitStore(root, ledger=Ledger(usage))
for i in range(60):
    store.record(LimitObservation(
        harness="codex", harness_version="0.159.3", observed_at=1790000000.0 + i, source="s",
        account_digest="acct:aaaa", windows=(Window("primary", 1.0, 1790001800.0, 300),) * (1 + i % 3),
        limit_id="codex", reached=True, reset_at=1790001800.0))
"""


def test_concurrent_recorders_leave_complete_snapshots_and_one_event(tmp_path):
    root, usage = tmp_path / "limits", tmp_path / "usage"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    procs = [subprocess.Popen([sys.executable, "-c", textwrap.dedent(WRITER), str(root),
                               str(usage), str(i)], env=env, stderr=subprocess.PIPE, text=True)
             for i in range(4)]
    seen, stop = [], threading.Event()
    store = LimitStore(root, ledger=Ledger(usage))

    def reader():
        while not stop.is_set():
            try:
                seen.append(store.latest("codex"))
            except Exception as exc:  # a torn read would surface here
                seen.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    for proc in procs:
        assert proc.wait(timeout=120) == 0, proc.stderr.read()
    stop.set()
    thread.join()
    assert not [s for s in seen if isinstance(s, Exception)]
    assert all(s is None or s.account_digest == "acct:aaaa" for s in seen)
    assert len(list(Ledger(usage).records(kind="limit_event"))) == 1


def test_with_no_proved_account_id_the_quota_reason_stays_disabled(store, fake):
    fake.configure(used=1.0, reached=True, reset=NOW + 1800, account=None)
    decision = store.assess("codex", lambda: refresh_codex(fake.exe, store, now=lambda: NOW + 5),
                            now=NOW + 5)
    assert decision.eligible_exhausted is False  # the provider remains eligible
    assert list(store.ledger.records(kind="limit_event"))[0]["account_digest"] is None


def test_the_fallback_chain_skips_an_exhausted_codex_end_to_end(store, fake, tmp_path, monkeypatch):
    """The real check (a refresh through the fake app-server) feeds C.9's chain."""
    from garuda.agents.fallbacks import choose
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.config import garuda_yaml as gy
    from garuda.runtime.roles import plan_role

    other = tmp_path / "other-acp"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(other.stat().st_mode | stat.S_IEXEC)
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("runtimes:\n" + "".join(
        f"  - runtime_id: {rid}\n    kind: acp\n    command: [{exe}]\n    version: '1'\n"
        f"    auth_probe: {{argv: [{exe}, status], authenticated_pattern: 'Logged in',"
        " unauthenticated_pattern: 'Not logged in'}\n"
        for rid, exe in (("codex", fake.exe), ("backup", str(other)))))
    monkeypatch.setattr("garuda.observability.limits.default_root", lambda: tmp_path / "limits")
    monkeypatch.setattr("garuda.observability.limits.Ledger", lambda: Ledger(tmp_path / "usage"))
    resolved = gy.resolve(gy.parse({"version": 1, "roles": {"coder": {
        "harness": "codex", "fallback": [{"harness": "backup"}]}}}), None, cli_role="coder")
    catalog = prepare_runtime_catalog(str(tmp_path))
    plan = plan_role(resolved, catalog)

    def login(argv, timeout):
        return 0, "Logged in"

    fake.configure(used=0.2)  # plenty left: the primary starts
    assert choose(plan, resolved, catalog, login_run=login).runtime_id == "codex"
    fake.configure(used=1.0, reached=True, reset=NOW + 10**9)  # exhausted, reset far ahead
    chosen = choose(plan, resolved, catalog, login_run=login)
    assert chosen.runtime_id == "backup"
    assert [s["reason"] for s in chosen.fallback["skipped"]] == ["harness.limit_reached"]
    methods = fake.log.read_text().split()
    assert "getAuthStatus" not in methods and "turn/start" not in methods
