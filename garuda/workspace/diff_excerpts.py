"""Bounded review views of the existing session delta.

Git output is spooled outside the workspace to count exact diff-byte omissions
without keeping a full patch in memory. Counts describe generated patch bytes,
not source-file sizes (in particular, not binary payload sizes).
"""

from __future__ import annotations

import json
import stat
import tempfile
from html import escape
from pathlib import Path, PurePosixPath

from garuda.context.redact import redact_text
from garuda.workspace.diff import _EMPTY_TREE, Baseline, DiffError, SessionDelta, _git

MAX_FILE_BYTES = 4096
MAX_TOTAL_BYTES = 32 * 1024
MAX_FILES = 200


def _bounded_text(text, limit):
    # Match render_packet's JSON string contents and HTML escaping.
    used = 0
    for index, char in enumerate(text):
        size = len(escape(json.dumps(char)[1:-1]).encode("utf-8"))
        if used + size > limit:
            return text[:index], used
        used += size
    return text, used


def _read_z(output):
    row = bytearray()
    while byte := output.read(1):
        if byte == b"\0":
            return row
        if len(row) > 65536:
            raise DiffError("diff numstat row is too large")
        row.extend(byte)
    raise DiffError("incomplete diff numstat")


def _target(root, path):
    parts = PurePosixPath(path).parts
    if (not parts or path.startswith("/") or ".." in parts or ".git" in parts
            or "\x00" in path or "\\" in path):
        raise DiffError("not a workspace-relative file path")
    target = root / path
    parent = target.parent.resolve()
    if not parent.is_relative_to(root):
        raise DiffError("file parent resolves outside the workspace")
    if ".git" in parent.relative_to(root).parts:
        raise DiffError("file parent resolves inside git metadata")
    return target


def _excerpt(workspace, baseline, file, limit):
    path = file.path
    root = Path(workspace).resolve()
    target = _target(root, path)
    if file.renamed_from:
        _target(root, file.renamed_from)
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        raise DiffError("not a regular file or symlink")
    options = ["--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
               "--unified=3", "--patch", "--numstat", "-z"]
    if file.kind == "untracked":
        if mode is None:
            raise DiffError("untracked candidate file is missing")
        if stat.S_ISLNK(mode):
            raise DiffError("untracked symlink excerpt is unavailable")
        args = ["diff", "--no-index", *options, "--", "/dev/null", "./" + path]
    else:
        paths = [path] + ([file.renamed_from] if file.renamed_from else [])
        args = ["diff", *options, "--relative", baseline.commit or _EMPTY_TREE, "--",
                *[":(literal)" + p for p in paths]]
    temp_dir = Path(tempfile.gettempdir()).resolve()
    if temp_dir.is_relative_to(root):
        raise DiffError("host temporary directory is inside the workspace")
    with tempfile.TemporaryFile(dir=temp_dir) as output:
        result = _git(root, *args, output=output)
        size = output.tell()
        if (result.returncode not in ({0, 1} if file.kind == "untracked" else {0})
                or result.stderr or (result.returncode == 1 and not size)):
            raise DiffError("git diff failed: " + result.stderr.strip()[:300])
        output.seek(0)
        # numstat precedes the patch and uses '-' for binary content. no-index
        # has two path tokens even with rename detection disabled.
        binary = False
        while size:
            row = _read_z(output)
            if not row:
                break
            fields = row.split(b"\t", 2)
            if len(fields) != 3:
                raise DiffError("invalid diff numstat")
            binary |= fields[:2] == [b"-", b"-"]
            if not fields[2]:
                _read_z(output)
                _read_z(output)
        start = output.tell()
        patch_bytes = size - start
        if result.returncode == 1 and patch_bytes <= 0:
            raise DiffError("git diff reported a change without a patch")
        text = "" if binary else output.read(limit).decode("utf-8", errors="ignore")
    _, findings = redact_text(text)
    redacted = bool(findings)
    text, rendered = _bounded_text("" if redacted else text, limit)
    shown = len(text.encode("utf-8"))
    return {"status": "binary" if binary else "redacted" if redacted else "captured",
            "binary": binary, "redacted": redacted, "rendered_bytes_shown": rendered,
            "excerpt": text, "bytes_total": patch_bytes, "bytes_shown": shown,
            "bytes_omitted": patch_bytes - shown}


def diff_excerpts(baseline: Baseline, delta: SessionDelta, workspace: str | Path,
                  changed: list[str], *, changed_total: int | None,
                  changed_complete: bool | None, per_file_bytes: int = MAX_FILE_BYTES,
                  total_bytes: int = MAX_TOTAL_BYTES) -> dict:
    """Selected candidate files compared with the session's baseline commit.

    A dirty baseline has fingerprints, not saved source: its preexisting text
    cannot be subtracted. Such files are explicitly labelled. Unknown files or
    failed reads have unknown byte counts, never a fabricated empty patch.
    """
    if per_file_bytes < 0 or total_bytes < 0:
        raise ValueError("diff byte limits must be nonnegative")
    by_path = {f.path: f for f in delta.files if not f.preexisting}
    files, used, rendered = [], 0, 0
    for path in changed[:MAX_FILES]:
        row = {"path": path, "preexisting_at_start": path in baseline.fingerprints}
        try:
            file = by_path.get(path)
            if file is None:
                raise DiffError("recorded path is not in the candidate session delta")
            row.update(kind=file.kind, renamed_from=file.renamed_from)
            row.update(_excerpt(workspace, baseline, file,
                                min(per_file_bytes, total_bytes - rendered)))
        except (DiffError, OSError, ValueError) as exc:
            row.update(status="unknown", binary=None, excerpt=None, bytes_total=None,
                       bytes_shown=0, bytes_omitted=None, rendered_bytes_shown=0,
                       redacted=None, reason=str(exc)[:500])
        used += row["bytes_shown"]
        rendered += row["rendered_bytes_shown"]
        files.append(row)
    all_known = (changed_complete is True and len(files) == len(changed)
                 and all(f["bytes_total"] is not None for f in files))
    return {"status": "recorded", "comparison": "session baseline commit to candidate",
            "baseline_commit": baseline.commit,
            "note": "preexisting text is included for files dirty at session start; byte "
                    "counts cover generated patches, not binary payloads; "
                    "undecodable UTF-8 bytes are omitted; byte limits include JSON/HTML "
                    "escaping of excerpt contents; secret-pattern matches withhold the excerpt",
            "per_file_byte_limit": per_file_bytes, "total_byte_limit": total_bytes,
            "files": files, "files_omitted": None if changed_total is None else
            changed_total - len(files), "bytes_shown": used,
            "rendered_bytes_shown": rendered,
            "bytes_total": sum(f["bytes_total"] for f in files) if all_known else None,
            "bytes_omitted": sum(f["bytes_omitted"] for f in files) if all_known else None,
            "complete": (all_known and all(f["bytes_omitted"] == 0 for f in files))
            if changed_complete is not None else None}
