"""Seed sessions that hit every branch of the trajectory inspector.

Not "a few runs" — one session per rendering path that could be silently wrong:
an approved gate, a rejected gate with evidence and feedback, a diff for each of the
three file-writing tools, a pending tool step, a turn that never finished, a prune and
an overflow compaction, a rigorous phase run with a critic, a permission denial, a
truncated response, a final_submission band, and a log old enough to have no config.
"""

import json
import shutil
import sys
from pathlib import Path

from garuda.core.modes import GATE_FIELDS, MODE_PRESETS
from garuda.core.sessions import SessionStore
from garuda.types import AgentResult

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/inspector-sessions")
if ROOT.exists():
    shutil.rmtree(ROOT)
store = SessionStore(root=ROOT)

CLOCK = [0]


def ts() -> str:
    CLOCK[0] += 3
    minute, second = divmod(CLOCK[0], 60)
    return f"2026-08-06T14:{minute:02d}:{second:02d}+00:00"


def event(kind: str, **payload) -> dict:
    return {"type": kind, "timestamp": ts(), "session_id": "seed", "payload": payload}


def write(session_id: str, task: str, records: list[dict], *, success=True, turns=3,
          model="openrouter/deepseek/deepseek-v4-flash-0731", agent="build", mode="eval",
          finish=True, buffers: dict[str, str] | None = None):
    store.begin(session_id=session_id, task=task, model=model, agent=agent,
                workspace="/Users/dev/project")
    if finish:
        store.finish(
            session_id,
            AgentResult(
                success=success,
                final_message="done" if success else "gave up",
                messages=[],
                turns=turns,
                metadata={
                    "usage": {"prompt_tokens": 18400, "completion_tokens": 2100,
                              "total_tokens": 20500, "cost_usd": 0.0231},
                    "mode": mode,
                    "metrics": {"turns": turns, "model_ms_total": 8400.0},
                },
            ),
        )
    path = store.events_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    for name, content in (buffers or {}).items():
        directory = store.session_dir(session_id) / "buffers"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.txt").write_text(content, encoding="utf-8")
    print(f"  {session_id}  {len(records):3d} events  {task[:52]}")


def start(mode="eval", condenser="microcompact", **overrides):
    config = {**MODE_PRESETS[mode], "condenser": condenser, "max_turns": 30,
              "max_context_tokens": 120000, "deadline_sec": 900, "permission_mode": "auto",
              "enable_verifier": True}
    config = {key: config.get(key) for key in (*GATE_FIELDS, "enable_verifier",
                                               "permission_mode", "max_turns", "deadline_sec",
                                               "condenser", "max_context_tokens")}
    config.update(overrides)
    return event("session_start", task="seeded", model="openrouter/deepseek/deepseek-v4-flash-0731",
                 agent="build", mode=mode, permission_mode="auto", config=config)


def budget(turn, fraction, capacity=120000):
    return event("budget", stage="context", turn=turn, used_tokens=int(capacity * fraction),
                 capacity_tokens=capacity, max_context_tokens=capacity, fraction=fraction,
                 tool_schema_tokens=3100, reserved_output_tokens=8000, provider_anchored=True)


