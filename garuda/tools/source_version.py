"""Source-version edit preconditions (#368).

A read reports a version token for the full content it saw; an edit may name
it as ``expected_version`` and is refused if the file is no longer that
content. The token binds the session, the normalized path and the content, so
a token read in another session or for another file never matches.

Two contracts, selected per tool through ``tools.options`` and validated when
the agent definition resolves:

* ``legacy`` (default): exact string matching as before; a token is optional
  and checked only when given. No version guarantee is implied without one.
* ``versioned``: every edit must carry a token, so a still-matching string can
  never apply to content the model did not read.

The check happens on the content the edit itself reads and transforms. It is
a precondition, not a lock: a writer that changes the file between that read
and the write is not prevented by this check.
"""

from __future__ import annotations

import hashlib
import posixpath

CONTRACTS = ("legacy", "versioned")
EDIT_TOOLS = ("edit", "multi_edit")


def contract_option(value) -> str | None:
    if value not in CONTRACTS:
        return f"must be one of {', '.join(CONTRACTS)}"
    return None


def contract(ctx, tool: str) -> str:
    return ctx.option(tool, "contract", "legacy") if hasattr(ctx, "option") else "legacy"


def versioned_reads(ctx) -> bool:
    """Whether reads should show their token: some edit tool requires one."""
    return any(contract(ctx, tool) == "versioned" for tool in EDIT_TOOLS)


def _normalized(env, path: str) -> str:
    root = getattr(env, "workspace_root", None) or ""
    return posixpath.normpath(posixpath.join(str(root), path))


def content_digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def version_token(ctx, env, path: str, content: str) -> str:
    material = "\x00".join((getattr(ctx, "session_id", "") or "", _normalized(env, path),
                            content_digest(content)))
    return "v1:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def precondition_error(arguments: dict, ctx, env, path: str, content: str, tool: str) -> str | None:
    """Why this edit must not apply to ``content``, or None."""
    expected = arguments.get("expected_version")
    if expected is None:
        if contract(ctx, tool) == "versioned":
            return (f"{tool} requires expected_version under the versioned edit contract: "
                    f"read {path} with read_file and pass the version it reports.")
        return None
    if not isinstance(expected, str) or expected != version_token(ctx, env, path, content):
        return (f"Refused: {path} is not the version you read (it changed since, or the "
                "version belongs to another file or session). Read it again before editing.")
    return None
