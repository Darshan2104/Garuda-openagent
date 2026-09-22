"""Pre-start safety: traits, candidate validation, workspace baseline.

Trait detection is bounded direct-filesystem inspection only: no shell, no
subprocess, no project-code import. Validation fails closed before any
segment is created. The workspace baseline recheck uses
:mod:`garuda.workspace.diff` as the truth.
"""

from __future__ import annotations

import os
from pathlib import Path

from .models import (
    MAX_MARKER_CHECKS,
    MAX_TRAIT_DEPTH,
    MAX_TRAIT_ENTRIES,
    MAX_TRAIT_FILES,
    InitialCandidate,
    InitialRequest,
    RepoTraits,
    SelectionError,
)

#: Heavy directories skipped during the bounded walk. The walk is a routing
#: hint, not an inventory: skipping vendored trees keeps it bounded, and
#: marker checks below still probe exact paths directly.
_SKIPPED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)

_EXTENSION_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".c": "c",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
    ".r": "r",
    ".lua": "lua",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".tf": "terraform",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
}

_FILENAME_LANGUAGES = {
    "dockerfile": "docker",
    "makefile": "make",
    "cmakelists.txt": "cmake",
}


def detect_repo_traits(
    workspace: str | Path,
    *,
    markers: tuple[str, ...] | list[str] = (),
    max_files: int = MAX_TRAIT_FILES,
    max_entries: int = MAX_TRAIT_ENTRIES,
    max_depth: int = MAX_TRAIT_DEPTH,
) -> RepoTraits:
    """Inspect a workspace with bounded, side-effect-free directory reads.

    Only entry names and extensions are observed: no file is opened, no
    subprocess runs, and no project code is imported. Symlinked directories
    are listed, never followed, so a link pointing outside the workspace
    cannot widen the scan. ``markers`` names extra basenames to probe with a
    direct existence check (bounded by :data:`MAX_MARKER_CHECKS`) so a rule
    can match a marker the truncated walk did not reach.
    """
    root = Path(workspace)
    marker_names = [str(m) for m in (markers or ())]
    if len(marker_names) > MAX_MARKER_CHECKS:
        raise SelectionError(
            f"trait detection: too many marker checks ({len(marker_names)} > {MAX_MARKER_CHECKS})"
        )
    languages: set[str] = set()
    root_markers: set[str] = set()
    files: list[str] = []
    entries = 0
    truncated = False
    if root.is_dir():
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and len(files) < max_files and entries < max_entries:
            current, depth = stack.pop()
            try:
                with os.scandir(current) as handle:
                    names = sorted((entry.name, entry) for entry in handle)
            except OSError:
                continue
            for name, entry in names:
                entries += 1
                if entries > max_entries or len(files) >= max_files:
                    # The bounds stopped the scan, not the tree.
                    truncated = True
                    break
                try:
                    # Never follow symlinked directories: list, do not descend,
                    # so a link pointing outside the workspace cannot widen
                    # the scan.
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if depth < max_depth and name not in _SKIPPED_DIRS:
                        stack.append((Path(entry.path), depth + 1))
                    continue
                try:
                    rel = Path(entry.path).relative_to(root).as_posix()
                except ValueError:
                    continue
                files.append(rel)
                lowered = name.lower()
                if depth == 0:
                    # Root-level basenames are the classic marker files
                    # (pyproject.toml, package.json, ...).
                    root_markers.add(lowered)
                language = _EXTENSION_LANGUAGES.get(Path(lowered).suffix)
                if language is None:
                    language = _FILENAME_LANGUAGES.get(lowered)
                if language is not None:
                    languages.add(language)
        # Work left over means the bounds stopped the scan, not the tree.
        truncated = truncated or bool(stack)
    probed: set[str] = set()
    for marker in marker_names:
        parts = Path(marker).parts
        # Direct probes are basenames only: absolute paths and ".." would
        # escape the workspace, and nested markers are already covered by
        # the walk above (or by path_globs rules).
        if not marker or Path(marker).is_absolute() or ".." in parts or len(parts) != 1:
            continue
        lowered = marker.lower()
        if lowered in root_markers:
            continue
        try:
            if (root / marker).is_file():
                probed.add(lowered)
        except OSError:
            continue
    files_sorted = tuple(sorted(files[:max_files]))
    return RepoTraits(
        languages=frozenset(sorted(languages)),
        marker_files=frozenset(sorted(root_markers | probed)),
        files=files_sorted,
        file_count=len(files_sorted),
        truncated=truncated,
    )


def validate_candidate(
    candidate: InitialCandidate, request: InitialRequest
) -> tuple[bool, str]:
    """Pre-start validation: health, auth, capabilities, workspace, policy.

    Anything ambiguous fails closed with a reason; the facade's
    ``select_initial`` records the reason instead of starting the runtime.
    """
    if not candidate.available:
        return False, f"{candidate.runtime_id}: unavailable ({candidate.unavailable_reason or 'no reason given'})"
    if candidate.health == "unavailable":
        return False, f"{candidate.runtime_id}: health is unavailable"
    if candidate.auth in ("unauthenticated", "expired"):
        return False, f"{candidate.runtime_id}: auth is {candidate.auth}"
    missing = set(request.required_capabilities) - set(candidate.capabilities)
    if missing:
        return False, f"{candidate.runtime_id}: lacks capabilities {sorted(missing)}"
    if candidate.workspace_kinds is not None and request.workspace_kind not in candidate.workspace_kinds:
        return (
            False,
            f"{candidate.runtime_id}: does not support workspace {request.workspace_kind!r}",
        )
    if (
        candidate.permission_ceilings is not None
        and request.permission_ceiling not in candidate.permission_ceilings
    ):
        return (
            False,
            f"{candidate.runtime_id}: does not support ceiling {request.permission_ceiling!r}",
        )
    return True, ""


def _candidate_table(
    candidates: list[InitialCandidate] | tuple[InitialCandidate, ...],
) -> dict[str, InitialCandidate]:
    table: dict[str, InitialCandidate] = {}
    for candidate in candidates:
        if candidate.runtime_id in table:
            raise SelectionError(f"duplicate candidate {candidate.runtime_id!r}")
        table[candidate.runtime_id] = candidate
    return table


def workspace_unchanged(baseline_before: object, workspace: str | Path) -> tuple[bool, str]:
    """Recheck the workspace baseline before a startup fallback.

    Uses :mod:`garuda.workspace.diff` as the truth: any non-preexisting
    change since ``baseline_before`` blocks automatic fallback, because the
    failed start may have mutated the tree. Outside a repository (or when
    git cannot answer) the check fails closed and blocks fallback.
    """
    try:
        from garuda.workspace import diff as workspace_diff
    except ImportError as exc:
        return False, f"baseline recheck unavailable: {exc}"
    try:
        if hasattr(baseline_before, "commit"):
            baseline = baseline_before  # type: ignore[assignment]
        else:
            baseline = workspace_diff.Baseline.from_dict(dict(baseline_before))  # type: ignore[arg-type]
        if not baseline.commit:
            # No baseline commit means the session started outside a
            # repository (or git could not answer then): there is nothing
            # to compare against, so fallback is refused rather than
            # allowed unverified.
            return False, "no baseline commit to compare against"
        delta = workspace_diff.session_delta(baseline, workspace)
    except Exception as exc:
        return False, f"baseline recheck failed closed: {exc}"
    if delta.changed:
        return False, f"workspace changed since baseline: {sorted(delta.changed)[:8]}"
    return True, "workspace matches baseline"
