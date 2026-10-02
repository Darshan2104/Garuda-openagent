"""Consult refusals carry a stable code (plan task G.2, #170)."""

CODES = (
    "consult.not_granted", "consult.nested", "consult.invalid", "consult.too_long",
    "consult.limit", "consult.busy", "consult.payload_changed", "consult.capacity_unavailable",
    "consult.target_unavailable", "consult.snapshot_unsupported", "consult.snapshot_unstable",
    "consult.isolation_unavailable", "consult.timeout", "consult.cancelled",
    "consult.failed", "consult.unexpected_changes", "consult.interrupted", "consult.quarantined",
)


class ConsultRefused(Exception):
    """A consult that did not produce an answer. ``code`` is stable; ``message`` never
    contains the question or the answer."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
