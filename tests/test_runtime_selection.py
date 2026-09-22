"""Initial selection tests for issue #77 (P1).

Precedence, deterministic rule ordering, every matcher family, pre-start
validation, project-trust boundaries, decision records, and startup
fallback. The P2 policy router (tests/test_runtime_router.py) is a
separate layer and is not exercised here.
"""

import json
import shutil
import subprocess

import pytest

import garuda.runtime.selection as selection
from garuda.core.sessions import SessionStore
from garuda.runtime.selection import (
    InitialCandidate,
    InitialRequest,
    SelectionError,
    parse_global_selection,
    parse_project_selection,
    parse_selection_rules,
    select_initial,
    select_startup_fallback,
    validate_candidate,
    workspace_unchanged,
)
from garuda.workspace.diff import capture_baseline


def _candidates(**overrides):
    pool = [
        InitialCandidate("native", kind="native", available=True, health="ok",
                         auth="authenticated", capabilities={"prompt", "cancel", "resume"}),
        InitialCandidate("codex", kind="acp", available=True, health="ok",
                         auth="authenticated", capabilities={"prompt", "cancel"}),
        InitialCandidate("claude", kind="acp", available=True, health="ok",
                         auth="unknown", capabilities={"prompt"}),
    ]
    if overrides:
        pool = [
            c if c.runtime_id not in overrides
            else InitialCandidate(c.runtime_id, **overrides[c.runtime_id])
            for c in pool
        ]
    return pool


def _request(**kwargs):
    base = {"task": "Fix the login bug", "agent": "build", "mode": "interactive"}
    base.update(kwargs)
    return InitialRequest(**base)


def _rules(*items, trusted=True):
    return parse_selection_rules(list(items), trusted=trusted)


# --- precedence -------------------------------------------------------------


def test_explicit_beats_everything():
    rules = _rules({"id": "r", "priority": 999, "runtime": "codex",
                    "when": {"agents": ["build"]}})
    decision = select_initial(
        _request(explicit_runtime="claude", profile_pin="codex", default_runtime="codex"),
        _candidates(),
        rules=rules,
        traits=None,
    )
    assert decision.selected == "claude"
    assert decision.source == "explicit"


def test_explicit_unknown_or_invalid_fails_loudly():
    with pytest.raises(SelectionError, match="not configured"):
        select_initial(_request(explicit_runtime="ghost"), _candidates())
    down = _candidates()
    down = [c if c.runtime_id != "codex" else
            InitialCandidate("codex", available=False, unavailable_reason="no binary")
            for c in down]
    with pytest.raises(SelectionError, match="explicit runtime"):
        select_initial(_request(explicit_runtime="codex"), down)


def test_profile_pin_beats_rules_and_defaults():
    rules = _rules({"id": "r", "priority": 999, "runtime": "codex",
                    "when": {"agents": ["build"]}})
    decision = select_initial(
        _request(profile_pin="claude", default_runtime="codex"),
        _candidates(),
        rules=rules,
    )
    assert (decision.selected, decision.source) == ("claude", "profile")
    with pytest.raises(SelectionError, match="profile pin"):
        select_initial(_request(profile_pin="ghost"), _candidates())


def test_rule_beats_default_and_native():
    rules = _rules({"id": "py", "priority": 10, "runtime": "codex",
                    "when": {"agents": ["build"]}})
    decision = select_initial(
        _request(default_runtime="claude"), _candidates(), rules=rules
    )
    assert (decision.selected, decision.source, decision.rule_id) == ("codex", "rule", "py")


def test_classifier_slot_never_selects_and_default_wins():
    decision = select_initial(
        _request(default_runtime="codex"), _candidates(), rules=[]
    )
    assert (decision.selected, decision.source) == ("codex", "default")
    assert decision.classifier.evaluated is False
    assert "classifier" in decision.classifier.reason


def test_native_is_the_final_fallback():
    decision = select_initial(_request(), _candidates(), rules=[])
    assert (decision.selected, decision.source) == ("native", "native")


