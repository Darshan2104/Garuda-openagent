"""Direct model transport registry (P2.2, issue #48).

A direct transport joins this registry only after its vendor publishes the
supported public authentication and the request/session semantics Garuda
needs — no private endpoints, no reverse-engineered subscription paths, no
copied tokens, ever. Each record carries the six admission artifacts: a
vendor-support citation, a capability declaration, a test double, an opt-in
integration test, cost semantics, and migration notes. `admission_gaps`
refuses incomplete entries so a half-documented transport cannot slip in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransportRecord:
    id: str
    vendor: str
    support_citation: tuple[str, ...]
    auth: str
    capabilities: tuple[str, ...]
    test_double: str
    integration_test: str
    cost_semantics: str
    migration_notes: str = ""

    def admission_gaps(self) -> list[str]:
        """Missing admission artifacts. Empty means admissible."""
        gaps = []
        if not self.support_citation:
            gaps.append("vendor-support citation")
        if not self.capabilities:
            gaps.append("capability declaration")
        if not self.test_double:
            gaps.append("test double")
        if not self.integration_test:
            gaps.append("opt-in integration test")
        if not self.cost_semantics:
            gaps.append("cost semantics")
        return gaps

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "vendor": self.vendor,
            "support_citation": list(self.support_citation),
            "auth": self.auth,
            "capabilities": list(self.capabilities),
            "test_double": self.test_double,
            "integration_test": self.integration_test,
            "cost_semantics": self.cost_semantics,
            "migration_notes": self.migration_notes,
        }


TRANSPORTS: tuple[TransportRecord, ...] = (
    TransportRecord(
        id="litellm",
        vendor="LiteLLM (multi-provider, e.g. OpenRouter, Anthropic, OpenAI)",
        support_citation=(
            "https://docs.litellm.ai/",
            "https://openrouter.ai/docs",
        ),
        auth="Provider API keys via environment (e.g. OPENROUTER_API_KEY); "
        "never subscription OAuth material.",
        capabilities=("streaming", "tool-calling", "reasoning", "prompt-caching", "retries"),
        test_double="garuda.model.script_model.ScriptModel",
        integration_test="tests/test_model_transports.py::test_live_transport_opt_in",
        cost_semantics="Provider-reported cost first, then explicit overrides, "
        "then the versioned in-repo price snapshot; unknown stays unknown.",
        migration_notes="Default transport since v1. New transports add a record "
        "here; nothing migrates away unless the vendor withdraws support.",
    ),
)


def registry() -> dict[str, TransportRecord]:
    return {record.id: record for record in TRANSPORTS}


def assert_admissible(record: TransportRecord) -> None:
    gaps = record.admission_gaps()
    if gaps:
        raise ValueError(
            f"transport {record.id!r} is missing admission artifacts: {', '.join(gaps)}"
        )
    for citation in record.support_citation:
        if not citation.startswith("https://"):
            raise ValueError(f"transport {record.id!r} citation must be https: {citation!r}")
