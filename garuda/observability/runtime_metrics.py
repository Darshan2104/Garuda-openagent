"""Runtime metrics and failure triage (P1.13, issue #45).

`RuntimeMetrics` records one adapter's lifecycle — startup, negotiation, turn
durations, approvals, handoffs, errors, adapter version, cleanup — without
touching native accounting (`core/metrics.py` is neither imported nor altered;
its schema is pinned by its own tests). `classify_error` maps every failure to
exactly one triage bucket so on-call can tell protocol, harness, permission,
workspace, and verifier failures apart at a glance.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

PHASES = (
    "startup",
    "negotiation",
    "turn",
    "approval",
    "handoff",
    "cleanup",
)

TRIAGE_BUCKETS = (
    "protocol",
    "harness",
    "permission",
    "workspace",
    "verifier",
    "unknown",
)


def classify_error(exc: BaseException) -> str:
    """Map a failure to its triage bucket. Unknown exceptions stay unknown —
    a wrong bucket misdirects the whole investigation."""
    from garuda.acp.protocol import (
        AcpCancelledError,
        AcpExitError,
        AcpProtocolError,
        AcpTimeoutError,
    )

    name = type(exc).__name__
    module = type(exc).__module__
    if isinstance(exc, AcpProtocolError):
        return "protocol"
    if isinstance(exc, (AcpExitError, AcpTimeoutError, AcpCancelledError)):
        return "harness"
    if "permission" in module.lower() or "Permission" in name or "Approval" in name:
        return "permission"
    if "workspace" in module.lower() or name in ("LeaseError", "LeaseConflictError", "DiffError"):
        return "workspace"
    if "verifier" in module.lower() or "Verification" in name:
        return "verifier"
    return "unknown"


@dataclass
class RuntimeMetrics:
    adapter_version: str = "unknown"
    counts: dict[str, int] = field(default_factory=dict)
    durations_ms: dict[str, list[float]] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)

    def note(self, phase: str, duration_ms: float) -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown metric phase {phase!r}")
        self.counts[phase] = self.counts.get(phase, 0) + 1
        self.durations_ms.setdefault(phase, []).append(duration_ms)

    def note_error(self, exc: BaseException) -> str:
        bucket = classify_error(exc)
        self.errors[bucket] = self.errors.get(bucket, 0) + 1
        return bucket

    @contextmanager
    def timed(self, phase: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.note(phase, (time.monotonic() - started) * 1000.0)

    def to_dict(self) -> dict:
        summary = {}
        for phase in PHASES:
            samples = sorted(self.durations_ms.get(phase, []))
            summary[phase] = {
                "count": self.counts.get(phase, 0),
                "median_ms": samples[len(samples) // 2] if samples else None,
            }
        return {
            "adapter_version": self.adapter_version,
            "phases": summary,
            "errors": {bucket: self.errors.get(bucket, 0) for bucket in TRIAGE_BUCKETS},
        }
