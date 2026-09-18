"""Schema tests for issue #16 (P0.8).

Forward-compatibility and invalid-document fixtures: valid files round-trip
with unknown fields preserved, over-bound or missing required fields fail,
and unknown versions are rejected.
"""

import pytest

from garuda.context.schemas import (
    CONTEXT_SCHEMA_VERSION,
    ContextSchemaError,
    CurrentTask,
    Handoff,
    parse_current_task,
    parse_handoff,
    render,
)


def _frontmatter(**fields) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {v}" for v in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def test_current_task_round_trip_with_body():
    doc, body = parse_current_task(
        _frontmatter(
            version=1,
            task="Ship it",
            acceptance_criteria=["green suite"],
            changed_files=["a.py"],
            evidence=["pytest -q"],
            next_action="merge",
        )
        + "\nNotes in **markdown** survive.\n"
    )
    assert doc.task == "Ship it"
    assert doc.acceptance_criteria == ("green suite",)
    assert doc.source_runtime == "native"
    assert body == "Notes in **markdown** survive."
    doc2, body2 = parse_current_task(render(doc.to_frontmatter(), body))
    assert doc2 == doc
    assert body2 == body


def test_handoff_requires_source_runtime_and_round_trips():
    text = _frontmatter(
        version=1,
        task="Hand over",
        source_runtime="native",
        garuda_session_id="g1",
        native_session_id="n1",
        redacted=True,
    )
    doc, _ = parse_handoff(text)
    assert doc.source_runtime == "native"
    assert doc.redacted is True
    doc2, _ = parse_handoff(render(doc.to_frontmatter()))
    assert doc2 == doc

    with pytest.raises(ContextSchemaError, match="source_runtime"):
        parse_handoff(_frontmatter(version=1, task="No source"))


def test_unknown_fields_round_trip_safely():
    text = _frontmatter(version=1, task="T", future_field="keep me") + "\nbody\n"
    doc, _ = parse_current_task(text)
    assert doc.extra == {"future_field": "keep me"}
    doc2, _ = parse_current_task(render(doc.to_frontmatter(), "body"))
    assert doc2.extra == {"future_field": "keep me"}


def test_unknown_version_is_rejected():
    with pytest.raises(ContextSchemaError, match="upgrade Garuda"):
        parse_current_task(_frontmatter(version=99, task="T"))
    with pytest.raises(ContextSchemaError, match="upgrade Garuda"):
        parse_handoff(_frontmatter(version=99, task="T", source_runtime="native"))


def test_missing_task_and_bad_types_fail():
    with pytest.raises(ContextSchemaError, match="task"):
        parse_current_task(_frontmatter(version=1))
    with pytest.raises(ContextSchemaError, match="acceptance_criteria"):
        parse_current_task(_frontmatter(version=1, task="T", acceptance_criteria="all"))
    with pytest.raises(ContextSchemaError, match="redacted"):
        parse_handoff('---\nversion: 1\ntask: T\nsource_runtime: n\nredacted: "yes"\n---\n')


def test_bounds_are_enforced():
    with pytest.raises(ContextSchemaError, match="exceeds bound"):
        parse_current_task(
            _frontmatter(version=1, task="T", changed_files=[f"f{i}.py" for i in range(41)])
        )
    with pytest.raises(ContextSchemaError, match="exceeds bound"):
        parse_handoff(
            _frontmatter(
                version=1, task="T", source_runtime="n", next_action="x" * 4001
            )
        )


def test_schema_version_constant():
    assert CONTEXT_SCHEMA_VERSION == 1
    assert CurrentTask(task="t").to_frontmatter()["version"] == 1
    assert Handoff(task="t", source_runtime="n").to_frontmatter()["version"] == 1
