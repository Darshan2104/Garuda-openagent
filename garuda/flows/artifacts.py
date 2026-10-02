"""Typed step artifacts (plan task C.6a, #158).

A step produces an artifact only through the structured-output envelope at
the end of its final output::

    <garuda-artifact type="plan">
    ...
    </garuda-artifact>

Free text never satisfies an artifact, and any attribute other than ``type``
— a ``path`` above all — is ignored: Garuda decides where an artifact lives.
Each artifact is bounded, stored once (exclusive, owner-only, no symlinks)
under the flow's own directory, and described by a reference: type, digest,
producer step and session, attempt, size, the workspace version it was
produced against, and its flow-relative path.

Before a step runs, every input it declares must resolve to an earlier
step's artifact whose file is still a regular, non-symlink file inside the
flow directory with the recorded digest and size; a workspace-bound one
(``patch``, ``review``, ``findings``) must also have been produced against the
workspace as it is now. A forged, escaping, symlinked or stale input refuses.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from garuda.config.garuda_yaml import ARTIFACTS

MAX_ARTIFACT_CHARS = 64_000
ARTIFACT_VERSION = 1
#: Artifacts that describe the workspace as it was: stale once it changes. A
#: plan or notes describe the task and stay usable across a retry (C.7).
WORKSPACE_BOUND = ("patch", "review", "findings")
_BLOCK = re.compile(
    r"<garuda-artifact\s+([^>]*)>\n?(.*?)\n?</garuda-artifact>", re.DOTALL)
_TYPE = re.compile(r'\btype\s*=\s*"([a-z]+)"')


class ArtifactError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ArtifactRef:
    type: str
    digest: str
    producer_step: str
    producer_session: str
    attempt: int
    size: int
    workspace_version: str | None
    path: str  # relative to the flow directory
    version: int | None = ARTIFACT_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ArtifactRef":
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k != "version"}
        return cls(**fields, version=data.get("version"))


def extract(output: str) -> dict[str, str]:
    """``{type: content}`` from the envelope blocks in ``output``; the last
    block of a type wins. Unknown types are ignored."""
    found: dict[str, str] = {}
    for attrs, body in _BLOCK.findall(output or ""):
        match = _TYPE.search(attrs)
        if not match or match.group(1) not in ARTIFACTS:
            continue
        found[match.group(1)] = body
    return found


def instructions(outputs: list[str]) -> str:
    if not outputs:
        return ""
    lines = ["End your answer with one block per output, exactly like this:"]
    lines += [f'<garuda-artifact type="{t}">\n...your {t}...\n</garuda-artifact>' for t in outputs]
    lines.append("Only these blocks are passed on; any text outside them is not.")
    return "\n".join(lines)


def store(flow_dir: Path, *, type: str, content: str, step: str, session_id: str,
          attempt: int, workspace_version: str | None) -> ArtifactRef:
    if type not in ARTIFACTS:
        raise ArtifactError("flow.artifact_type", f"{type!r} is not an artifact type")
    if len(content) > MAX_ARTIFACT_CHARS:
        raise ArtifactError("flow.artifact_too_large",
                            f"{type} from {step} is {len(content)} characters "
                            f"(at most {MAX_ARTIFACT_CHARS})")
    data = content.encode("utf-8")
    directory = flow_dir / "artifacts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    rel = f"artifacts/{step}-{attempt}-{type}.txt"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(flow_dir / rel, flags, 0o400)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return ArtifactRef(type=type, digest=hashlib.sha256(data).hexdigest(), producer_step=step,
                       producer_session=session_id, attempt=attempt, size=len(data),
                       workspace_version=workspace_version, path=rel)


def load(flow_dir: Path, ref: ArtifactRef, *, workspace_version: str | None) -> str:
    """The artifact's content, after every check in the module docstring."""
    if ref.version != ARTIFACT_VERSION:
        raise ArtifactError("flow.input_version",
                            f"{ref.type} from {ref.producer_step} is artifact version "
                            f"{ref.version!r}; this Garuda reads version {ARTIFACT_VERSION}")
    root = flow_dir.resolve()
    path = (flow_dir / ref.path)
    if Path(ref.path).is_absolute() or ".." in Path(ref.path).parts:
        raise ArtifactError("flow.input_escapes", f"{ref.path} leaves the flow directory")
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise ArtifactError("flow.input_missing", f"{ref.path} is gone") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArtifactError("flow.input_not_regular", f"{ref.path} is not a regular file")
    if root not in path.resolve().parents:
        raise ArtifactError("flow.input_escapes", f"{ref.path} leaves the flow directory")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        data = os.read(fd, MAX_ARTIFACT_CHARS * 4 + 1)
    finally:
        os.close(fd)
    if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.digest:
        raise ArtifactError("flow.input_forged",
                            f"{ref.path} does not match the digest its step recorded")
    if ref.type in WORKSPACE_BOUND and ref.workspace_version != workspace_version:
        raise ArtifactError(
            "flow.input_stale",
            f"{ref.type} from {ref.producer_step} was produced against another version "
            "of the workspace",
        )
    return data.decode("utf-8")
