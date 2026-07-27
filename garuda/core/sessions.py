"""Persistent session store: conversation state on disk, resumable across runs.

Layout (default root ``~/.agent/sessions``, ``~/.garuda/sessions`` back-compat;
override with ``GARUDA_SESSIONS_DIR``):

    <root>/<session_id>/
        meta.json        # task, model, agent, workspace, status, timestamps
        messages.json    # full Message list (including tool_calls) for resume
        events.jsonl     # incremental event log (crash-safe, appended live)
"""

import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from garuda.types import AgentResult, Message, Role, ToolCall

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# A session ref must be a single path component: no separators, no traversal, no
# absolute paths. Refs reach this module from untrusted callers (the `resume`
# JSON-RPC param is client-controlled), and every on-disk path is derived by
# joining the ref onto the store root — so an unvalidated `../../x` would read
# arbitrary `messages.json` files straight into the model context.
_SESSION_REF_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_session_ref(session_ref: str) -> str:
    """Return ``session_ref`` if it is a safe single path component, else raise.

    Rejects separators, ``..``/``.``, null bytes, and anything outside
    ``[A-Za-z0-9._-]`` so a ref can never escape the sessions root.
    """
    if not isinstance(session_ref, str) or not session_ref:
        raise ValueError("Session id must be a non-empty string.")
    if session_ref in (".", "..") or not _SESSION_REF_RE.match(session_ref):
        raise ValueError(
            f"Invalid session id {session_ref!r}: expected a bare id "
            "(letters, digits, '.', '-', '_' only — no path separators)."
        )
    return session_ref


def default_sessions_root() -> Path:
    override = os.environ.get("GARUDA_SESSIONS_DIR")
    if override:
        return Path(override).expanduser()
    from garuda.config.agent_home import global_home_dir

    return global_home_dir() / "sessions"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so a crash mid-write can't corrupt the target.

    The temp name carries the pid: a shared ``.tmp`` name means two concurrent
    writers scribble over each other's staging file and one publishes the other's
    half-written bytes, which is exactly the corruption os.replace is here to
    prevent.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _meta_lock(meta_path: Path) -> Iterator[None]:
    """Serialize read-modify-write cycles on ``meta_path`` across processes.

    The lock lives on a sidecar ``meta.json.lock`` rather than on meta.json
    itself, because publishing goes through ``os.replace`` — locking the target
    would leave each writer holding a lock on an inode the next replace detaches,
    which serializes nothing.

    Best-effort by design: on a platform without ``fcntl``, or a filesystem where
    locking fails, the cycle proceeds unlocked. A lost meta update degrades the
    session index; refusing to record the run at all would be worse.
    """
    if fcntl is None:
        yield
        return
    lock_path = meta_path.with_name(meta_path.name + ".lock")
    handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        if handle is not None:
            handle.close()
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def merge_meta(meta_path: Path, updates: dict) -> None:
    """Merge ``updates`` into the meta document under an exclusive lock.

    The single chokepoint for every meta read-modify-write. Two writers used to
    read, mutate and publish independently, so whichever replaced last silently
    dropped the other's fields — a finished run could lose its usage totals to a
    concurrent provenance patch.
    """
    with _meta_lock(meta_path):
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # An already-corrupt meta.json shouldn't take the run down with
                # it; rebuild from the updates rather than propagating.
                logger.warning("Overwriting unparseable meta at %s", meta_path)
                meta = {}
        meta.update(updates)
        meta.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        _atomic_write_text(meta_path, json.dumps(meta, indent=2, default=str))


def message_to_dict(message: Message) -> dict:
    payload: dict = {"role": message.role.value, "content": message.content}
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls
        ]
    if message.metadata:
        payload["metadata"] = message.metadata
    return payload


def message_from_dict(payload: dict) -> Message:
    tool_calls = None
    if payload.get("tool_calls"):
        tool_calls = [
            ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments", {}))
            for c in payload["tool_calls"]
        ]
    return Message(
        role=Role(payload["role"]),
        content=payload.get("content", ""),
        name=payload.get("name"),
        tool_call_id=payload.get("tool_call_id"),
        tool_calls=tool_calls,
        metadata=payload.get("metadata", {}),
    )


