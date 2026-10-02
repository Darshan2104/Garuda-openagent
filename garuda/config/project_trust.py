"""Hash-bound trust for a project's garuda.yaml (plan task C.2, #158).

A project file may narrow your roles freely. What it may **not** do without
your explicit, interactive trust is make Garuda run something it chose:

- its ``checks`` (project commands);
- a model string for the native harness (``roles.*.model_id`` where the
  harness is ``native``), which picks a provider and endpoint;
- prompt templates (none in schema v1; any future one joins this list).

Trust is a record in the content-bound store from #143
(``mcp/trust.py``), keyed by the canonical repository, the schema version and
the SHA-256 of the file's exact bytes. Any byte change means no trust. The file
is read **once**, without following symlinks, and the parsed values that run
are the ones parsed from the hashed bytes — so a file replaced or swapped for a
symlink after it was checked can never run as trusted.

Without trust the trust-needing values are **withheld** — left out of the
effective configuration and reported (``config.project_untrusted``) — and
everything else still applies. A headless run cannot create trust:
``garuda config trust`` needs a terminal.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from garuda.config import garuda_yaml as gy

MAX_BYTES = 1 << 20
UNTRUSTED = "config.project_untrusted"


@dataclass(frozen=True)
class ProjectFile:
    path: Path
    data: bytes
    repository: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def read_project_file(workspace) -> ProjectFile | None:
    """The project file's bytes, read once without following a symlink."""
    from garuda.core.sessions import project_root

    root = project_root(str(Path(workspace).expanduser().resolve()))
    path = Path(root) / "garuda.yaml"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise gy.GarudaConfigError("config.invalid", "", f"{path}: {exc} (a symlinked "
                                   "garuda.yaml is refused)") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise gy.GarudaConfigError("config.invalid", "", f"{path} is not a regular file")
        chunks, size = [], 0
        while chunk := os.read(fd, 65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise gy.GarudaConfigError("config.invalid", "", f"{path} is over 1 MiB")
            chunks.append(chunk)
    finally:
        os.close(fd)
    return ProjectFile(path=path, data=b"".join(chunks), repository=str(Path(root).resolve()))


def _key(project: ProjectFile):
    from garuda.mcp.trust import TrustKey

    return TrustKey(repository=project.repository, name=f"garuda.yaml#schema-{gy.VERSION}",
                    digest=project.digest)


def is_trusted(project: ProjectFile) -> bool:
    from garuda.mcp.trust import has_grant

    return has_grant(_key(project))


def grant(project: ProjectFile):
    """Record trust in these exact bytes. Callers must have asked the user."""
    from garuda.mcp.trust import grant_key

    return grant_key(_key(project))


def needs_trust(doc: dict) -> list[str]:
    """Paths of the values that need trust before they can run."""
    paths = [f"checks[{i}]" for i in range(len(doc.get("checks", [])))]
    for name, role in doc.get("roles", {}).items():
        if role.get("harness") == "native" and "model_id" in role:
            paths.append(f"roles.{name}.model_id")
    return paths


def withhold(doc: dict) -> tuple[dict, list[str]]:
    """``doc`` without the values that need trust, and their paths."""
    paths = needs_trust(doc)
    if not paths:
        return doc, []
    out = dict(doc)
    out.pop("checks", None)
    roles = {}
    for name, role in doc.get("roles", {}).items():
        if role.get("harness") == "native" and "model_id" in role:
            role = {k: v for k, v in role.items() if k != "model_id"}
        roles[name] = role
    if roles:
        out["roles"] = roles
    return out, paths


@dataclass
class LoadedProject:
    doc: dict | None
    trusted: bool = False
    withheld: list[str] = field(default_factory=list)
    file: ProjectFile | None = None


def load_project(workspace) -> LoadedProject:
    """Parse the project file from the exact bytes read, withholding what needs
    trust unless those bytes are trusted."""
    project = read_project_file(workspace)
    if project is None:
        return LoadedProject(None)
    try:
        text = project.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise gy.GarudaConfigError("config.invalid", "", f"{project.path} is not UTF-8") from exc
    doc = gy.load_text(text, source=str(project.path))
    if not needs_trust(doc) or is_trusted(project):
        return LoadedProject(doc, trusted=bool(needs_trust(doc)), file=project)
    kept, withheld = withhold(doc)
    return LoadedProject(kept, trusted=False, withheld=withheld, file=project)