def test_no_candidates_or_no_viable_native_fails_closed():
    with pytest.raises(SelectionError, match="at least one candidate"):
        select_initial(_request(), [])
    only_down = [InitialCandidate("native", kind="native", available=False)]
    with pytest.raises(SelectionError, match="no initial runtime"):
        select_initial(_request(), only_down)


def test_duplicate_candidates_fail_closed():
    with pytest.raises(SelectionError, match="duplicate candidate"):
        select_initial(_request(), _candidates() + [_candidates()[0]])


def test_selection_is_deterministic():
    rules = _rules(
        {"id": "a", "priority": 5, "runtime": "codex", "when": {"modes": ["interactive"]}},
        {"id": "b", "priority": 5, "runtime": "claude", "when": {"modes": ["interactive"]}},
    )
    first = select_initial(_request(), _candidates(), rules=rules)
    second = select_initial(_request(), _candidates(), rules=rules)
    assert first == second
    # Declaration order breaks the priority tie: rule "a" was declared first.
    assert (first.selected, first.rule_id) == ("codex", "a")


def test_higher_priority_wins_regardless_of_order():
    rules = _rules(
        {"id": "low", "priority": 1, "runtime": "claude", "when": {"modes": ["interactive"]}},
        {"id": "high", "priority": 100, "runtime": "codex", "when": {"modes": ["interactive"]}},
    )
    decision = select_initial(_request(), _candidates(), rules=rules)
    assert (decision.selected, decision.rule_id) == ("codex", "high")


# --- matchers ---------------------------------------------------------------


