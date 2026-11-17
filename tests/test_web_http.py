"""HTTP header refusals must happen without consuming untrusted bodies."""

import io

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.web.http import _Handler
from garuda.interfaces.web.routes import DashboardContext


class HttpConnection:
    def __init__(self, headers, *, path="/api/chat", body=None, connection="close"):
        class HeaderInput(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError(f"refused request attempted body read({size})")

            def close(self):
                self.consumed = self.tell()
                super().close()

        wire = (f"POST {path} HTTP/1.1\r\nConnection: {connection}\r\n" + headers + "\r\n\r\n").encode()
        self.input = HeaderInput(wire) if body is None else io.BytesIO(wire + body)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, timeout):
        pass


@pytest.fixture
def handler(tmp_path):
    return type("Handler", (_Handler,), {"ctx": DashboardContext(
        port=8787, token="t", store=SessionStore(root=tmp_path), allow_run=True,
    )})


@pytest.mark.parametrize("length", ["-1", "abc", "", "1.5", "+5", "1_0", "5 "])
def test_bad_content_length_is_400_without_reading_body(handler, length):
    connection = HttpConnection(
        "Host: 127.0.0.1:8787\r\nOrigin: http://127.0.0.1:8787\r\n"
        f"X-Garuda-Token: t\r\nContent-Length: {length}", connection="keep-alive",
    )
    connection.input.seek(0, io.SEEK_END)
    unread_boundary = connection.input.tell()
    connection.input.write(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:8787\r\n\r\n")
    connection.input.seek(0)
    handler(connection, ("127.0.0.1", 1), None)
    assert bytes(connection.output).startswith(b"HTTP/1.1 400 ")
    assert connection.input.consumed == unread_boundary
    assert bytes(connection.output).count(b"HTTP/1.1 ") == 1


@pytest.mark.parametrize("headers,status", [
    ("Host: evil.test\r\n", 403),
    ("Host: 127.0.0.1:8787\r\nOrigin: http://evil.test\r\n", 403),
    ("Host: 127.0.0.1:8787\r\nOrigin: http://127.0.0.1:8787\r\n", 401),
])
def test_header_gate_refuses_before_body_read(handler, headers, status):
    connection = HttpConnection(headers + "Content-Length: 12582912", connection="keep-alive")
    handler(connection, ("127.0.0.1", 1), None)
    assert bytes(connection.output).startswith(f"HTTP/1.1 {status} ".encode())


def test_authorized_body_reaches_the_route_after_header_admission(handler):
    body = b'{"allow":true,"digest":"' + b"a" * 64 + b'"}'
    connection = HttpConnection(
        "Host: 127.0.0.1:8787\r\nOrigin: http://127.0.0.1:8787\r\n"
        f"X-Garuda-Token: t\r\nContent-Length: {len(body)}",
        path="/api/sessions/session1/approvals/ask1", body=body,
    )
    handler(connection, ("127.0.0.1", 1), None)
    # A well-formed answer with no pending ask is 404. Dropping its body is 400.
    assert bytes(connection.output).startswith(b"HTTP/1.1 404 ")


@pytest.mark.parametrize("framing", [
    "Content-Length: 5\r\nContent-Length: 10",
    "Content-Length: 5, 10",
    "Transfer-Encoding: chunked",
    "Content-Length: 5\r\nTransfer-Encoding: identity",
])
def test_ambiguous_framing_is_400_and_closes_before_next_request(handler, framing):
    connection = HttpConnection(
        "Host: 127.0.0.1:8787\r\nOrigin: http://127.0.0.1:8787\r\n"
        "X-Garuda-Token: t\r\n" + framing, connection="keep-alive",
    )
    connection.input.seek(0, io.SEEK_END)
    unread_boundary = connection.input.tell()
    connection.input.write(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:8787\r\n\r\n")
    connection.input.seek(0)
    handler(connection, ("127.0.0.1", 1), None)
    assert bytes(connection.output).startswith(b"HTTP/1.1 400 ")
    assert connection.input.consumed == unread_boundary
    assert bytes(connection.output).count(b"HTTP/1.1 ") == 1
