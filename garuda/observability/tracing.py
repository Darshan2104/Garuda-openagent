"""Optional OpenTelemetry / OpenInference tracing for Garuda.

Design goal: zero hard dependency. OpenTelemetry and OpenInference are
optional. When the SDK is not installed OR tracing is not enabled, every
public function here is a safe no-op and no import errors are raised.

Spans are derived from a finished session's :class:`~garuda.core.events.EventStore`
records, so the agent loop never needs to know that tracing exists. A live
:func:`span` context manager is also provided for future call sites (or the
ATIF exporter) to wrap operations; it is a no-op when tracing is disabled.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# OpenInference semantic-convention attribute keys. We set these strings
# directly so we never hard-depend on the ``openinference`` package.
# ---------------------------------------------------------------------------
SPAN_KIND = "openinference.span.kind"
LLM_TOKEN_PROMPT = "llm.token_count.prompt"
LLM_TOKEN_COMPLETION = "llm.token_count.completion"
LLM_TOKEN_TOTAL = "llm.token_count.total"
LLM_MODEL_NAME = "llm.model_name"
TOOL_NAME = "tool.name"

# Span-kind values used across the span tree.
KIND_AGENT = "AGENT"
KIND_LLM = "LLM"
KIND_TOOL = "TOOL"
KIND_CHAIN = "CHAIN"

_FALSEY = {"", "0", "false", "no", "off", "none"}

# Module-level tracing state. We keep our own provider reference rather than
# relying solely on the global one so re-configuration (e.g. in tests) works
# and does not fight OpenTelemetry's "set global provider once" behaviour.
_PROVIDER: Any = None
_CONFIGURED: bool = False
_SERVICE_NAME: str = "garuda"


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in _FALSEY


def is_available() -> bool:
    """Return True if the ``opentelemetry`` API is importable."""
    try:
        import opentelemetry  # noqa: F401

        return True
    except Exception:
        return False


def is_configured() -> bool:
    """Return True if tracing has been configured and is active."""
    return _CONFIGURED and _PROVIDER is not None


def reset_tracing() -> None:
    """Shut down and clear any configured tracer provider (no-op if none)."""
    global _PROVIDER, _CONFIGURED
    provider = _PROVIDER
    _PROVIDER = None
    _CONFIGURED = False
    if provider is not None:
        try:
            provider.shutdown()
        except Exception:
            pass


def configure_tracing(
    service_name: str = "garuda",
    endpoint: str | None = None,
    enabled: bool | None = None,
    *,
    span_exporter: Any = None,
) -> bool:
    """Set up a tracer provider for Garuda.

    Args:
        service_name: ``service.name`` resource attribute for emitted spans.
        endpoint: OTLP endpoint. Falls back to ``OTEL_EXPORTER_OTLP_ENDPOINT``.
        enabled: Force enable/disable. Defaults to ``GARUDA_TRACING`` being
            truthy OR ``endpoint`` being provided.
        span_exporter: Optional explicit span exporter (mainly for tests, e.g.
            an in-memory exporter). Overrides the default OTLP/console choice.

    Returns:
        True if tracing was configured, False if it was a no-op (OTel not
        installed or tracing not enabled).
    """
    global _PROVIDER, _CONFIGURED, _SERVICE_NAME

    env_value = os.environ.get("GARUDA_TRACING")
    if enabled is None:
        enabled = _truthy(env_value) or endpoint is not None
    if not enabled:
        return False
    if not is_available():
        return False

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
    except Exception:
        # SDK not installed (only the API is) -> stay a no-op.
        return False

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    exporter = span_exporter
    processor_cls: Any = SimpleSpanProcessor
    if exporter is None:
        console_mode = (env_value or "").strip().lower() == "console"
        if console_mode:
            exporter = ConsoleSpanExporter()
        else:
            resolved = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            exporter = _make_otlp_exporter(resolved)
            if exporter is not None:
                processor_cls = BatchSpanProcessor
            else:
                exporter = ConsoleSpanExporter()

    provider.add_span_processor(processor_cls(exporter))

    _PROVIDER = provider
    _CONFIGURED = True
    _SERVICE_NAME = service_name

    # Best-effort: expose as the global provider for any other instrumentation.
    try:
        from opentelemetry import trace

        trace.set_tracer_provider(provider)
    except Exception:
        pass

    return True


def _make_otlp_exporter(endpoint: str | None) -> Any:
    """Build an OTLP span exporter, trying gRPC then HTTP. None on failure."""
    candidates = (
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    )
    import importlib

    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
            exporter_cls = getattr(module, "OTLPSpanExporter")
            return exporter_cls(endpoint=endpoint) if endpoint else exporter_cls()
        except Exception:
            continue
    return None


def _tracer() -> Any:
    return _PROVIDER.get_tracer(_SERVICE_NAME)


def _iso_to_nanos(timestamp: str | None) -> int | None:
    """Convert an ISO-8601 timestamp to epoch nanoseconds, or None."""
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None
    return int(dt.timestamp() * 1_000_000_000)


def _set_attrs(span: Any, attrs: dict[str, Any]) -> None:
    for key, value in attrs.items():
        if value is not None:
            span.set_attribute(key, value)


@contextmanager
def span(name: str, kind: str = KIND_CHAIN, **attrs: Any) -> Iterator[Any]:
    """Live span context manager, e.g. ``with span("op", "TOOL"):``.

    A no-op (yields None) when tracing is unavailable or not configured.
    """
    if not (is_available() and is_configured()):
        yield None
        return

    tracer = _tracer()
    with tracer.start_as_current_span(name) as current:
        current.set_attribute(SPAN_KIND, kind)
        _set_attrs(current, attrs)
        yield current


def emit_spans_from_events(events: list[dict], service_name: str = "garuda") -> int:
    """Walk a finished session's events and emit an OpenInference span tree.

    Builds one root AGENT span for the session, child LLM spans per
    ``model_response``, child TOOL spans per ``tool_call``/``tool_result``
    pair, plus COMPACTION (summarization) and VERIFICATION spans.

    Returns the number of spans emitted, or 0 when tracing is unavailable or
    not configured (never raises for those cases).
    """
    if not (is_available() and is_configured()):
        return 0
    if not events:
        return 0

    from opentelemetry.trace import Status, StatusCode, set_span_in_context

    tracer = _tracer()

    session_start = _first(events, "session_start")
    session_end = _last(events, "session_end")

    session_id = events[0].get("session_id")
    model = (session_start or {}).get("payload", {}).get("model")

    start_ns = _iso_to_nanos((session_start or events[0]).get("timestamp"))
    end_ns = _iso_to_nanos((session_end or events[-1]).get("timestamp"))

    end_payload = (session_end or {}).get("payload", {})
    root_attrs = {
        SPAN_KIND: KIND_AGENT,
        "session.id": session_id,
        LLM_MODEL_NAME: model,
        "session.success": end_payload.get("success"),
        "session.turns": end_payload.get("turns"),
    }

    root = tracer.start_span("session", start_time=start_ns)
    _set_attrs(root, root_attrs)
    count = 1
    root_ctx = set_span_in_context(root)

    # Index tool results by their call id so we can pair them with calls.
    tool_results: dict[Any, dict] = {}
    for event in events:
        if event.get("type") == "tool_result":
            payload = event.get("payload", {})
            key = payload.get("tool_call_id") or payload.get("id")
            if key is not None:
                tool_results[key] = event

    for event in events:
        event_type = event.get("type")
        payload = event.get("payload", {})
        ts_ns = _iso_to_nanos(event.get("timestamp"))

        if event_type == "model_response":
            usage = payload.get("usage") or {}
            child = tracer.start_span("llm", context=root_ctx, start_time=ts_ns)
            _set_attrs(
                child,
                {
                    SPAN_KIND: KIND_LLM,
                    LLM_MODEL_NAME: model,
                    LLM_TOKEN_PROMPT: usage.get("prompt_tokens"),
                    LLM_TOKEN_COMPLETION: usage.get("completion_tokens"),
                    LLM_TOKEN_TOTAL: usage.get("total_tokens"),
                    "llm.finish_reason": payload.get("finish_reason"),
                },
            )
            child.end(end_time=ts_ns)
            count += 1

        elif event_type == "tool_call":
            call_id = payload.get("id") or payload.get("tool_call_id")
            result = tool_results.get(call_id)
            is_error = bool((result or {}).get("payload", {}).get("is_error"))
            end_tool_ns = _iso_to_nanos((result or event).get("timestamp"))
            child = tracer.start_span(
                payload.get("name") or "tool",
                context=root_ctx,
                start_time=ts_ns,
            )
            _set_attrs(
                child,
                {
                    SPAN_KIND: KIND_TOOL,
                    TOOL_NAME: payload.get("name"),
                    "tool.is_error": is_error,
                },
            )
            if is_error:
                child.set_status(Status(StatusCode.ERROR))
            child.end(end_time=end_tool_ns)
            count += 1

        elif event_type == "summarization":
            child = tracer.start_span("compaction", context=root_ctx, start_time=ts_ns)
            _set_attrs(child, {SPAN_KIND: KIND_CHAIN, "compaction.turn": payload.get("turn")})
            child.end(end_time=ts_ns)
            count += 1

        elif event_type == "verification":
            child = tracer.start_span("verification", context=root_ctx, start_time=ts_ns)
            _set_attrs(
                child,
                {
                    SPAN_KIND: KIND_CHAIN,
                    "verification.approved": payload.get("approved"),
                },
            )
            if payload.get("approved") is False:
                child.set_status(Status(StatusCode.ERROR))
            child.end(end_time=ts_ns)
            count += 1

    root.end(end_time=end_ns)

    # Flush so batch processors export before the caller inspects results.
    try:
        _PROVIDER.force_flush()
    except Exception:
        pass

    return count


def _first(events: list[dict], event_type: str) -> dict | None:
    for event in events:
        if event.get("type") == event_type:
            return event
    return None


def _last(events: list[dict], event_type: str) -> dict | None:
    found = None
    for event in events:
        if event.get("type") == event_type:
            found = event
    return found
