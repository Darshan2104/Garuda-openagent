"""Session tags: attach other sessions' briefs to a run (plan task B.7, #157).

There is no automatic cross-session index. A session's brief reaches another
session only when the user tags it:

- ``--with NAME`` (repeatable) on ``run`` and ``chat`` names a session in the
  **current project**. An unknown name refuses (``session.tag_unknown``); a
  full id of another project's session refuses
  (``session.cross_project_context_denied``).
- ``@name`` in the task or a chat message is a tag only when the whole token
  (trailing punctuation aside) exactly matches a current-project session
  name. ``@pytest.fixture``, unknown names and full ids in text stay text, so
  nothing a message says can reach another project.
- ``--with-id FULL_ID`` may name another project's session, but only with the
  user's grant: ``--allow-cross-project-context`` (headless) or a yes to the
  interactive preview. Project configuration and model or tool output can
  never grant it.

Both sessions record the link. A cross-project grant also writes an
immutable receipt in the receiving session — ids, projects, field names,
fingerprint and provenance, never the brief's text.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from garuda.context.brief import Brief, Rendered, build_brief, render

_TOKEN = re.compile(r"^@([a-z0-9][a-z0-9-]{0,47})[,.;:!?)\]]*$")
_FULL_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TagError(Exception):
    """A tag was refused before any prompt; ``code`` says why."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Tag:
    session_id: str
    name: str | None
    project_id: str | None
    cross_project: bool
    provenance: str  # flag | mention | cross-project-flag | cross-project-interactive


def mentions(text: str) -> list[str]:
    """Names written as ``@name`` tokens in ``text``, in order, without repeats."""
    found: list[str] = []
    for token in (text or "").split():
        match = _TOKEN.match(token)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def _project(store, workspace):
    from garuda.core.project_identity import project_identity

    return project_identity(store.root, workspace)


def _meta(store, session_id):
    try:
        return store.load_meta(session_id)
    except Exception:
        return None


def _by_name(store, project_id, name):
    from garuda.core.project_identity import ProjectIdentityError, session_for_name, validate_name

    try:
        validate_name(name)
    except ProjectIdentityError:
        return None
    sid = session_for_name(store.root, project_id, name)
    return sid if sid and store.session_dir(sid).is_dir() else None


def resolve(
    store,
    workspace,
    *,
    with_refs=(),
    with_ids=(),
    text: str = "",
    allow_cross_project: bool = False,
    confirm: Callable[[Brief], bool] | None = None,
    exclude: str | None = None,
) -> list[Tag]:
    """Resolve every tag for a run, or refuse before anything is prompted."""
    if not (with_refs or with_ids or mentions(text)):
        return []  # nothing tagged: no project lookup at all
    ident = _project(store, workspace)
    tags: dict[str, Tag] = {}

    def add(tag: Tag) -> None:
        if tag.session_id != exclude and tag.session_id not in tags:
            tags[tag.session_id] = tag

    for ref in with_refs:
        sid = _by_name(store, ident.project_id, ref)
        if sid is None and _FULL_ID.match(ref or ""):
            meta = _meta(store, ref)
            if meta is not None:
                if meta.get("project_id") != ident.project_id:
                    raise TagError(
                        "session.cross_project_context_denied",
                        f"{ref} belongs to another project; use --with-id with "
                        "--allow-cross-project-context to share it",
                    )
                sid = ref
        if sid is None:
            raise TagError("session.tag_unknown", f"no session named {ref!r} in this project")
        add(Tag(sid, (_meta(store, sid) or {}).get("name"), ident.project_id, False, "flag"))

    for name in mentions(text):
        sid = _by_name(store, ident.project_id, name)
        if sid is not None:
            add(Tag(sid, name, ident.project_id, False, "mention"))

    for full_id in with_ids:
        meta = _meta(store, full_id) if _FULL_ID.match(full_id or "") else None
        if meta is None:
            raise TagError("session.tag_unknown", f"no session with id {full_id!r}")
        if meta.get("project_id") == ident.project_id:
            add(Tag(full_id, meta.get("name"), ident.project_id, False, "flag"))
            continue
        if allow_cross_project:
            provenance = "cross-project-flag"
        elif confirm is not None and confirm(build_brief(store, full_id)):
            provenance = "cross-project-interactive"
        else:
            raise TagError(
                "session.cross_project_context_denied",
                f"{full_id} belongs to another project; sharing it needs "
                "--allow-cross-project-context or an interactive confirmation",
            )
        add(Tag(full_id, meta.get("name"), meta.get("project_id"), True, provenance))
    return list(tags.values())


