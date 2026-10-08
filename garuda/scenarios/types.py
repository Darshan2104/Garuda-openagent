"""Serializable starter definitions, launch inputs and typed refusals."""

from __future__ import annotations

from dataclasses import asdict, dataclass


class StarterError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Starter:
    version: int
    id: str
    title: str
    launch: dict
    fields: dict
    brief: str
    example: str


@dataclass(frozen=True)
class LaunchPlan:
    compiler_version: int
    starter_id: str
    starter_version: int
    workspace: str
    kind: str
    target: str
    inputs: dict
    task: str
    sources: list[dict]
    bindings: dict
    options: dict
    flow: dict | None
    provenance: dict
    checks: list[dict]
    limits: dict
    equivalent_command: str
    digest: str

    def to_dict(self) -> dict:
        return asdict(self)

    def launch_metadata(self) -> dict:
        """Bounded redacted inputs/provenance to retain for explicit follow-up."""
        return {"version": self.compiler_version, "starter_id": self.starter_id,
                "starter_version": self.starter_version, "inputs": self.inputs,
                "sources": self.sources, "plan_digest": self.digest,
                "inputs_sha256": self.provenance["inputs_sha256"],
                "approved_scope": self.provenance["approved_scope"],
                "approved_scope_sha256": self.provenance["approved_scope_sha256"],
                "task_sha256": self.provenance["task_sha256"],
                "configuration_digest": self.provenance["configuration_digest"]}