@dataclass
class SessionMeta:
    session_id: str
    task: str
    model: str
    agent: str
    workspace: str
    status: str  # running | success | failed
    created_at: str
    updated_at: str
    turns: int = 0

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "task": self.task,
            "model": self.model,
            "agent": self.agent,
            "workspace": self.workspace,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": self.turns,
        }


class SessionStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_sessions_root()

    def session_dir(self, session_id: str) -> Path:
        # Validated here because this is the single chokepoint every on-disk path
        # (meta, messages, events) is derived from.
        return self.root / validate_session_ref(session_id)

    def events_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "events.jsonl"

    def begin(
        self,
        session_id: str,
        task: str,
        model: str,
        agent: str,
        workspace: str,
    ) -> Path:
        """Create the session directory and initial meta; returns the events path
        for EventStore.attach_persistence()."""
        directory = self.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        meta = SessionMeta(
            session_id=session_id,
            task=task,
            model=model,
            agent=agent,
            workspace=workspace,
            status="running",
            created_at=now,
            updated_at=now,
        )
        # Locked + atomic like every other meta write: a plain write_text here let a
        # concurrent list_sessions read a half-created document.
        merge_meta(directory / "meta.json", meta.to_dict())
        return self.events_path(session_id)

    def checkpoint_messages(self, session_id: str, messages: list[Message]) -> None:
        """Atomically persist the current message list mid-run.

        Called each turn so a crashed/killed session is still resumable from its
        last completed turn (previously messages.json was written only at finish(),
        so any interrupted run was unresumable despite the crash-safe event log).
        """
        directory = self.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            directory / "messages.json",
            json.dumps([message_to_dict(m) for m in messages], indent=2, default=str),
        )

    def finish(self, session_id: str, result: AgentResult) -> None:
        """Persist the final message state and update meta."""
        directory = self.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            directory / "messages.json",
            json.dumps([message_to_dict(m) for m in result.messages], indent=2, default=str),
        )
        self.update_meta(
            session_id,
            {
                "session_id": session_id,
                "status": "success" if result.success else "failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "turns": result.turns,
                "final_message": result.final_message[:2000],
                "usage": result.metadata.get("usage", {}),
            },
        )

    def update_meta(self, session_id: str, updates: dict) -> None:
        """Merge fields into this session's meta under an exclusive lock."""
        merge_meta(self.session_dir(session_id) / "meta.json", updates)

    def load_messages(self, session_id: str) -> list[Message]:
        path = self.session_dir(session_id) / "messages.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Session {session_id} has no saved messages at {path}."
            )
        return [message_from_dict(p) for p in json.loads(path.read_text(encoding="utf-8"))]

    def load_meta(self, session_id: str) -> dict:
        path = self.session_dir(session_id) / "meta.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def list_sessions(self, limit: int = 20) -> list[dict]:
        if not self.root.exists():
            return []
        metas: list[dict] = []
        for directory in self.root.iterdir():
            meta_path = directory / "meta.json"
            if meta_path.is_file():
                try:
                    metas.append(json.loads(meta_path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        metas.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
        return metas[:limit]

    def resolve(self, session_ref: str) -> str:
        """Resolve 'latest' or a unique session-id prefix to a full session id.

        Raises ``ValueError`` for a ref that is not a bare id — refs can arrive
        from remote clients via the server's ``resume`` param.
        """
        if session_ref == "latest":
            sessions = self.list_sessions(limit=1)
            if not sessions:
                raise FileNotFoundError("No saved sessions to resume.")
            return sessions[0]["session_id"]
        validate_session_ref(session_ref)
        if self.session_dir(session_ref).is_dir():
            return session_ref
        if self.root.exists():
            matches = [d.name for d in self.root.iterdir() if d.name.startswith(session_ref)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError(f"Ambiguous session prefix {session_ref!r}: {matches}")
        raise FileNotFoundError(f"No session found for {session_ref!r}")
