"""Immutable inputs for the internal asynchronous acceptance boundary."""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from typing import Protocol

from garuda.core.acceptance import ACCEPTANCE
from garuda.core.acceptance_records import digest


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def is_digest(value, size=64):
    return isinstance(value, str) and len(value) == size and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class FrozenSource:
    path: str
    digest: str

    def document(self):
        if not isinstance(self.path, str) or not os.path.isabs(self.path) or not is_digest(self.digest):
            raise ValueError("invalid frozen check source")
        return {"path": self.path, "digest": self.digest}


@dataclass(frozen=True)
class FrozenCheck:
    run: str | tuple[str, ...]
    authority: str
    cwd: str = "."
    mode: str = "host"
    env: tuple[tuple[str, str], ...] = ()

    def document(self):
        if self.authority not in ACCEPTANCE or self.mode not in {"host", "docker"}:
            raise ValueError("check authority or environment is unsupported")
        if not isinstance(self.run, (str, tuple)) or not self.run:
            raise ValueError("check command is empty")
        words = (self.run,) if isinstance(self.run, str) else self.run
        if not words[0] or any(not isinstance(word, str) or "\0" in word for word in words):
            raise ValueError("invalid check command")
        if not isinstance(self.cwd, str) or not self.cwd or "\0" in self.cwd:
            raise ValueError("invalid check cwd")
        if (not isinstance(self.env, tuple) or any(not isinstance(pair, tuple) or len(pair) != 2
                                                 for pair in self.env)
                or len(dict(self.env)) != len(self.env)):
            raise ValueError("check environment must be immutable and unique")
        if any(not isinstance(k, str) or not k or "=" in k or "\0" in k
               or not isinstance(v, str) or "\0" in v for k, v in self.env):
            raise ValueError("invalid check environment")
        return {"run": self.run, "authority": self.authority, "cwd": self.cwd,
                "mode": self.mode, "env": dict(self.env)}

    @property
    def identity(self):
        return digest(self.document())


@dataclass(frozen=True)
class CheckLimits:
    timeout: float = 900
    phase_timeout: float = 3600
    term_grace: float = 3
    kill_grace: float = 3

    def document(self):
        result = {"timeout": self.timeout, "phase_timeout": self.phase_timeout,
                  "term_grace": self.term_grace, "kill_grace": self.kill_grace}
        maxima = {"timeout": 900, "phase_timeout": 3600, "term_grace": 3, "kill_grace": 3}
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not 0 < value <= maxima[key] for key, value in result.items()):
            raise ValueError("acceptance bounds exceed trusted policy")
        return result


@dataclass(frozen=True)
class FrozenCheckRequest:
    checks: tuple[FrozenCheck, ...]
    sources: tuple[FrozenSource, ...]
    ceiling_policy: str
    limits: CheckLimits = CheckLimits()

    @classmethod
    def freeze(cls, checks, sources, permissions, *, limits=None):
        result = cls(tuple(checks), tuple(sources), canonical(permissions.snapshot_policy()),
                     limits if limits is not None else CheckLimits())
        result.document()
        return result

    def document(self):
        if not isinstance(self.checks, tuple) or not isinstance(self.sources, tuple) or (
                len(self.checks) > 32 or len(self.sources) > 32):
            raise ValueError("check request exceeds its immutable bounds")
        if any(check.authority != "user-request" for check in self.checks) and not self.sources:
            raise ValueError("configured checks require frozen source authority")
        result = {"checks": [c.document() for c in self.checks],
                  "sources": [s.document() for s in self.sources],
                  "ceiling": json.loads(self.ceiling_policy), "limits": self.limits.document()}
        if len(canonical(result).encode()) > 32_768:
            raise ValueError("check request exceeds its byte bound")
        return result

    @property
    def identity(self):
        return digest(self.document())

    @property
    def authority_digest(self):
        return digest({"checks": [c.identity for c in self.checks],
                       "sources": [s.document() for s in self.sources]})

    @property
    def ceiling_digest(self):
        return digest(json.loads(self.ceiling_policy))

    def validate_ceiling(self, permissions):
        if canonical(permissions.snapshot_policy()) != self.ceiling_policy:
            raise ValueError("effective check permission ceiling changed")


@dataclass(frozen=True)
class CheckContext:
    parent_id: str
    phase_id: str
    workspace: str
    candidate: str
    evidence_digest: str
    changed_paths: tuple[str, ...]
    evidence_complete: bool
    workspace_identity: tuple[int, int]

    def validate(self):
        if any(str(uuid.UUID(v)) != v for v in (self.parent_id, self.phase_id)):
            raise ValueError("noncanonical acceptance parent or phase")
        if not os.path.isabs(self.workspace) or not is_digest(self.candidate, 16) or not is_digest(self.evidence_digest):
            raise ValueError("invalid acceptance context binding")
        if (not isinstance(self.workspace_identity, tuple) or len(self.workspace_identity) != 2
                or any(type(v) is not int or v < 0 for v in self.workspace_identity)):
            raise ValueError("workspace identity is required")
        if (self.evidence_complete is not True or not isinstance(self.changed_paths, tuple)
                or len(self.changed_paths) > 100_000 or any(not isinstance(p, str) for p in self.changed_paths)
                or len(canonical(self.changed_paths).encode()) > 16 * 1024 * 1024):
            raise ValueError("complete bounded changed-path evidence is required")


class CheckOwner(Protocol):
    workspace: str
    session_id: str

    def snapshot_ownership(self) -> dict: ...
    async def stop_heartbeat(self) -> None: ...
