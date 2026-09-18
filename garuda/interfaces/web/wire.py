"""HTTP wire types for the dashboard.

Its own module, importing nothing, so ``security.py``, ``routes.py`` and ``http.py``
can all speak the same shapes without importing each other. The point of these being
plain dataclasses is that :func:`routes.dispatch` never touches a socket — every
route test is a function call, the way ``JsonRpcServer.handle()`` is tested with a
plain dict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: Error codes the frontend switches on. Strings, not HTTP statuses, because the UI
#: reacts differently to `expired` (fall back to the on-disk run) than to `not_found`
#: even though both are 4xx.
CODE_UNAUTHORIZED = "unauthorized"
CODE_FORBIDDEN_HOST = "forbidden_host"
CODE_FORBIDDEN_ORIGIN = "forbidden_origin"
CODE_INVALID_REQUEST = "invalid_request"
CODE_NOT_FOUND = "not_found"
CODE_METHOD_NOT_ALLOWED = "method_not_allowed"
CODE_PAYLOAD_TOO_LARGE = "payload_too_large"
CODE_EXPIRED = "expired"
CODE_READ_ONLY = "read_only"
CODE_UNAVAILABLE = "unavailable"

JSON_CONTENT_TYPE = "application/json; charset=utf-8"


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    #: Parsed query string. Values are lists because that is what ``parse_qs``
    #: returns and collapsing them here would hide a duplicated parameter.
    query: dict[str, list[str]] = field(default_factory=dict)
    #: Header names lowercased, so no call site has to remember the casing a
    #: particular client sent.
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def first(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name) or []
        return values[0] if values else default

    def int_param(self, name: str, default: int) -> int | None:
        """A positive integer query parameter, or ``None`` when it is malformed.

        ``None`` rather than the default, so a caller can answer a typo with a 400
        instead of silently using a limit the user did not ask for.
        """
        raw = self.first(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    def json_body(self) -> Any:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = JSON_CONTENT_TYPE
    headers: dict[str, str] = field(default_factory=dict)


def ok(payload: Any, *, status: int = 200) -> Response:
    # `default=str` for the same reason EventStore.append uses it: a Path or a
    # datetime reaching here should degrade to its string, not 500 the request.
    return Response(status=status, body=json.dumps(payload, default=str).encode("utf-8"))


def error(code: str, message: str, *, status: int) -> Response:
    """The one error envelope. Every non-2xx JSON response has this shape."""
    return Response(
        status=status,
        body=json.dumps({"error": {"code": code, "message": message}}).encode("utf-8"),
    )


def not_found(message: str = "No such resource.") -> Response:
    return error(CODE_NOT_FOUND, message, status=404)


def invalid(message: str) -> Response:
    return error(CODE_INVALID_REQUEST, message, status=400)
