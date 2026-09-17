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
| `garuda runtime list [--json]` | List configured runtimes with health. |
| `garuda runtime inspect <id> [--json]` | Inspect one runtime, login, and quota. |
| `garuda runtime handoff --session S --to R [--confirm]` | Preview (default) or prepare a handoff. |
| `garuda runtime recover --session S [--json]` | Classify and recover a session. |

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
