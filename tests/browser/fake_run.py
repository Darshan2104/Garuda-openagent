"""Write a session live, through the real EventStore.

    fake_run.py ROOT [SESSION_ID] [TURNS] [DELAY] [--stepped]

Not a file the test writes directly: the point is to exercise `attach_persistence`, which
opens/writes/closes per event, because that is what makes a poll able to land mid-write.

Timed (the default): one turn every DELAY seconds, for poking at a dashboard by hand.

Stepped (``--stepped``): the caller drives it, one line on stdin per step, and the script
prints ``step N written`` once the step is on disk. A check that waits for wall-clock luck
is flaky under load — on a busy machine the run raced ahead of the browser (or finished)
before the check had looked, and "the timeline did not grow" failed about one run in three.
Each step finishes the previous turn and *starts* the next one (budget, model response and
a dispatched tool call with no result yet), so between steps the last turn is genuinely in
progress, the state a live view must not report as stopped. The step after the last turn
finishes it and ends the session.
"""

import sys
import time
from pathlib import Path

from garuda.core.events import EventStore, EventType
from garuda.core.modes import GATE_FIELDS, MODE_PRESETS
from garuda.core.sessions import SessionStore
from garuda.types import AgentResult

args = [a for a in sys.argv[1:] if a != "--stepped"]
stepped = "--stepped" in sys.argv[1:]
root = Path(args[0])
session_id = args[1] if len(args) > 1 else "live-demo"
turns = int(args[2]) if len(args) > 2 else 6
delay = float(args[3]) if len(args) > 3 else 1.2
# Between the events of one step, so a poll can still land mid-step.
pause = 0.05 if stepped else delay / 3

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


def wait_for_step(number: int) -> None:
    """Stepped: block until the caller asks for the next step. Timed: pace by the clock."""
    if stepped:
        if not sys.stdin.readline():
            raise SystemExit("stdin closed before the run was finished")
    elif number > 1:
        time.sleep(pause)


def start_turn(turn: int) -> None:
    events.append(EventType.BUDGET, {"stage": "context", "turn": turn,
                                     "used_tokens": 9000 * turn, "capacity_tokens": 120000,
                                     "fraction": round(0.075 * turn, 4)})
    time.sleep(pause)
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
    if not last:
        time.sleep(pause)
        events.append(EventType.TOOL_CALL, {"turn": turn, "id": f"c{turn}", "name": "bash",
                                            "arguments": calls[0]["arguments"]})


def finish_turn(turn: int) -> None:
    if turn == turns:
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
        events.append(EventType.TOOL_RESULT, {"turn": turn, "tool_call_id": f"c{turn}",
                                              "content": f"{turn * 2} passed\n",
                                              "is_error": False, "duration_ms": 320.0})
    time.sleep(pause)
    events.append(EventType.TURN_METRICS, {"turn": turn, "model_ms": 1500.0,
                                           "tool_ms_total": 320.0, "tool_wall_ms": 320.0})


for number in range(1, turns + 2):
    wait_for_step(number)
    if number > 1:
        finish_turn(number - 1)
    if number <= turns:
        start_turn(number)
    print(f"step {number} written", flush=True)

events.append(EventType.SESSION_END, {"success": True, "turns": turns, "reason": "task_complete",
                                      "via": "gate"})
store.finish(session_id, AgentResult(
    success=True, final_message="done", messages=[], turns=turns,
    metadata={"usage": {"prompt_tokens": 9000 * turns, "completion_tokens": 220 * turns,
                        "total_tokens": 9220 * turns, "cost_usd": 0.0018 * turns * turns},
              "mode": "eval", "metrics": {"turns": turns, "model_ms_total": 1500.0 * turns}},
))
print("finished", flush=True)
