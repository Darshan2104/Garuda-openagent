# Pluggable Terminal Strategy Design

**Issue:** [#75](https://github.com/Darshan2104/Garuda-openagent/issues/75)

**Status:** Design approved; written-spec review pending

**Date:** 2026-09-28

## Objective

Make native-loop termination pluggable without introducing a second agent loop.
Normal runs must retain the existing `task_complete` completion gate and its
verification semantics. A later collection worker must be able to terminate
through its own structured tool, such as `submit_collection`, by supplying a
different strategy for that run.

## Scope

This change extracts only the loop's terminal-tool control flow:

- terminal-tool identity;
- whether forced final submission is supported;
- asynchronous acceptance or rejection of a terminal call;
- deferred-note flushing after tool-call adjacency has been restored.

It does not implement collection execution, collection report validation, new
tool effects, or any main-task verification change.

## Public boundary

Add `garuda/core/termination.py` with two small contracts:

- `TerminalDecision`: an immutable result containing `accepted` and `summary`;
- `TerminalStrategy`: a protocol exposing `tool_name`,
  `supports_forced_submission`, asynchronous `attempt(call, turn=...)`, and
  synchronous `flush_notes()`.

`TaskCompletionStrategy` adapts the existing `CompletionGate` to that protocol.
It delegates attempts and note flushing rather than moving verification logic.
The adapter converts the gate's existing `(approved, summary)` tuple into a
`TerminalDecision`.

`DefaultAgent.run` and `prepare_run` gain an optional per-run
`terminal_strategy` parameter. When absent, `prepare_run` constructs the current
`CompletionGate` and wraps it in `TaskCompletionStrategy`. Per-run injection is
required because a reusable agent instance may serve normal and specialized
child runs concurrently; constructor or configuration-file state would couple
otherwise independent runs.

## Runtime flow

`RunState` carries the resolved terminal strategy. The loop compares tool calls
to `strategy.tool_name` instead of the literal `task_complete` string.

For an ordinary turn:

1. The assistant response is recorded with all tool calls.
2. When the terminal call is reached, the strategy evaluates it.
3. A rejection remains strategy-owned. The default adapter preserves the
   existing gate behavior, including the immediate terminal tool result and
   deferred notes.
4. On acceptance, the loop answers the accepted call and every still-open
   sibling call before calling `strategy.flush_notes()`.
5. The existing session-end event and successful `AgentResult` are produced.

`RunState.answer_open_calls` receives the terminal tool name so its generated
messages can describe an alternate strategy. With the default
`task_complete` name, existing message text remains unchanged.

## Forced final submission

The final-submission exchange remains part of the existing loop. It runs only
when all of these are true:

- `force_final_submission` is enabled;
- verification is enabled;
- the strategy supports forced submission;
- the strategy's terminal tool is present in the run's tool map and schema.

The exchange exposes only the strategy's terminal schema, locates only that
terminal call in the response, and evaluates it through the same strategy. A
strategy that does not support forced submission exhausts cleanly without an
extra model call. Responses that omit the expected terminal call still have all
open tool calls answered before exhaustion.

## Compatibility and failure behavior

- `CompletionGate` remains the owner of contracts, side-effect sweeping,
  verification, rejection feedback, and verification events.
- Default `task_complete` acceptance, rejection, livelock, forced-submission,
  and session-event behavior remains unchanged.
- `EnvironmentUnavailableError` follows the existing abort path.
- Strategy exceptions propagate through the same path as current completion
  errors; this refactor does not add a silent fallback.
- An unavailable terminal tool does not create a new configuration failure.
  Forced submission is skipped as it is today when `task_complete` is absent.
- Tool-call adjacency remains mandatory: no user-role deferred note may appear
  between an assistant tool-call block and the corresponding tool results.

## Tests

Create `tests/test_termination_strategy.py` with a deterministic fake strategy
and alternate terminal tool. Cover:

- a normal run still defaults to `task_complete`;
- an alternate terminal tool completes an isolated run;
- an alternate rejection continues the run with valid adjacency;
- an accepted terminal call closes every unexecuted sibling call;
- a strategy without forced-submission support exhausts without another model
  request;
- forced submission exposes only the configured terminal tool when supported.

Extend existing adjacency coverage where needed. Run:

```bash
pytest tests/test_termination_strategy.py tests/test_tool_call_adjacency.py tests/test_verifier_v2.py tests/test_gate_livelock_and_rewrite.py tests/test_core_v2.py -q
ruff check garuda tests
pytest -q
```

## Documentation and rollout

Update `docs/MODULES.md` to name the terminal-strategy boundary. No user-facing
configuration, feature flag, or migration is introduced. The implementation is
delivered as a focused PR for #75, merged only after required CI passes. Issue
#78 follows after #75 is closed.
