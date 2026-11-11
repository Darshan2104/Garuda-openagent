"""Versioned evaluation inputs, separate from the product's default posture."""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass, fields
from functools import lru_cache
from importlib.resources import files

from garuda.types import AgentConfig


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_identity(*paths: str) -> str | None:
    root = files("garuda")
    try:
        return digest({path: hashlib.sha256(root.joinpath(path).read_bytes()).hexdigest() for path in paths})
    except OSError:
        return None


@lru_cache(maxsize=1)
def runtime_identity() -> str | None:
    """Identify the installed Garuda source, including the shared owners."""
    sources = {}

    def visit(directory, prefix=""):
        for entry in directory.iterdir():
            name = prefix + entry.name
            if entry.is_dir() and entry.name != "__pycache__":
                visit(entry, name + "/")
            elif entry.name.endswith(".py"):
                sources[name] = hashlib.sha256(entry.read_bytes()).hexdigest()

    try:
        visit(files("garuda"))
    except OSError:
        return None
    return digest(sources) if sources else None


def tool_identity(tools) -> str | None:
    definitions = []
    for tool in tools:
        module = type(tool).__module__
        source = source_identity(module.removeprefix("garuda.").replace(".", "/") + ".py")
        if source is None:
            return None
        definitions.append({"name": tool.name, "parameters": tool.parameters, "implementation": source})
    return digest(definitions)


def environment_identity() -> str:
    return digest({"kind": "local", "platform": platform.platform(), "python": platform.python_version(),
                   "implementation": source_identity("workspace/local.py")})


def authority_identity(permissions) -> str:
    policy = permissions.snapshot_policy()
    # Each trial has its own fresh directory under the same logical fixture
    # root. Absolute temporary directory names are not policy differences.
    policy["workspace_root"] = "<fixture-workspace>"
    return digest(policy)


@dataclass(frozen=True)
class MinimalConfig:
    version: int
    settings_json: str

    @classmethod
    def load(cls) -> MinimalConfig:
        data = json.loads(files("garuda.eval").joinpath("fixtures/minimal-v1.json").read_text())
        if (type(data["version"]) is not int or data["version"] != 1
                or set(data["settings"]) != {f.name for f in fields(AgentConfig)}):
            raise ValueError("minimal-v1 configuration schema drift; review and version the evaluation inputs")
        return cls(1, json.dumps(data["settings"], sort_keys=True))

    @property
    def identity(self) -> str:
        return digest({"version": self.version, "settings": json.loads(self.settings_json)})

    def config(self, *, mechanism: str | None = None, enabled: bool | None = None) -> AgentConfig:
        settings = json.loads(self.settings_json)
        if mechanism is not None:
            # Only single boolean mechanisms; authority, models, environments and
            # acceptance checks cannot hitch a ride in an ablation override.
            if mechanism not in {
                "enable_verifier", "require_discriminating_evidence", "require_stable_verification",
                "enable_request_preflight", "enable_adaptive_output", "enable_working_state_card",
            } or type(enabled) is not bool:
                raise ValueError("select one supported boolean evaluation mechanism")
            settings[mechanism] = enabled
        elif enabled is not None:
            raise ValueError("enabled requires a selected mechanism")
        return AgentConfig(**settings)