def test_matchers_cover_every_family(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plan.md").write_text("hi\n", encoding="utf-8")

    traits = selection.detect_repo_traits(tmp_path)
    request = _request(
        task="migrate the python service to asyncio",
        tags=["Architecture"],
        agent="Plan",
        mode="Interactive",
        workspace_kind="local",
        permission_ceiling="smart",
        required_capabilities=("prompt",),
    )
    cases = [
        ({"agents": ["plan"]}, True),
        ({"agents": ["build"]}, False),
        ({"modes": ["interactive"]}, True),
        ({"modes": ["eval"]}, False),
        ({"tags": ["architecture"]}, True),
        ({"tags": ["other"]}, False),
        ({"workspace_kinds": ["local"]}, True),
        ({"workspace_kinds": ["docker"]}, False),
        ({"permission_ceilings": ["smart"]}, True),
        ({"permission_ceilings": ["readonly"]}, False),
        ({"languages": ["python"]}, True),
        ({"languages": ["go"]}, False),
        ({"marker_files": ["pyproject.toml"]}, True),
        ({"marker_files": ["Cargo.toml"]}, False),
        ({"task_substrings": ["asyncio"]}, True),
        ({"task_substrings": ["kubernetes"]}, False),
        ({"task_globs": ["*python*"]}, True),
        ({"task_globs": ["*kubernetes*"]}, False),
        ({"path_globs": ["docs/*.md"]}, True),
        ({"path_globs": ["src/*.go"]}, False),
        ({"capabilities": ["prompt"]}, True),  # checked against the target below
    ]
    for when, expected in cases:
        rule = _rules({"id": "m", "priority": 1, "runtime": "codex", "when": when})[0]
        if "capabilities" in when:
            continue  # capability matching needs the candidate pool; tested next
        matched, _ = selection.rule_matches_request(rule, request, traits)
        assert matched is expected, when
    # Rule-level capabilities filter against the target runtime.
    capable = _rules({"id": "c", "priority": 1, "runtime": "codex",
                      "when": {"capabilities": ["cancel"]}})[0]
    decision = select_initial(request, _candidates(), rules=[capable])
    assert decision.selected == "codex"
    incapable = _rules({"id": "c", "priority": 1, "runtime": "claude",
                        "when": {"capabilities": ["cancel"]}})[0]
    decision = select_initial(request, _candidates(), rules=[incapable])
    assert decision.selected == "native"
    assert any("lacks" in line for line in decision.rejections)


def test_global_task_regex_matches_and_project_regex_rejected():
    rules = _rules({"id": "rx", "priority": 1, "runtime": "codex",
                    "when": {"task_regex": r"migrat\w+.*asyncio"}})
    decision = select_initial(
        _request(task="migrate the service to asyncio"), _candidates(), rules=rules
    )
    assert decision.selected == "codex"
    decision = select_initial(_request(task="fix a typo"), _candidates(), rules=rules)
    assert decision.selected == "native"
    with pytest.raises(SelectionError, match="global-trust-only"):
        parse_selection_rules(
            [{"id": "evil", "runtime": "codex", "when": {"task_regex": ".*"}}],
            trusted=False,
        )
    with pytest.raises(SelectionError, match="invalid pattern"):
        parse_selection_rules(
            [{"id": "bad", "runtime": "codex", "when": {"task_regex": "(unclosed"}}],
            trusted=True,
        )


def test_rule_config_rejects_unknown_and_executable_keys():
    with pytest.raises(SelectionError, match="unknown fields"):
        parse_selection_rules([{"id": "x", "runtime": "codex", "bogus": 1}], trusted=True)
    with pytest.raises(SelectionError, match="unknown condition"):
        parse_selection_rules(
            [{"id": "x", "runtime": "codex", "when": {"teleport": ["yes"]}}], trusted=True
        )
    with pytest.raises(SelectionError, match="forbidden"):
        parse_selection_rules(
            [{"id": "x", "runtime": "codex", "command": ["rm"]}], trusted=True
        )


# --- trust boundaries -------------------------------------------------------


def test_project_rules_are_recommendations_without_global_trust():
    project = _rules({"id": "proj", "priority": 999, "runtime": "codex",
                      "when": {"agents": ["build"]}}, trusted=False)
    decision = select_initial(_request(), _candidates(), project_rules=project)
    assert decision.selected == "native"
    assert decision.source == "native"
    assert any("proj" in line for line in decision.recommendations)
    assert any("recommendations only" in line for line in decision.rationale)


def test_project_rules_select_only_when_globally_authorized():
    project = _rules({"id": "proj", "priority": 999, "runtime": "codex",
                      "when": {"agents": ["build"]}}, trusted=False)
    decision = select_initial(
        _request(), _candidates(), project_rules=project, trust_project_routes=True
    )
    assert (decision.selected, decision.source, decision.rule_id) == ("codex", "rule", "proj")


def test_global_selection_parsing_with_trust_flag():
    config = parse_global_selection(
        {"routing": {"default_runtime": "codex", "fallback_runtime": "claude",
                      "trust_project_routes": True,
                      "rules": [{"id": "r", "runtime": "codex",
                                 "when": {"modes": ["interactive"]}}]}}
    )
    assert config.default_runtime == "codex"
    assert config.fallback_runtime == "claude"
    assert config.trust_project_routes is True
    assert [r.rule_id for r in config.rules] == ["r"]
    empty = parse_global_selection({"models": {"x": {}}})
    assert empty.rules == () and empty.default_runtime is None
    with pytest.raises(SelectionError, match="unknown fields"):
        parse_global_selection({"routing": {"teleport": True}})
    project = parse_project_selection({"rules": [{"id": "p", "runtime": "codex"}]})
    assert [r.rule_id for r in project.rules] == ["p"]
    assert all(not r.trusted for r in project.rules)
    with pytest.raises(SelectionError, match="forbidden"):
        parse_project_selection({"routing": {"rules": [], "command": ["x"]}})


# --- pre-start validation ---------------------------------------------------


def test_validation_rejects_before_start():
    unhealthy = InitialCandidate("codex", available=True, health="unavailable")
    ok, reason = validate_candidate(unhealthy, _request())
    assert not ok and "unavailable" in reason
    logged_out = InitialCandidate("codex", auth="unauthenticated")
    ok, _ = validate_candidate(logged_out, _request())
    assert not ok
    expired = InitialCandidate("codex", auth="expired")
    ok, _ = validate_candidate(expired, _request())
    assert not ok
    # Unknown login state is allowed: it stays unknown until a run.
    unknown = InitialCandidate("codex", auth="unknown")
    ok, _ = validate_candidate(unknown, _request())
    assert ok
    degraded = InitialCandidate("codex", health="degraded")
    ok, _ = validate_candidate(degraded, _request())
    assert ok
    incapable = InitialCandidate("codex", capabilities={"prompt"})
    ok, reason = validate_candidate(incapable, _request(required_capabilities=("cancel",)))
    assert not ok and "lacks" in reason
    confined = InitialCandidate("codex", workspace_kinds=("docker",))
    ok, _ = validate_candidate(confined, _request(workspace_kind="local"))
    assert not ok
    strict = InitialCandidate("codex", permission_ceilings=("readonly",))
    ok, _ = validate_candidate(strict, _request(permission_ceiling="smart"))
    assert not ok


def test_rule_with_invalid_target_falls_through_with_reasons():
    rules = _rules(
        {"id": "bad", "priority": 100, "runtime": "codex", "when": {"modes": ["interactive"]}},
        {"id": "good", "priority": 1, "runtime": "claude", "when": {"modes": ["interactive"]}},
    )
    down = [c if c.runtime_id != "codex" else
            InitialCandidate("codex", available=False, unavailable_reason="crashed")
            for c in _candidates()]
    decision = select_initial(_request(), down, rules=rules)
    assert (decision.selected, decision.rule_id) == ("claude", "good")
    assert any("codex" in line for line in decision.rejections)


# --- decision records -------------------------------------------------------


def test_decision_record_is_complete_and_json_safe():
    rules = _rules({"id": "r", "priority": 1, "runtime": "codex",
                    "when": {"agents": ["build"]}})
    decision = select_initial(_request(default_runtime="codex"), _candidates(), rules=rules)
    record = decision.to_dict()
    assert record["schema_version"] == 1
    assert record["selected"] == "codex" and record["source"] == "rule"
    assert record["rule_id"] == "r"
    assert set(record) >= {"candidates", "matches", "rejections", "rationale",
                           "recommendations", "classifier", "capabilities", "fallback_from"}
    assert record["classifier"]["evaluated"] is False
    json.dumps(record)  # must survive the session store
    assert decision.explain()
    assert any("codex" in line for line in decision.explain())


def test_selection_persists_onto_the_session(tmp_path):
    store = SessionStore(tmp_path)
    store.begin("s1", task="t", model="m", agent="build", workspace="/tmp/ws")
    decision = select_initial(_request(), _candidates(), rules=[])
    selection.record_initial_selection(store, "s1", decision)
    meta = store.load_meta("s1")
    record = selection.load_initial_selection(meta)
    assert record is not None and record["selected"] == "native"
    # The unified document still validates: the record rides the preserved fields.
    unified = store.load_unified("s1")
    assert unified.session_id == "s1"
    assert store.load_meta("s1")["initial_selection"]["source"] == "native"


# --- startup fallback ---------------------------------------------------------


def _git_repo(path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", str(path)], check=True)
    subprocess.run([git, "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run([git, "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run([git, "-C", str(path), "add", "."], check=True)
    subprocess.run([git, "-C", str(path), "commit", "-qm", "base"], check=True)


def test_fallback_requires_an_unchanged_baseline(tmp_path):
    _git_repo(tmp_path)
    before = capture_baseline(tmp_path)
    unchanged, _ = workspace_unchanged(before, tmp_path)
    assert unchanged is True
    (tmp_path / "mutated.txt").write_text("failed start wrote this\n", encoding="utf-8")
    unchanged, reason = workspace_unchanged(before, tmp_path)
    assert unchanged is False
    assert "mutated.txt" in reason


def test_fallback_selects_once_and_records_origin(tmp_path):
    _git_repo(tmp_path)
    before = capture_baseline(tmp_path)
    first = select_initial(_request(), _candidates(), rules=[])
    assert first.selected == "native"
    # A failed non-native start falls back to native on a clean tree.
    started = select_initial(
        _request(), _candidates(),
        rules=_rules({"id": "r", "priority": 1, "runtime": "codex",
                      "when": {"agents": ["build"]}}),
    )
    assert started.selected == "codex"
    fallback = select_startup_fallback(
        started, _request(), _candidates(), baseline_before=before, workspace=tmp_path
    )
    assert (fallback.selected, fallback.source, fallback.fallback_from) == ("native", "fallback", "codex")
    with pytest.raises(SelectionError, match="already failed"):
        select_startup_fallback(
            first, _request(), _candidates(), baseline_before=before, workspace=tmp_path
        )
    (tmp_path / "dirty.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(SelectionError, match="fallback refused"):
        select_startup_fallback(
            started, _request(), _candidates(), baseline_before=before, workspace=tmp_path
        )


def test_fallback_outside_a_repo_fails_closed(tmp_path):
    first = select_initial(
        _request(), _candidates(),
        rules=_rules({"id": "r", "priority": 1, "runtime": "codex",
                      "when": {"agents": ["build"]}}),
    )
    assert first.selected == "codex"
    before = capture_baseline(tmp_path)  # not a repo: empty baseline
    assert before.commit == ""
    unchanged, reason = workspace_unchanged(before, tmp_path)
    assert unchanged is False
    assert "no baseline commit" in reason
    with pytest.raises(SelectionError, match="fallback refused"):
        select_startup_fallback(
            first, _request(), _candidates(), baseline_before=before, workspace=tmp_path
        )


# --- layer separation ---------------------------------------------------------


def test_selection_layer_owns_no_router_types_and_no_handoff():
    for name in ("RoutingRequest", "RoutingCandidate", "RoutingDecision"):
        assert not hasattr(selection, name), name
    for name in dir(selection):
        lowered = name.lower()
        assert "handoff" not in lowered, name
        assert "routing" not in lowered or name.startswith("parse_"), name
    assert "router" not in selection.__doc__.lower() or "distinct from" in selection.__doc__


# --- explanation safety -------------------------------------------------------


def test_explanations_contain_no_secrets_or_workspace_paths(monkeypatch):
    decision = select_initial(_request(), _candidates(), rules=[])
    # Normal routing facts (runtime ids, rule ids, sources, capabilities)
    # must always pass the safety check.
    selection.assert_explanation_safe(decision)

    def _bad(**kwargs):
        base = {
            "selected": "codex",
            "source": "rule",
            "rule_id": "r",
            "capabilities": ("prompt",),
            "candidates": ("codex", "native"),
        }
        base.update(kwargs)
        return selection.InitialSelection(**base)

    adversarial = [
        {"rationale": ("deploy with API_KEY=sk-live-1234",)},
        {"rationale": ("use token abcdef for auth",)},
        {"rationale": ("bearer eyJhbGciOiJIUzI1NiJ9",)},
        {"rationale": ("the password is hunter2",)},
        {"rationale": ("db credential leaked here",)},
        {"rationale": ("oauth code=xyz",)},
        {"rationale": ("read ~/Library/Keychain/login.keychain",)},
        {"matches": ("r -> codex: yes (secret sauce)",)},
        {"rationale": ("config at /home/alice/work/garuda",)},
        {"rationale": ("config at /Users/alice/work/garuda",)},
        {"rationale": ("config at $HOME/.config/garuda",)},
        {"rationale": ("config at /tmp/garuda-work/session-1",)},
    ]
    for payload in adversarial:
        with pytest.raises(SelectionError):
            selection.assert_explanation_safe(_bad(**payload))

    # Secret-named env values must never leak through explanations either.
    monkeypatch.setenv("GARUDA_TEST_SECRET_TOKEN", "zz-top-999-qwerty-value")
    leaked = _bad(rationale=("failed with zz-top-999-qwerty-value in output",))
    with pytest.raises(SelectionError):
        selection.assert_explanation_safe(leaked)
