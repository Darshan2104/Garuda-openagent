"""Structured JSON verifier verdict parsing (with plain-text fallback)."""

from garuda.core.verifier import parse_verdict


def test_json_approved():
    ok, reason = parse_verdict('{"verdict": "APPROVED", "reason": "tests pass"}')
    assert ok is True
    assert reason == "tests pass"


def test_json_rejected():
    ok, reason = parse_verdict('{"verdict": "REJECTED", "reason": "no tests were run"}')
    assert ok is False
    assert reason == "no tests were run"


def test_json_fenced():
    ok, reason = parse_verdict('```json\n{"verdict": "APPROVED", "reason": "done"}\n```')
    assert ok is True
    assert reason == "done"


def test_json_embedded_in_prose():
    text = 'Here is my verdict:\n{"verdict": "REJECTED", "reason": "artifact missing"}\nThanks.'
    ok, reason = parse_verdict(text)
    assert ok is False
    assert reason == "artifact missing"


def test_verdict_synonyms():
    assert parse_verdict('{"verdict": "pass"}')[0] is True
    assert parse_verdict('{"verdict": "fail"}')[0] is False
    assert parse_verdict('{"verdict": "accept", "reason": "x"}')[0] is True


def test_plaintext_prefix_fallback_approved():
    ok, reason = parse_verdict("APPROVED: work is verified.")
    assert ok is True
    assert "verified" in reason


def test_plaintext_prefix_fallback_rejected():
    ok, reason = parse_verdict("REJECTED: no tests were run.")
    assert ok is False
    assert "no tests were run" in reason


def test_markdown_prefix_tolerated():
    ok, _ = parse_verdict("**APPROVED** — looks good")
    assert ok is True


def test_noise_is_unparseable():
    ok, reason = parse_verdict("Well, it looks mostly fine I guess.")
    assert ok is None
    assert reason == ""


def test_empty_is_unparseable():
    assert parse_verdict("")[0] is None
    assert parse_verdict("   \n  ")[0] is None


def test_json_without_verdict_key_falls_through():
    # A dict with no 'verdict' key isn't a verdict; and no prefix line → unparseable.
    assert parse_verdict('{"note": "hello"}')[0] is None
