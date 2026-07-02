"""Optional OpenTelemetry / OpenInference observability for Garuda.

Everything here is a safe no-op unless OpenTelemetry is installed *and*
tracing is explicitly enabled (see :func:`configure_tracing`).
"""

from garuda.observability.tracing import (
    configure_tracing,
    emit_spans_from_events,
    is_available,
    is_configured,
    reset_tracing,
    span,
)

__all__ = [
    "configure_tracing",
    "emit_spans_from_events",
    "is_available",
    "is_configured",
    "reset_tracing",
    "span",
]
