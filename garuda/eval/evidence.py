"""Additive, content-free provenance for deterministic evaluation trials."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class EvaluationEvidence:
    task: str | None
    model: str | None
    runtime: str | None
    prompt: str | None
    tools: str | None
    environment: str | None
    checks: str | None
    config: str | None
    authority: str | None
    mechanism: str
    model_completed: bool | None
    authorized_acceptance: str
    coverage: str
    cost_reason: str
    variant: str | None = None
    minimal_config: str | None = None
    expected_failure: str | None = None
    version: int = 1

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("unsupported evaluation evidence version")
        for name in ("task", "model", "runtime", "prompt", "tools", "environment", "checks", "config",
                     "authority", "variant", "minimal_config"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"evaluation.{name} must be an identity or null")
        if self.model_completed is not None and type(self.model_completed) is not bool:
            raise ValueError("evaluation.model_completed must be boolean or null")
        if self.authorized_acceptance not in {"passed", "failed", "unavailable", "invalidated"}:
            raise ValueError("invalid authorized acceptance status")
        if self.coverage not in {"pass", "fail", "expected-fail", "unknown"}:
            raise ValueError("invalid evaluation coverage")
        if not isinstance(self.mechanism, str) or not self.mechanism:
            raise ValueError("evaluation mechanism is required")
        if not isinstance(self.cost_reason, str) or not self.cost_reason:
            raise ValueError("evaluation cost provenance is required")
        if self.expected_failure is not None and (
            not isinstance(self.expected_failure, str) or not self.expected_failure
        ):
            raise ValueError("expected failure must name the unguarded mechanism")
        if self.coverage == "expected-fail" and self.expected_failure is None:
            raise ValueError("expected-fail requires an unguarded mechanism")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != {f.name for f in fields(cls)}:
            raise ValueError("evaluation evidence must include every identity; use null for unknown")
        return cls(**data)
