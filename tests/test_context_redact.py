"""Redaction and validation tests for issue #19 (P0.11).

Known-secret, oversize, path-escape, and false-positive fixtures: secrets never
reach generated files, unsafe handoffs block with every violation listed, and
redaction touches only pack text — never repository documentation.
"""

import pytest

from garuda.context.pack import ContextPackManager, compile_handoff
from garuda.context.redact import (
    UnsafeHandoffError,
    assert_safe_for_switch,
    redact_pack,
    redact_text,
    summarize_command_output,
    validate_pack,
)
from garuda.context.schemas import parse_handoff
from garuda.context.state_card import WorkingState


def _state() -> WorkingState:
    state = WorkingState(task="Rotate keys")
    state.note_check("pytest -q", 0, turn=1)
    return state


def test_known_secrets_are_redacted_with_kinds():
    text = "key sk-abc123XYZ_, ghp_deadbeefcafe1234, and AKIAIOSFODNN7EXAMPLE"
    cleaned, findings = redact_text(text)
    assert "sk-abc123" not in cleaned
    assert "ghp_deadbeef" not in cleaned
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
    assert cleaned.count("[REDACTED:") == 3
    kinds = {f.kind for f in findings}
    assert {"api-token", "aws-key"} <= kinds


def test_assignment_and_bearer_patterns():
    cleaned, _ = redact_text("password=hunter2 hunter2\nAuthorization: Bearer abcdef123456")
    assert "hunter2 hunter2" not in cleaned
    assert "abcdef123456" not in cleaned


def test_false_positives_stay_readable():
    prose = "The token bucket refills every minute; no secret handshake required."
    cleaned, findings = redact_text(prose)
    assert cleaned == prose
    assert findings == []


def test_oversize_path_escape_and_reasoning_block():
    result = validate_pack({}, "x" * 32769)
    assert not result.valid
    assert any("exceeds bound" in e for e in result.errors)

    result = validate_pack({}, "ok", changed_files=("../escape.txt", "/abs.txt"))
    assert not result.valid
    assert sum("workspace" in e for e in result.errors) == 2

    result = validate_pack({}, "My <thinking>step by step</thinking> plan")
    assert not result.valid
    assert any("no-reasoning" in e for e in result.errors)

    with pytest.raises(UnsafeHandoffError, match="unsafe handoff blocks switching"):
        assert_safe_for_switch({}, "x" * 32769, changed_files=("../e",))


def test_writer_redacts_secrets_and_marks_handoff(tmp_path):
    manager = ContextPackManager(tmp_path / ".context")
    doc, body = compile_handoff(
        _state(), source_runtime="native", session_id="g", native_session_id="n"
    )
    doc, body = (
        doc,
        body + "\nDeploy with api_key=supersecretvalue9.\n",
    )
    target = manager.write_handoff(doc, body)
    written = target.read_text(encoding="utf-8")
    assert "supersecretvalue9" not in written
    assert "[REDACTED:credential]" in written
    parsed, _ = parse_handoff(written)
    assert parsed.redacted is True


def test_writer_blocks_unsafe_handoff_leaving_no_file(tmp_path):
    from dataclasses import replace

    manager = ContextPackManager(tmp_path / ".context")
    doc, body = compile_handoff(_state(), source_runtime="native")
    doc = replace(doc, changed_files=("../escape.txt",))
    with pytest.raises(UnsafeHandoffError, match="workspace"):
        manager.write_handoff(doc, body)
    assert not manager.handoff_path.exists()


def test_redaction_never_touches_repository_files(tmp_path):
    durable = tmp_path / "notes.md"
    durable.write_text("password=hunter2\n", encoding="utf-8")
    manager = ContextPackManager(tmp_path / ".context")
    doc, body = compile_handoff(_state(), source_runtime="native")
    manager.write_handoff(doc, body + "nothing secret here")
    assert durable.read_text(encoding="utf-8") == "password=hunter2\n"


def test_command_output_summaries_keep_head_and_tail():
    output = "\n".join(f"line {i}" for i in range(500))
    summary = summarize_command_output(output, limit=200)
    assert len(summary) < len(output)
    assert "line 0" in summary
    assert "line 499" in summary
    assert "clipped" in summary
    assert summarize_command_output("short") == "short"


def test_redact_pack_preserves_structure():
    front = {"task": "T", "count": 3, "flag": True, "note": "token=abc123XYZ"}
    cleaned_front, cleaned_body, findings = redact_pack(front, "clean body")
    assert cleaned_front["count"] == 3
    assert cleaned_front["flag"] is True
    assert "abc123XYZ" not in cleaned_front["note"]
    assert cleaned_body == "clean body"
    assert findings and findings[0].kind == "credential"
