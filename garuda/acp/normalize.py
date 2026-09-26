"""ACP event normalizer (P0.14, issue #23).

Transforms ACP v1 `session/update` payloads — keyed by `sessionUpdate`:
message/thought chunks carrying content blocks, tool calls and their updates
(whose `content` may hold diffs), plans and mode/command updates — into the
`RuntimeEvent` vocabulary, so one reader renders native and external trails.
Permission requests are JSON-RPC *requests* in v1, not updates; the adapter
turns them into `APPROVAL_REQUEST` events. Raw protocol records stay in
session-local diagnostics, redacted on read.

Ordering rules, stated once because every consumer depends on them:

- `seq` is assigned in emission order and never repeats within the session.
- A `tool_call` is always emitted before its `tool_call_update`s. Updates for
  a call not yet seen are buffered and flushed on arrival (causal order wins
  over arrival order).
- Message chunks emit in arrival order; each chunk is preserved exactly once.
- The terminal `LIFECYCLE` event is always last. `finish` emits no `MESSAGE`
  with the full text, so a transcript can never show the answer twice.
- `completed` closes the turn: feeding resumes only after `new_turn`.
  `cancelled`/`failed` close the session: anything after is rejected, not
  stored — same rule as every other runtime in this package.
"""

from __future__ import annotations

import json
from typing import Any

from garuda.acp.protocol import AcpProtocolError
from garuda.context.redact import redact_text
from garuda.runtime.events import RuntimeEvent, RuntimeEventKind

#: ACP v1 `StopReason` values, plus the two Garuda-internal closes the adapter
#: uses for transport failure (`failed`) and a caller-closed turn (`completed`).
#: A stop that ends only the turn keeps the session open for the next prompt.
_TERMINAL_REASONS = {
    "end_turn": "completed",
    "max_tokens": "completed",
    "max_turn_requests": "completed",
    "refusal": "completed",
    "cancelled": "cancelled",
    "completed": "completed",
    "failed": "failed",
}


