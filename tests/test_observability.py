import importlib.util

import pytest

from garuda.core.events import EventStore, EventType
from garuda.observability import tracing
from garuda.observability.tracing import (
    LLM_TOKEN_COMPLETION,
    LLM_TOKEN_PROMPT,
    SPAN_KIND,
    TOOL_NAME,
    configure_tracing,
    emit_spans_from_events,
    is_available,
)

OTEL_INSTALLED = importlib.util.find_spec("opentelemetry") is not None


@pytest.fixture(autouse=True)
def _reset_tracing():
    """Keep each test isolated from module-level tracer state."""
    tracing.reset_tracing()
    yield
    tracing.reset_tracing()


def test_is_available_returns_bool():
    assert isinstance(is_available(), bool)


def test_configure_tracing_disabled_is_noop(monkeypatch):
    monkeypatch.delenv("GARUDA_TRACING", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert configure_tracing() is False
    assert tracing.is_configured() is False


def test_emit_spans_returns_zero_when_disabled(monkeypatch):
    monkeypatch.delenv("GARUDA_TRACING", raising=False)
    events = [{"type": "session_start", "timestamp": None, "session_id": "s", "payload": {}}]
    assert emit_spans_from_events(events) == 0


def test_span_context_manager_noop_when_disabled():
    with tracing.span("op", "TOOL") as active:
        assert active is None


def test_on_append_callback_fires():
    received: list[dict] = []
    store = EventStore(on_append=received.append)
    store.append(EventType.SESSION_START, {"task": "x", "model": "m"})

    assert len(received) == 1
    assert received[0]["type"] == "session_start"
    assert received[0]["payload"]["task"] == "x"


def test_on_append_raising_callback_does_not_break_append():
    def boom(_event):
        raise RuntimeError("observer failure")

    store = EventStore(on_append=boom)
    # Must not raise despite the failing observer.
    store.append(EventType.USER_MESSAGE, {"content": "hi"})

    assert len(store.get_all()) == 1
    assert store.get_all()[0]["payload"]["content"] == "hi"


def test_on_append_default_none_is_backward_compatible():
    store = EventStore()
    store.append(EventType.SESSION_START, {"task": "x"})
    assert len(store.get_all()) == 1


@pytest.mark.skipif(not OTEL_INSTALLED, reason="opentelemetry not installed")
def test_emit_spans_from_events_with_in_memory_exporter(monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.setenv("GARUDA_TRACING", "console")
    exporter = InMemorySpanExporter()

    assert configure_tracing(service_name="garuda-test", span_exporter=exporter) is True
    assert tracing.is_configured() is True

    events = [
        {
            "type": "session_start",
            "timestamp": "2026-07-02T10:00:00+00:00",
            "session_id": "s1",
            "payload": {"task": "do x", "model": "gpt-test"},
        },
        {
            "type": "model_response",
            "timestamp": "2026-07-02T10:00:01+00:00",
            "session_id": "s1",
            "payload": {
                "content": None,
                "tool_calls": [{"id": "c1", "name": "bash", "arguments": {}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        },
        {
            "type": "tool_call",
            "timestamp": "2026-07-02T10:00:02+00:00",
            "session_id": "s1",
            "payload": {"id": "c1", "name": "bash", "arguments": {"command": "ls"}},
        },
        {
            "type": "tool_result",
            "timestamp": "2026-07-02T10:00:03+00:00",
            "session_id": "s1",
            "payload": {"tool_call_id": "c1", "name": "bash", "content": "ok", "is_error": False},
        },
        {
            "type": "session_end",
            "timestamp": "2026-07-02T10:00:04+00:00",
            "session_id": "s1",
            "payload": {"success": True, "turns": 1},
        },
    ]

    count = emit_spans_from_events(events, service_name="garuda-test")
    assert count == 3  # root session + one LLM + one TOOL

    spans = exporter.get_finished_spans()
    assert len(spans) == 3

    by_name = {s.name: s for s in spans}
    assert set(by_name) == {"session", "llm", "bash"}

    root = by_name["session"]
    assert root.attributes[SPAN_KIND] == "AGENT"
    assert root.attributes["session.id"] == "s1"
    assert root.attributes["session.success"] is True
    assert root.attributes["session.turns"] == 1

    llm = by_name["llm"]
    assert llm.attributes[SPAN_KIND] == "LLM"
    assert llm.attributes[LLM_TOKEN_PROMPT] == 10
    assert llm.attributes[LLM_TOKEN_COMPLETION] == 5

    tool = by_name["bash"]
    assert tool.attributes[SPAN_KIND] == "TOOL"
    assert tool.attributes[TOOL_NAME] == "bash"
    assert tool.attributes["tool.is_error"] is False

    # Children hang off the root span (same trace, parented to root).
    assert llm.parent is not None
    assert llm.context.trace_id == root.context.trace_id
    assert tool.parent.span_id == root.context.span_id


@pytest.mark.skipif(not OTEL_INSTALLED, reason="opentelemetry not installed")
def test_emit_spans_counts_compaction_and_verification(monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.setenv("GARUDA_TRACING", "console")
    exporter = InMemorySpanExporter()
    assert configure_tracing(service_name="garuda-test", span_exporter=exporter) is True

    events = [
        {
            "type": "session_start",
            "timestamp": "2026-07-02T10:00:00+00:00",
            "session_id": "s2",
            "payload": {"model": "gpt-test"},
        },
        {
            "type": "summarization",
            "timestamp": "2026-07-02T10:00:01+00:00",
            "session_id": "s2",
            "payload": {"turn": 2},
        },
        {
            "type": "verification",
            "timestamp": "2026-07-02T10:00:02+00:00",
            "session_id": "s2",
            "payload": {"approved": True, "feedback": "", "checklist": []},
        },
        {
            "type": "session_end",
            "timestamp": "2026-07-02T10:00:03+00:00",
            "session_id": "s2",
            "payload": {"success": True, "turns": 2},
        },
    ]

    # root + compaction + verification
    assert emit_spans_from_events(events, service_name="garuda-test") == 3
    names = {s.name for s in exporter.get_finished_spans()}
    assert names == {"session", "compaction", "verification"}
