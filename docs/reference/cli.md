# CLI reference

| Command | Use |
|---|---|
| `garuda run -t "…"` | Execute a headless task. |
| `garuda chat` | Start an interactive session. |
| `garuda serve` | Run the authenticated JSON-RPC job queue. |
| `garuda web` | Serve the local dashboard. |
| `garuda sessions` | List resumable persisted sessions. |
| `garuda eval dual-model report ...` | Build a paired rollout report from completed native sessions. |
| `garuda mcp list` | Resolve and inspect MCP configuration. |
| `garuda recipe run file.yaml` | Execute a YAML workflow. |
| `garuda runtime list [--json] [--workspace W]` | List configured runtimes with health (globally disabled ones read unavailable). |
| `garuda runtime inspect <id> [--json] [--workspace W]` | Inspect one runtime, login, and quota. |
| `garuda runtime handoff --session S --to R [--workspace W] [--confirm]` | Preview (default) or execute a one-shot handoff to an ACP runtime (see below). |
| `garuda runtime resume --session S -t TASK` | Resume a persisted native session through the real run lifecycle (classifies first; refuses a session an external runtime owns). |
| `garuda runtime recover --session S [--json]` | Classify and recover a session: `resumable`, `rolled_back`, or `external`. |
| `garuda runtime reclaim --session S` | Return an `external` session to native once the target is proven stopped: no lease names it, no recorded child is alive, and the target left a retired child or a `closed`/`failed` state. |
| `garuda runtime support --session S` | Print a redacted support bundle (lanes, tallies, metrics). |

## ACP runs and handoffs

`garuda run --runtime <id>` starts a trusted ACP catalog entry. A handoff without
`--confirm` is a preview; confirmed handoff transfers session ownership. Runtime
session arguments accept a full ID, unique prefix, or `latest`.

ACP completion means the harness ended its turn, not that Garuda verified the
result. See [External harnesses](../guides/external-harnesses.md) for discovery,
authentication, approvals, transaction ordering, failure behavior, recovery,
reclaim, and support bundles.

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
--docker-image             container image for docker/remote workspaces
--docker-host              remote Docker daemon host
--allow-network            allow egress for the sandbox workspace kind
--no-network               disable egress for docker/remote containers
--allow-unsandboxed        allow an unconfined fallback if OS sandboxing is unavailable
--resume                   prior session ID, prefix, or latest
--runtime                  executor runtime id (default: native)
--reasoning-effort         portable reasoning setting
--thinking-budget          Anthropic thinking budget
--max-turns                agent turn limit
--json                     emit event JSON
--trajectory               write a trajectory file
```

The three network and unsandboxed flags above are accepted only by `garuda run`.
`--docker-image` and `--docker-host` are also accepted by `garuda chat`,
`garuda serve`, and `garuda recipe run`. `--allow-unsandboxed` permits execution
without an OS sandbox when the requested backend is unavailable; it does not
create an isolation boundary. Use `garuda run --help` as the source of truth for
version-specific run flags.

## Paired dual-model reports

`garuda eval dual-model report` is an offline, read-only command. Supply an
exact task-mix manifest, one `--baseline TASK=SESSION` and one
`--candidate TASK=SESSION` for every task, pinned `--model-version` values,
`--price-source`, `--prompt-revision`, and `--output`. It refuses incomplete or
non-terminal input sessions and existing output unless `--overwrite` is set.
It does not launch a model or read provider credentials. Add
`--require-passing-gates` when a valid report with failed rollout gates must
produce a nonzero exit status. See [dual-model evaluation](../evaluation/dual-model-routing.md)
for the manifest and full example.
