# CLI reference

| Command | Use |
|---|---|
| `garuda run -t "…"` | Execute a headless task. |
| `garuda chat` | Start an interactive session. |
| `garuda serve` | Run the authenticated JSON-RPC job queue. |
| `garuda web` | Serve the local dashboard. |
| `garuda sessions` | List resumable persisted sessions. |
| `garuda mcp list` | Resolve and inspect MCP configuration. |
| `garuda recipe run file.yaml` | Execute a YAML workflow. |
| `garuda runtime list [--json] [--workspace W]` | List configured runtimes with health (globally disabled ones read unavailable). |
| `garuda runtime inspect <id> [--json] [--workspace W]` | Inspect one runtime, login, and quota. |
| `garuda runtime handoff --session S --to R [--workspace W] [--confirm]` | Preview (default) or execute a one-shot handoff to an ACP runtime (see below). |
| `garuda runtime resume --session S -t TASK` | Resume a persisted native session through the real run lifecycle (classifies first; refuses a session an external runtime owns). |
| `garuda runtime recover --session S [--json]` | Classify and recover a session: `resumable`, `rolled_back`, or `external`. |
| `garuda runtime reclaim --session S` | Return an `external` session to native once its target is recorded `closed`/`failed`, no lease names it, and no recorded child is alive. |

## ACP runs and handoffs

`garuda run --runtime <id>` with an ACP runtime resolves and discovers it
through the trusted catalog (unknown, disabled, or missing executables refuse
before anything starts), then holds the same invariants as a native run: the
workspace's mutating lease (a live foreign holder refuses), a persisted session
whose only segment is that runtime, the workspace baseline before the prompt and
the delta after, approvals through the session broker (headless: every ask is
denied and audited; on a TTY: a y/N prompt), and a recorded child that is
reaped and retired on close. The harness session is rooted at the absolute
workspace. The session status is `completed` when the agent ended its turn —
Garuda does not verify an ACP result — or `failed`.

`garuda runtime handoff --confirm` is one-shot: pause the native source →
checkpoint → capture the workspace delta → start the target (the exact
executable the pre-check discovered) → acknowledge (handoff state and the
target's active segment are persisted together, then its child is recorded) →
close the source → send the handoff package as the target's first prompt
(bounded, 600 s) → close the target and record `target_state: closed`. A
failure before acknowledgement rolls back to the source; a failure after it is
a target failure — the target is closed and `target_state: failed` recorded,
with no rollback over changes it may have made. Either way the session then
belongs to the target: `recover` reports `external`, and native resume or a
second handoff refuses until `garuda runtime reclaim` returns it to native
(refused while the target may still be acting). If a failed handoff cannot even
record its return to the source, the error names this command. Native sessions
record a relative workspace, so pass `--workspace`.

## Common `run` flags

```text
-t, --task                 task text
-f, --file                 task file
--model                    LiteLLM provider/model
--agent                    profile: build, plan, explore, reviewer, harbor
--mode                     interactive, eval, rigorous, readonly
--permission-mode          smart, auto, readonly, yolo
--workspace                workspace root
--workspace-kind           local, sandbox, tmux, docker, remote
--resume                   prior session ID, prefix, or latest
--runtime                  executor runtime id (default: native)
--reasoning-effort         portable reasoning setting
--thinking-budget          Anthropic thinking budget
--max-turns                agent turn limit
--json                     emit event JSON
--trajectory               write a trajectory file
```

Use `garuda <command> --help` as the source of truth for version-specific flags.
