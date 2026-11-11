"""DAP pipe peer that delays replies and stops without requiring local sockets."""

import json
import sys
import time
from pathlib import Path


def send(message):
    payload = json.dumps(message).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
    sys.stdout.buffer.flush()


def wait_release():
    while not Path(sys.argv[1]).exists():
        time.sleep(0.01)


while header := sys.stdin.buffer.readline():
    length = int(header.decode().split(":")[1])
    assert sys.stdin.buffer.readline() == b"\r\n"
    request = json.loads(sys.stdin.buffer.read(length))
    if request["command"] == "stackTrace":
        wait_release()
    send({"type": "response", "request_seq": request["seq"], "success": True,
          "body": {"command": request["command"]}})
    if request["command"] == "next":
        wait_release()
        send({"type": "event", "event": "stopped", "body": {"threadId": 1}})
