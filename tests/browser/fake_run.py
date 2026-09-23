"""Write a session live, at human speed, through the real EventStore.

Not a file the test writes directly: the point is to exercise `attach_persistence`, which
opens/writes/closes per event, because that is what makes a poll able to land mid-write.
"""

import sys
import time
from pathlib import Path

from garuda.core.events import EventStore, EventType
from garuda.core.modes import GATE_FIELDS, MODE_PRESETS
from garuda.core.sessions import SessionStore
from garuda.types import AgentResult

root = Path(sys.argv[1])
session_id = sys.argv[2] if len(sys.argv) > 2 else "live-demo"
turns = int(sys.argv[3]) if len(sys.argv) > 3 else 6
delay = float(sys.argv[4]) if len(sys.argv) > 4 else 1.2

store = SessionStore(root=root)
store.begin(session_id=session_id, task="Watch me work: build a parser and verify it",
            model="openrouter/deepseek/deepseek-v4-flash-0731", agent="build",
            workspace="/Users/dev/project")
events = EventStore(session_id=session_id)
events.attach_persistence(store.events_path(session_id))

config = {key: MODE_PRESETS["eval"].get(key) for key in GATE_FIELDS}
config.update({"condenser": "microcompact", "max_turns": 30, "max_context_tokens": 120000,
               "permission_mode": "auto", "enable_verifier": True, "deadline_sec": 900})
events.append(EventType.SESSION_START, {
    "task": "Watch me work: build a parser and verify it",
    "model": "openrouter/deepseek/deepseek-v4-flash-0731",
    "agent": "build", "mode": "eval", "permission_mode": "auto", "config": config,
})
print(f"started {session_id}", flush=True)

for turn in range(1, turns + 1):
    events.append(EventType.BUDGET, {"stage": "context", "turn": turn,
                                     "used_tokens": 9000 * turn, "capacity_tokens": 120000,
                                     "fraction": round(0.075 * turn, 4)})
    time.sleep(delay / 3)
    last = turn == turns
    calls = [] if last else [{"id": f"c{turn}", "name": "bash",
                              "arguments": {"command": f"pytest tests/step_{turn}.py -q"}}]
    if last:
        calls = [{"id": f"c{turn}", "name": "task_complete", "arguments": {
            "summary": "Wrote parser.py with a recursive-descent expression parser and "
                       "verified it against the 14 cases in tests/test_parser.py.",
            "verification_commands": ["pytest tests/test_parser.py -q"]}}]
    events.append(EventType.MODEL_RESPONSE, {
        "turn": turn, "content": f"Step {turn}: working on it.", "tool_calls": calls,
        "usage": {"prompt_tokens": 9000 * turn, "completion_tokens": 220,
                  "cost_usd": 0.0018 * turn},
        "duration_ms": 1500.0,
    })
    time.sleep(delay / 3)
    if last:
        events.append(EventType.VERIFICATION, {
            "turn": turn, "approved": True, "attempt": 1,
            "summary": "Wrote parser.py and verified it.",
            "checklist": {"summary_present": True, "summary_length": True,
                          "evidence_discriminating": True, "verify_cmd_0": True,
                          "verification_stable": True},
            "evidence": [{"command": "pytest tests/test_parser.py -q",
                          "class": "discriminating", "exit_code": 0,
                          "stdout": "14 passed\n", "stderr": ""}],
        })
    else:
        events.append(EventType.TOOL_CALL, {"turn": turn, "id": f"c{turn}", "name": "bash",
                                            "arguments": calls[0]["arguments"]})
        events.append(EventType.TOOL_RESULT, {"turn": turn, "tool_call_id": f"c{turn}",
                                              "content": f"{turn * 2} passed\n",
                                              "is_error": False, "duration_ms": 320.0})
    events.append(EventType.TURN_METRICS, {"turn": turn, "model_ms": 1500.0,
                                           "tool_ms_total": 320.0, "tool_wall_ms": 320.0})
    print(f"  turn {turn} written", flush=True)
    time.sleep(delay / 3)

events.append(EventType.SESSION_END, {"success": True, "turns": turns, "reason": "task_complete",
                                      "via": "gate"})
store.finish(session_id, AgentResult(
    success=True, final_message="done", messages=[], turns=turns,
    metadata={"usage": {"prompt_tokens": 9000 * turns, "completion_tokens": 220 * turns,
                        "total_tokens": 9220 * turns, "cost_usd": 0.0018 * turns * turns},
              "mode": "eval", "metrics": {"turns": turns, "model_ms_total": 1500.0 * turns}},
))
print("finished", flush=True)
