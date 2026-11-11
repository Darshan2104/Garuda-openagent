"""Replay the recorded no-prompt `omp acp` handshake (omp 18.16.0) over stdio.

Answers `initialize` and `session/new` with the exact results captured from
omp, emits the notifications omp sent after `session/new`, accepts the
`session/cancel` notification silently (as omp did on an idle session), and
exits when stdin closes. Every method received is appended to the log file so
a test can prove which requests Garuda sent. Anything else — including
`session/prompt` — is answered with a JSON-RPC error: the capture never
exercised it.

Usage: replay_agent.py FIXTURE LOG
"""

import json
import sys


def main() -> int:
    fixture_path, log_path = sys.argv[1], sys.argv[2]
    with open(fixture_path, encoding="utf-8") as handle:
        fixture = json.load(handle)
    session_id = fixture["session_new_result"]["sessionId"]

    def send(message: dict) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method = message.get("method")
        if method is None:
            continue  # a response to an agent request; the replay sends none
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(method + "\n")
        call_id = message.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": call_id, "result": fixture["initialize_result"]})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": call_id, "result": fixture["session_new_result"]})
            for update in fixture["session_new_notifications"]:
                send({"jsonrpc": "2.0", "method": "session/update",
                      "params": {"sessionId": session_id, "update": update}})
        elif call_id is None:
            continue  # notifications such as session/cancel get no reply
        else:
            send({"jsonrpc": "2.0", "id": call_id,
                  "error": {"code": -32601, "message": f"not captured: {method}"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
