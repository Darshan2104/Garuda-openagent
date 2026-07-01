import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from garuda.types import Message


class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SESSION_START = "session_start"
    SESSION_END = "session_end"


class EventStore:
    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or str(uuid.uuid4())
        self._events: list[dict[str, Any]] = []

    def append(self, event_type: EventType, payload: dict[str, Any]) -> None:
        self._events.append(
            {
                "type": event_type.value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": self.session_id,
                "payload": payload,
            }
        )

    def get_all(self) -> list[dict[str, Any]]:
        return list(self._events)

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(event) for event in self._events]
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "EventStore":
        store = cls()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                store._events.append(json.loads(line))
        if store._events:
            store.session_id = store._events[0].get("session_id", store.session_id)
        return store

    def messages_snapshot(self, messages: list[Message]) -> list[dict[str, str]]:
        return [{"role": m.role.value, "content": m.content} for m in messages]