def response(turn, content, calls=(), *, prompt=14000, completion=400, reasoning=None,
             cost=0.0021):
    return event(
        "model_response", turn=turn, content=content, reasoning=reasoning,
        tool_calls=[{"id": c[0], "name": c[1], "arguments": c[2]} for c in calls],
        usage={"prompt_tokens": prompt, "completion_tokens": completion,
               "cache_read_input_tokens": prompt // 2, "cost_usd": cost},
        duration_ms=1840.0,
    )


#: Every call spec written once and referenced by id, because the harness emits the
#: *same dict object* in `model_response.tool_calls` and in `tool_call` — a fixture that
#: strips the arguments in one of the two invents a divergence the reader is right to
#: report as a hook rewrite, and then the diff renders the stripped copy.
CALLS: dict[str, tuple[str, dict]] = {}


def spec(call_id, name, arguments):
    CALLS[call_id] = (name, arguments)
    return (call_id, name, arguments)


def tool(turn, call_id):
    name, arguments = CALLS[call_id]
    return event("tool_call", turn=turn, id=call_id, name=name, arguments=arguments)


def result(turn, call_id, content, *, is_error=False, ms=120.0):
    return event("tool_result", turn=turn, tool_call_id=call_id, content=content,
                 is_error=is_error, duration_ms=ms)


def metrics(turn, **extra):
    return event("turn_metrics", turn=turn, model_ms=1840.0, tool_ms_total=340.0,
                 tool_wall_ms=180.0, cache_hit_rate=0.5, **extra)


def end(success=True, turns=3, reason="task_complete"):
    return event("session_end", success=success, turns=turns, reason=reason, via="gate")


print("Seeding inspector fixtures…")

# 1. The happy path: an approved gate with real evidence, and a write_file diff.
write("a1-approved-gate", "Create fib.py with a fibonacci function and verify it", [
    start(),
    event("environment_snapshot", chars=1840),
    budget(1, 0.11),
    response(1, "I'll write the module first.", [
        spec("call_a", "write_file", {"path": "fib.py",
         "content": "def fib(n: int) -> int:\n    if n < 2:\n        return n\n"
                    "    a, b = 0, 1\n    for _ in range(n - 1):\n        a, b = b, a + b\n"
                    "    return b\n"}),
    ]),
    tool(1, "call_a"),
    result(1, "call_a", "Wrote fib.py (163 bytes, 7 lines)"),
    metrics(1),
    budget(2, 0.19),
    response(2, "Now a quick check that it agrees with the closed form.", [
        spec("call_b", "bash", {"command": "python -c \"from fib import fib; "
                                       "assert [fib(i) for i in range(8)] == [0,1,1,2,3,5,8,13]; "
                                       "print('ok')\""}),
    ]),
    tool(2, "call_b"),
    result(2, "call_b", "ok\n", ms=310.0),
    metrics(2),
    budget(3, 0.26),
    response(3, "Verified.", [
        spec("call_c", "task_complete", {
            "summary": "Added fib.py with an iterative fibonacci and verified the first "
                       "eight terms against the known sequence.",
            "verification_commands": ["python -c \"from fib import fib; assert fib(10)==55\""],
        }),
    ]),
    event("contract", action="gate_check", outstanding=[]),
    event("verification", turn=3, approved=True, attempt=1,
          summary="Added fib.py with an iterative fibonacci and verified the first eight terms.",
          checklist={"summary_present": True, "summary_length": True,
                     "evidence_discriminating": True, "verify_cmd_0": True,
                     "verification_stable": True, "side_effects_clear": True},
          evidence=[{"command": "python -c \"from fib import fib; assert fib(10)==55\"",
                     "class": "discriminating", "exit_code": 0, "stdout": "", "stderr": ""}]),
    metrics(3),
    end(True, 3),
])

# 2. A rejected gate, then an accepted one: two attempts in one turn, an edit diff,
#    a tool error, and a permission denial.
write("b2-rejected-then-passed", "Fix the off-by-one in parse_range and prove it", [
    start(),
    budget(1, 0.14),
    response(1, "Let me look at the current implementation.",
             [spec("r1", "read_file", {"path": "ranges.py"})],
             reasoning="The bug report says the upper bound is excluded when it "
                       "shouldn't be. Most likely a range() call."),
    tool(1, "r1"),
    result(1, "r1", "def parse_range(spec):\n    lo, hi = spec.split('-')\n"
                    "    return list(range(int(lo), int(hi)))\n"),
    metrics(1),
    budget(2, 0.22),
    response(2, "Found it — the upper bound is exclusive.", [
        spec("e1", "edit", {"path": "ranges.py",
                        "old_string": "    return list(range(int(lo), int(hi)))",
                        "new_string": "    return list(range(int(lo), int(hi) + 1))"}),
        spec("e2", "bash", {"command": "rm -rf /"}),
    ]),
    tool(2, "e1"),
    result(2, "e1", "Applied 1 edit to ranges.py"),
    event("permission_ask", turn=2, name="bash", id="e2", approved=False,
          reason="`rm -rf /` matches a denied command pattern."),
    metrics(2, tool_errors=0),
    budget(3, 0.31),
    response(3, "Let me run the tests.",
             [spec("t1", "bash", {"command": "pytest tests/test_ranges.py -q"})]),
    tool(3, "t1"),
    result(3, "t1", "F.\n1 failed, 1 passed\nE   assert parse_range('1-3') == [1,2,3]\n"
                    "E   AssertionError: got [1, 2, 3, 4]  [buffer:pytest_out_01]",
           is_error=True, ms=2100.0),
    metrics(3, tool_errors=1),
    budget(4, 0.38),
    response(4, "That overshot. Reverting to a bounded slice.", [
        spec("e3", "multi_edit", {"path": "ranges.py", "edits": [
            {"old_string": "int(hi) + 1", "new_string": "int(hi) + 1 if inclusive else int(hi)"},
            {"old_string": "def parse_range(spec):",
             "new_string": "def parse_range(spec, *, inclusive=True):"},
        ]}),
    ]),
    tool(4, "e3"),
    result(4, "e3", "Applied 2 edits to ranges.py"),
    metrics(4),
    budget(5, 0.44),
    response(5, "Done.", [spec("g1", "task_complete", {
        "summary": "Fixed it.",
        "verification_commands": ["ls ranges.py"],
    })]),
    event("verification", turn=5, approved=False, attempt=1,
          summary="Fixed it.",
          checklist={"summary_present": True, "summary_length": False,
                     "evidence_discriminating": False, "verify_cmd_0": True},
          evidence=[{"command": "ls ranges.py", "class": "non_discriminating", "exit_code": 0,
                     "stdout": "ranges.py\n", "stderr": ""}],
          feedback="`ls ranges.py` cannot fail whether or not the bug is fixed, so it is "
                   "not evidence. Run the test that reproduced the bug. The summary is also "
                   "too short to describe what changed."),
    metrics(5),
    # The resubmission is the NEXT turn, not a second response in this one: a gate
    # rejection appends its feedback and the loop continues, so attempt 2 has its own
    # budget snapshot and its own metrics.
    budget(6, 0.51),
    response(6, "Understood — running the actual test.", [spec("g2", "task_complete", {
        "summary": "parse_range now takes an `inclusive` keyword defaulting to True and "
                   "adds 1 to the upper bound only when it is set, so '1-3' yields [1,2,3] "
                   "and the exclusive callers are unaffected.",
        "verification_commands": ["pytest tests/test_ranges.py -q"],
    })]),
    event("verification", turn=6, approved=True, attempt=2,
          summary="parse_range now takes an `inclusive` keyword defaulting to True.",
          checklist={"summary_present": True, "summary_length": True,
                     "evidence_discriminating": True, "verify_cmd_0": True,
                     "verification_stable": True},
          evidence=[{"command": "pytest tests/test_ranges.py -q", "class": "discriminating",
                     "exit_code": 0, "stdout": "2 passed\n", "stderr": ""}]),
    metrics(6),
    end(True, 6),
], turns=6, buffers={"pytest_out_01": "=" * 40 + "\nFAILED tests/test_ranges.py::test_inclusive\n"
                    + "assert [1, 2, 3, 4] == [1, 2, 3]\n" + "-" * 40 + "\n"})

# 3. Context pressure: a prune, an overflow retry, a missing snapshot (the gap), and a
#    final_submission band sharing the last turn number.
write("c3-context-pressure", "Summarise every file under src/ and write ARCHITECTURE.md", [
    start(condenser="microcompact"),
    budget(1, 0.31),
    response(1, "Reading the tree.", [spec("s1", "glob", {"pattern": "src/**/*.py"})]),
    tool(1, "s1"),
    result(1, "s1", "\n".join(f"src/mod_{i}.py" for i in range(40))),
    metrics(1),
    budget(2, 0.58),
    response(2, "Reading them in batches.", [spec("s2", "read_file", {"path": "src/mod_0.py"})]),
    tool(2, "s2"),
    result(2, "s2", "# " + "x" * 400),
    metrics(2),
    # Turn 3 has no budget event at all — this is the gap the chart must not interpolate.
    response(3, "Continuing.", [spec("s3", "read_file", {"path": "src/mod_1.py"})]),
    tool(3, "s3"),
    result(3, "s3", "# " + "y" * 400),
    metrics(3),
    budget(4, 0.77),
    event("summarization", turn=4, reason="proactive", strategy="microcompact", action="prune",
          pruned=11, duration_ms=64.0, tokens_before=92400, tokens_after=61200,
          messages_before=48, messages_after=48),
    response(4, "Compacted; carrying on.", [spec("s4", "read_file", {"path": "src/mod_2.py"})]),
    tool(4, "s4"),
    result(4, "s4", "# " + "z" * 400),
    metrics(4),
    budget(5, 0.94),
    event("summarization", turn=5, reason="context_overflow", strategy="microcompact",
          action="summarize", duration_ms=2140.0, tokens_before=118400, tokens_after=52100,
          messages_before=52, messages_after=14, recovered=True),
    response(5, "Recovered from an overflow. Writing the document now.", [
        spec("w1", "write_file", {"path": "ARCHITECTURE.md",
                              "content": "# Architecture\n\n40 modules under src/.\n"}),
    ]),
    tool(5, "w1"),
    result(5, "w1", "Wrote ARCHITECTURE.md (44 bytes, 3 lines)"),
    metrics(5),
    event("budget", stage=0.8, turn=6, note="80% of the turn budget spent"),
    budget(6, 0.61),
    response(6, "Nearly out of turns; wrapping up.", []),
    metrics(6),
    # The labelled band: same turn number, opened by stage="final_submission".
    event("budget", stage="final_submission", turn=6),
    response(6, "Final answer: ARCHITECTURE.md documents all 40 modules.", [], completion=900),
    metrics(6, label="final_submission"),
    end(True, 6, reason="max_turns"),
], turns=6)

# 4. A killed run: a pending tool step and a turn with no turn_metrics.
write("d4-killed-midrun", "Run the full integration suite and fix what breaks", [
    start(),
    budget(1, 0.12),
    response(1, "Starting the suite.", [spec("k1", "bash", {"command": "pytest -q"})]),
    tool(1, "k1"),
    result(1, "k1", "14 failed, 220 passed", is_error=True, ms=48000.0),
    metrics(1, tool_errors=1),
    budget(2, 0.21),
    response(2, "Reproducing the first failure in isolation.", [
        spec("k2", "bash", {"command": "pytest tests/test_pipeline.py::test_backpressure -x"}),
    ]),
    tool(2, "k2"),
    # No tool_result and no turn_metrics: this is where it was killed.
], success=False, turns=2, finish=False)

# 5. A rigorous run: plan → execute → critic → repair.
write("e5-rigorous-phases", "Refactor the retry logic to use exponential backoff", [
    start(mode="rigorous"),
    budget(1, 0.14),
    response(1, "Plan: locate the retry sites, add a backoff helper, migrate callers.", []),
    metrics(1),
    event("user_message", content="[rigorous:plan] 1. Locate retry sites 2. Add helper "
                                 "3. Migrate callers 4. Test"),
    budget(1, 0.18),
    response(1, "Adding the helper.", [
        spec("p1", "write_file", {"path": "retry.py",
                              "content": "def backoff(attempt: int) -> float:\n"
                                         "    return min(2 ** attempt * 0.1, 30.0)\n"}),
    ]),
    tool(1, "p1"),
    result(1, "p1", "Wrote retry.py (81 bytes, 2 lines)"),
    metrics(1),
    budget(2, 0.24),
    response(2, "Migrated the callers.", [spec("p2", "bash", {"command": "pytest tests/ -q -k retry"})]),
    tool(2, "p2"),
    result(2, "p2", "6 passed\n"),
    metrics(2),
    event("verification", phase="critic", approved=False, attempt=1,
          feedback="The backoff has no jitter, so every client retries in lockstep after a "
                   "shared outage. Add full jitter before calling this done."),
    budget(1, 0.29),
    response(1, "Adding jitter.", [
        spec("p3", "edit", {"path": "retry.py",
                        "old_string": "    return min(2 ** attempt * 0.1, 30.0)",
                        "new_string": "    ceiling = min(2 ** attempt * 0.1, 30.0)\n"
                                      "    return random.uniform(0, ceiling)"}),
    ]),
    tool(1, "p3"),
    result(1, "p3", "Applied 1 edit to retry.py"),
    metrics(1),
    event("verification", phase="critic", approved=True, attempt=2,
          feedback="Full jitter is present and the ceiling is unchanged. Accepted."),
    end(True, 4),
], turns=4, agent="build", mode="rigorous")

# 6. An old log: seven event types, no config, no turn numbers. The 1.1.0 vintage that
#    138 real harbor trials are written in.
write("f6-old-format", "Legacy log with no config and no turn attribution", [
    {"type": "session_start", "timestamp": ts(),
     "payload": {"task": "legacy", "model": "fireworks/llama-v3", "agent": "harbor"}},
    {"type": "model_response", "timestamp": ts(),
     "payload": {"content": "Looking around.",
                 "tool_calls": [{"id": "o1", "name": "ls", "arguments": {"path": "."}}],
                 "usage": {"prompt_tokens": 900, "completion_tokens": 40}}},
    {"type": "tool_call", "timestamp": ts(), "payload": {"id": "o1", "name": "ls",
                                                         "arguments": {"path": "."}}},
    {"type": "tool_result", "timestamp": ts(), "payload": {"tool_call_id": "o1",
                                                           "content": "README.md\nsrc\n"}},
    {"type": "model_response", "timestamp": ts(),
     "payload": {"content": "Done.", "tool_calls": [],
                 "usage": {"prompt_tokens": 1100, "completion_tokens": 20}}},
    {"type": "session_end", "timestamp": ts(), "payload": {"success": True}},
], turns=2, model="fireworks/llama-v3", agent="harbor")

# 7. A truncated response — the provider cut it at max_tokens mid tool call.
write("g7-truncated", "Generate a 5000-line fixture file", [
    start(),
    budget(1, 0.15),
    response(1, "Writing it out.", [spec("x1", "write_file", {"path": "fixture.py",
                                                          "content": "# line\n" * 200})]),
    event("model_response", truncated=True),
    tool(1, "x1"),
    result(1, "x1", "Wrote fixture.py (1400 bytes, 200 lines)"),
    metrics(1),
    budget(2, 0.71),
    response(2, "That got cut off at max_tokens. Switching to a generator script.", []),
    metrics(2),
    end(False, 2, reason="max_turns"),
], success=False, turns=2)

# 8. A run that delegates: an `invoke_subagent` step, its handoff, and the subagent's own
#    log written as a sibling — the two halves of the join the nested trace needs.
SUB_ID = "44444444-4444-4444-4444-444444444444"

write("h8-subagent", "Research the retry semantics, then implement them", [
    start(),
    budget(1, 0.13),
    response(1, "This needs reading before writing. Delegating the research.", [
        spec("d1", "invoke_subagent", {"profile": "research",
                                       "task": "Find how urllib3 implements Retry backoff"}),
    ]),
    tool(1, "d1"),
    event("user_message",
          content="[subagent:research] urllib3 uses backoff_factor * (2 ** (retries - 1)) "
                  "capped by BACKOFF_MAX, with optional jitter.",
          subagent="research", success=True, session_id=SUB_ID, turns=2,
          task="Find how urllib3 implements Retry backoff"),
    result(1, "d1", "urllib3 uses backoff_factor * (2 ** (retries - 1)) capped by BACKOFF_MAX."),
    metrics(1),
    budget(2, 0.24),
    response(2, "Implementing the same shape.", [
        spec("d2", "write_file", {"path": "backoff.py",
                                  "content": "BACKOFF_MAX = 120.0\n\n\n"
                                             "def delay(retries: int, factor: float) -> float:\n"
                                             "    return min(factor * 2 ** (retries - 1), "
                                             "BACKOFF_MAX)\n"}),
    ]),
    tool(2, "d2"),
    result(2, "d2", "Wrote backoff.py (128 bytes, 5 lines)"),
    metrics(2),
    end(True, 2),
], turns=2)

# The subagent's own log, in the sibling directory the parent's handoff points at. Written
# after `write` so the session directory already exists.
sub_dir = store.session_dir("h8-subagent") / "subagents"
sub_dir.mkdir(parents=True, exist_ok=True)
CLOCK[0] = 0
SUB_RECORDS = [
    event("session_start", task="Find how urllib3 implements Retry backoff",
          model="openrouter/deepseek/deepseek-v4-flash-0731", agent="research", mode="readonly"),
    budget(1, 0.08),
    response(1, "Looking for the Retry class.",
             [spec("sa1", "grep", {"pattern": "backoff_factor", "path": "."})]),
    tool(1, "sa1"),
    result(1, "sa1", "urllib3/util/retry.py:412:  backoff_value = self.backoff_factor * (2 ** ...)"),
    metrics(1),
    budget(2, 0.15),
    response(2, "Reading the implementation.",
             [spec("sa2", "read_file", {"path": "urllib3/util/retry.py"})]),
    tool(2, "sa2"),
    result(2, "sa2", "    def get_backoff_time(self):\n        ...\n"),
    metrics(2),
    end(True, 2),
]
(sub_dir / f"{SUB_ID}.jsonl").write_text(
    "\n".join(json.dumps(r) for r in SUB_RECORDS) + "\n", encoding="utf-8"
)
print(f"  h8-subagent/subagents/{SUB_ID[:8]}…  {len(SUB_RECORDS)} events  nested trace")

print(f"\nSeeded {len(list(ROOT.iterdir()))} sessions at {ROOT}")