@dataclass
class Attached:
    tags: list[Tag]
    briefs: list[Brief]
    rendered: Rendered

    @property
    def preamble(self) -> str:
        return self.rendered.text

    def echo_lines(self) -> list[str]:
        lines = [f"tagged: {b.label} ({b.runtime} · {b.model})" for b in self.briefs]
        lines += [f"trimmed: {t['session']} {t['field']} (-{t['chars']})"
                  for t in self.rendered.trimmed]
        return lines


def brief_tags(store, tags: list[Tag], workspace) -> Attached:
    """Build and render the briefs for ``tags`` (no writes)."""
    briefs = [build_brief(store, t.session_id, workspace=workspace) for t in tags]
    return Attached(tags=tags, briefs=briefs, rendered=render(briefs))


def prompt_with(attached: Attached | None, task: str) -> str:
    """The text the model sees: the briefs envelope, then the task."""
    if attached is None or not attached.briefs:
        return task
    return f"{attached.preamble}\n\n{task}"


def record_links(store, session_id: str, attached: Attached) -> None:
    """Both sides record the link; a cross-project grant writes a receipt."""
    if not attached.briefs:
        return
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    trimmed = attached.rendered.trimmed
    links = []
    back_link = {"session_id": session_id, "at": now}

    def add_to(meta):
        return {"context_to": [*(meta.get("context_to") or []), back_link]}

    for tag, brief in zip(attached.tags, attached.briefs, strict=True):
        links.append({
            "session_id": tag.session_id, "name": tag.name, "provenance": tag.provenance,
            "cross_project": tag.cross_project, "fingerprint": brief.fingerprint(),
            "trimmed": [t for t in trimmed if t["session"] == brief.label],
        })
        store.mutate_meta(tag.session_id, add_to)
        if tag.cross_project:
            _write_receipt(store, session_id, tag, brief, now)

    def add_from(meta):
        return {"context_from": [*(meta.get("context_from") or []), *links]}

    store.mutate_meta(session_id, add_from)


def _write_receipt(store, session_id: str, tag: Tag, brief: Brief, now: str) -> None:
    directory = Path(store.session_dir(session_id)) / "receipts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        destination_project = store.load_meta(session_id).get("project_id")
    except Exception:
        destination_project = None
    receipt = {
        "version": 1,
        "kind": "cross_project_context",
        "source_session_id": tag.session_id,
        "source_project_id": tag.project_id,
        "destination_session_id": session_id,
        "destination_project_id": destination_project,
        "fields": brief.fields(),
        "fingerprint": brief.fingerprint(),
        "provenance": tag.provenance,
        "created_at": now,
    }
    path = directory / f"{tag.session_id}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o400)  # immutable: created once, never rewritten
    try:
        os.write(fd, json.dumps(receipt, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def attach_for_cli(workspace, task: str, *, with_refs=(), with_ids=(), allow_cross_project=False,
                   confirm=None, out=None, exclude=None, store=None) -> Attached | None:
    """Resolve, brief and echo a command's tags; ``None`` when nothing is tagged."""
    from garuda.core.sessions import SessionStore

    if not (with_refs or with_ids or mentions(task)):
        return None
    store = store or SessionStore()
    resolved = resolve(store, workspace, with_refs=with_refs, with_ids=with_ids, text=task,
                       allow_cross_project=allow_cross_project, confirm=confirm,
                       exclude=exclude)
    if not resolved:
        return None
    attached = brief_tags(store, resolved, workspace)
    for line in attached.echo_lines():
        print(f"[garuda] {line}", file=out)
    return attached


def with_resume_brief(store, plan, workspace, attached: Attached | None) -> Attached:
    """Add a brief-resumed session's own brief to a run's tags (B.7)."""
    meta = store.load_meta(plan.source)
    tag = Tag(plan.source, meta.get("name"), meta.get("project_id"), False, "resume")
    existing = [t for t in (attached.tags if attached else []) if t.session_id != plan.source]
    return brief_tags(store, [tag, *existing], workspace)
