import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from garuda.types import Message

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PERMISSION_ASK = "permission_ask"
    VERIFICATION = "verification"
    SUMMARIZATION = "summarization"
    SESSION_START = "session_start"
    SESSION_END = "session_end"


class EventStore:
    def __init__(
        self,
        session_id: str | None = None,
        persist_path: str | Path | None = None,
        on_append: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.session_id = session_id or str(uuid.uuid4())
        self._events: list[dict[str, Any]] = []
        self._persist_path: Path | None = None
        # Optional subscriber invoked after each append. Lets a live tracer (or
        # any observer) react to events without the agent loop knowing. It is
        # always wrapped in try/except and can never break appends.
        self._on_append = on_append
        if persist_path:
            self.attach_persistence(persist_path)

    def attach_persistence(self, path: str | Path) -> None:
        """Append every future event to a JSONL file so crashes keep the trail."""
        self._persist_path = Path(path)
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: EventType, payload: dict[str, Any]) -> None:
        event = {
            "type": event_type.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "payload": payload,
        }
        self._events.append(event)
        if self._persist_path:
            try:
                with self._persist_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, default=str) + "\n")
            except OSError:
                # Don't let a full/again-unwritable disk break the run, but surface
                # it once so a silently-stopped trajectory isn't a mystery.
                logger.warning("Failed to persist event to %s", self._persist_path, exc_info=True)
        if self._on_append is not None:
            try:
                self._on_append(event)
            except Exception:
                # Observers must never break the event trail.
                pass

    def get_all(self) -> list[dict[str, Any]]:
        return list(self._events)

    def get_since(self, cursor: int) -> list[dict[str, Any]]:
        """Events appended after ``cursor`` (an index into the event list).

        Enables cursor-based incremental polling: a client passes back the cursor
        returned last time to receive only newly-appended events.
        """
        if cursor < 0:
            cursor = 0
        return list(self._events[cursor:])

    def count(self) -> int:
        return len(self._events)

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # default=str mirrors append(): payloads may carry datetime/Path/exception
        # objects that are not natively JSON-serializable. Without it, save() would
        # crash on exactly the events the incremental append path persisted fine.
        lines = [json.dumps(event, default=str) for event in self._events]
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "EventStore":
        store = cls()
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                store._events.append(json.loads(line))
            except json.JSONDecodeError:
                # A crash mid-write can leave a torn final line; skip it rather than
                # making the whole crash-safe trajectory unreadable.
                logger.warning("Skipping malformed event on line %d of %s", lineno, path)
        if store._events:
            store.session_id = store._events[0].get("session_id", store.session_id)
        return store

    def messages_snapshot(self, messages: list[Message]) -> list[dict[str, str]]:
        return [{"role": m.role.value, "content": m.content} for m in messages]