class AcpNormalizer:
    """Stateful per-session normalizer. One instance, one session."""

    def __init__(self, session_id: str):
        if not session_id:
            raise AcpProtocolError("normalizer requires a session_id")
        self._session_id = session_id
        self._turn = 0
        self._seq = 0
        self._terminated = False
        self._turn_open = False
        self._pending_updates: dict[str, list[dict[str, Any]]] = {}
        self._seen_calls: set[str] = set()
        self._diagnostics: list[dict[str, Any]] = []

    @property
    def turn(self) -> int:
        return self._turn

    def new_turn(self) -> int:
        if self._terminated:
            raise AcpProtocolError("new turn on a terminated session")
        self._turn += 1
        self._turn_open = True
        return self._turn

    def _ensure_open(self) -> None:
        if self._terminated:
            raise AcpProtocolError("update arrived after the terminal event")
        if not self._turn_open:
            raise AcpProtocolError("update arrived with no open turn")

    def _emit(
        self, kind: RuntimeEventKind, payload: dict[str, Any]
    ) -> RuntimeEvent:
        event = RuntimeEvent(
            kind=kind,
            session_id=self._session_id,
            turn=self._turn,
            seq=self._seq,
            payload=payload,
        )
        self._seq += 1
        return event

    def feed(self, update: dict[str, Any]) -> list[RuntimeEvent]:
        """Normalize one v1 `session/update` payload. Unknown kinds are kept
        as informational events, never dropped; a missing discriminator is a
        protocol error."""
        self._ensure_open()
        if not isinstance(update, dict):
            raise AcpProtocolError(f"update must be an object, got {update!r}")
        kind = update.get("sessionUpdate")
        if not isinstance(kind, str) or not kind:
            raise AcpProtocolError("session/update requires a sessionUpdate kind")
        self._diagnostics.append(dict(update))
        handler = {
            "agent_message_chunk": self._on_message_chunk,
            "agent_thought_chunk": self._on_thought_chunk,
            "tool_call": self._on_tool_call,
            "tool_call_update": self._on_tool_call_update,
        }.get(kind, self._on_unknown)
        return handler(update)

    def finish(self, stop_reason: str, *, detail: str = "") -> list[RuntimeEvent]:
        """Close the turn with a terminal event. No duplicate final message.

        `completed` ends the turn (a later `new_turn` reopens feeding);
        `cancelled`/`failed` end the session — anything after is rejected.
        """
        self._ensure_open()
        if self._pending_updates:
            pending = sorted(self._pending_updates)
            raise AcpProtocolError(
                "turn ended with updates for unknown tool calls: "
                f"{pending}"
            )
        try:
            state = _TERMINAL_REASONS[stop_reason]
        except KeyError:
            raise AcpProtocolError(f"unknown stop reason {stop_reason!r}") from None
        payload: dict[str, Any] = {"state": state, "stop_reason": stop_reason}
        if detail:
            payload["detail"] = detail
        if state == "completed":
            self._turn_open = False
            return [self._emit(RuntimeEventKind.LIFECYCLE, payload)]
        self._terminated = True
        if state == "failed":
            return [
                self._emit(RuntimeEventKind.ERROR, {"message": detail or stop_reason}),
                self._emit(RuntimeEventKind.LIFECYCLE, payload),
            ]
        return [self._emit(RuntimeEventKind.LIFECYCLE, payload)]

    def _on_message_chunk(self, update: dict[str, Any]) -> list[RuntimeEvent]:
        text, block_type = _block_text(update.get("content"), where="agent_message_chunk")
        payload: dict[str, Any] = {"chunk": text}
        if block_type != "text":
            payload["content_type"] = block_type
        return [self._emit(RuntimeEventKind.MESSAGE, payload)]

    def _on_thought_chunk(self, update: dict[str, Any]) -> list[RuntimeEvent]:
        text, _ = _block_text(update.get("content"), where="agent_thought_chunk")
        return [self._emit(RuntimeEventKind.MESSAGE, {"thought": text})]

    def _on_tool_call(self, update: dict[str, Any]) -> list[RuntimeEvent]:
        call_id = update.get("toolCallId", "")
        if not call_id or not isinstance(call_id, str):
            raise AcpProtocolError("tool_call.toolCallId is required")
        if call_id in self._seen_calls:
            raise AcpProtocolError(f"duplicate tool_call id {call_id!r}")
        self._seen_calls.add(call_id)
        events = [
            self._emit(
                RuntimeEventKind.TOOL_CALL,
                {
                    "tool_call_id": call_id,
                    "title": update.get("title", ""),
                    "kind": update.get("kind", ""),
                    "status": update.get("status", "pending"),
                },
            )
        ]
        events.extend(self._content_results(call_id, update.get("content")))
        for buffered in self._pending_updates.pop(call_id, []):
            events.extend(self._on_tool_call_update(buffered, known_id=call_id))
        return events

    def _on_tool_call_update(
        self, update: dict[str, Any], *, known_id: str = ""
    ) -> list[RuntimeEvent]:
        call_id = known_id or update.get("toolCallId", "")
        if not call_id or not isinstance(call_id, str):
            raise AcpProtocolError("tool_call_update.toolCallId is required")
        if not known_id and call_id not in self._seen_calls:
            self._pending_updates.setdefault(call_id, []).append(update)
            return []
        content = update.get("content")
        events = [
            self._emit(
                RuntimeEventKind.TOOL_RESULT,
                {
                    "tool_call_id": call_id,
                    "status": update.get("status", ""),
                    "content": _content_text(content),
                },
            )
        ]
        events.extend(self._content_results(call_id, content))
        return events

    def _content_results(self, call_id: str, content: object) -> list[RuntimeEvent]:
        """Diffs inside tool-call content become their own results."""
        if content is None:
            return []
        if not isinstance(content, list):
            raise AcpProtocolError("tool call content must be a list")
        events = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "diff":
                continue
            path = item.get("path", "")
            if not path or not isinstance(path, str):
                raise AcpProtocolError("diff content requires a path")
            events.append(
                self._emit(
                    RuntimeEventKind.TOOL_RESULT,
                    {
                        "tool_call_id": call_id,
                        "status": "diff",
                        "path": path,
                        "old_text": item.get("oldText") or "",
                        "new_text": item.get("newText") or "",
                    },
                )
            )
        return events

    def _on_unknown(self, update: dict[str, Any]) -> list[RuntimeEvent]:
        # `plan`, `available_commands_update`, `current_mode_update`,
        # `user_message_chunk` (history replay) and future kinds.
        return [
            self._emit(
                RuntimeEventKind.MESSAGE,
                {"text": "", "acp_update": update["sessionUpdate"]},
            )
        ]

    def diagnostic_trail(self) -> list[dict[str, Any]]:
        """Raw updates with secrets scrubbed. Session-local; never a transcript."""
        trail = []
        for record in self._diagnostics:
            cleaned, _ = redact_text(_freeze(record))
            trail.append({"raw": cleaned})
        return trail


def _block_text(block: object, *, where: str) -> tuple[str, str]:
    """Text of one v1 content block, plus its type. Non-text blocks carry no
    text here; their type is kept so a reader knows something was elided."""
    if not isinstance(block, dict):
        raise AcpProtocolError(f"{where}.content must be a content block")
    block_type = block.get("type")
    if not isinstance(block_type, str) or not block_type:
        raise AcpProtocolError(f"{where}.content requires a type")
    if block_type == "text":
        text = block.get("text", "")
        if not isinstance(text, str):
            raise AcpProtocolError(f"{where}.content.text must be a string")
        return text, block_type
    return "", block_type


def _content_text(content: object) -> str:
    """Joined text of `{"type": "content", "content": <block>}` items."""
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "content":
            block = item.get("content")
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def _freeze(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)
