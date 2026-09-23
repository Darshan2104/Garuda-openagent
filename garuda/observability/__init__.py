"""Observability for Garuda: trajectory reading, and optional OpenTelemetry export.

:mod:`garuda.observability.trajectory` is always available and depends on nothing
outside the standard library — it rebuilds the turn structure of a finished or
in-flight run from its ``events.jsonl``.

Everything OpenTelemetry-related is a safe no-op unless the SDK is installed *and*
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
from garuda.observability.trajectory import (
    EventTail,
    Run,
    TrajectoryReader,
    Turn,
    build_run,
    find_events_file,
    read_trajectory,
)

__all__ = [
    "EventTail",
    "Run",
    "TrajectoryReader",
    "Turn",
    "build_run",
    "configure_tracing",
    "emit_spans_from_events",
    "find_events_file",
    "is_available",
    "is_configured",
    "read_trajectory",
    "reset_tracing",
    "span",
]
