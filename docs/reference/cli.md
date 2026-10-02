# CLI reference

Every command and its flags. `--help` on any command (for example `garuda run --help`) is the source of truth
for your installed version. For ready-made examples, see the
[Cheat sheet](cheat-sheet.md).

## Commands

| Area | Command | Use |
|---|---|---|
| Run tasks | `garuda run -t "…"` | Execute one headless task |
| | `garuda chat` | Interactive session with `y/N` permission prompts |
| | `garuda recipe run file.yaml` | Execute a YAML workflow |
| Sessions | `garuda sessions` | List recent saved sessions |
| | `garuda sessions merge S --check CMD [--into BRANCH]` | Check a worktree session merged into a branch (in Docker) and publish `refs/garuda/integration/<S>`; never changes your checkout |
| | `garuda sessions remove-worktree S [--force]` | Remove a worktree session's worktree; refuses unpublished work without `--force` |
| | `garuda doctor --recover-project-ids` | Rebuild session project ids after the project key was lost |
| Interfaces | `garuda web` | Serve the local dashboard (alias: `garuda dashboard`) |
| | `garuda serve` | Run the authenticated JSON-RPC job service |
| Configuration | `garuda mcp list` | Resolve and inspect MCP configuration |
| | `garuda mcp trust` | Review and trust MCP servers the project's own config defines |
| Runtimes | `garuda runtime list [--json] [--workspace W]` | List configured runtimes with health (globally disabled ones read unavailable) |
| | `garuda runtime inspect <id> [--json] [--workspace W]` | Inspect one runtime, login, and quota |
| | `garuda runtime handoff --session S --to R [--workspace W] [--confirm]` | Preview (default) or execute a one-shot handoff to an ACP runtime |
| | `garuda runtime resume --session S -t TASK` | Resume a native session through the normal run lifecycle; refuses a session an external runtime owns |
| | `garuda runtime recover --session S [--json]` | Classify and recover a session: `resumable`, `rolled_back`, or `external` |
| | `garuda runtime reclaim --session S` | Return an `external` session to native once the target is proven stopped |
| | `garuda runtime support --session S` | Print a redacted support bundle (lanes, tallies, metrics) |
| Evaluation | `garuda eval dual-model report ...` | Build a paired rollout report from completed native sessions |

`--resume` continues a native session from its transcript. A session of an
ACP runtime continues through the agent's own reload where that is proven for
the adapter version, and otherwise through a new linked session started from
its brief; a session whose owner is still running refuses.

Session arguments (`--resume`, `--session`) accept a full ID, a unique prefix
from `garuda sessions`, a session name, or `latest`. Names, prefixes and
`latest` are looked up in the current project; add `--all-projects` to search
every project. A full ID always resolves.

## Common `run` flags

=== "Task and model"

    | Flag | Use |
    |---|---|
    | `-t, --task TEXT` | Task text |
    | `-f, --file PATH` | Read the task from a file |
    | `--resume ID` | Continue a saved session (ID, prefix, or `latest`: this project's newest) |
    | `--all-projects` | With `--resume latest`, take the newest session from any project |
    | `--as RUNTIME` | With `--resume`, continue on another runtime: a new linked session started from the resumed session's brief |
    | `--name NAME` | Name the session (unique in the project; defaults to a slug of the task) |
    | `--with NAME` | Attach the brief of a session in this project (repeatable); `@name` in the task does the same |
    | `--with-id FULL_ID` | Attach a session's brief by full id; another project's needs `--allow-cross-project-context` or a yes when asked |
    | `--allow-cross-project-context` | Grant `--with-id` sessions from other projects without asking (headless) |
    | `--model MODEL` | Reasoning model, `provider/model` (alias of `--reasoning-model`) |
    | `--collection-model MODEL` | Optional collection model (needs collection enabled in trusted settings) |
    | `--no-collection` | Turn the collection role off for this run |
    | `--reasoning-effort LEVEL` | `minimal`, `low`, `medium`, or `high` extended thinking |
    | `--thinking-budget TOKENS` | Anthropic extended-thinking budget |

=== "Workspace"

    | Flag | Use |
    |---|---|
    | `--workspace DIR` | Workspace root (default: current directory) |
    | `--isolation MODE` | `shared` (default): edit the workspace; `worktree`: a linked worktree on branch `garuda/<session>`; `auto`: a worktree only while another session edits. Uncommitted source changes are not carried over. Not confinement |
    | `--workspace-kind KIND` | `local` (default), `sandbox`, `tmux`, `docker`, or `remote` |
    | `--docker-image IMAGE` | Container image for docker/remote (default `ubuntu:22.04`) |
    | `--docker-host HOST` | Remote Docker daemon host |
    | `--docker-memory SIZE` | Container memory limit, e.g. `4g` (default `2g`) |
    | `--docker-cpus N` | Container CPU limit (default `2`) |
    | `--no-network` | Disable container networking for docker/remote (default: bridged) |
    | `--allow-network` | Allow network inside the `sandbox` kind (default: denied) |
    | `--allow-unsandboxed` | Run `sandbox` unconfined if no OS backend exists (default: refuse) |

=== "Agent and permissions"

    | Flag | Use |
    |---|---|
    | `--agent NAME` | Profile: `build` (default), `plan`, `explore`, `reviewer`, `harbor`, or your own |
    | `--agents-dir DIR` | Profiles directory to use instead of `.agent/agents` and `.garuda/agents` (built-ins still apply) |
    | `--mode MODE` | `interactive` (default), `readonly`, `eval`, or `rigorous` (`standard` = `interactive`) |
    | `--permission-mode MODE` | `smart`, `readonly`, `auto`, or `yolo`; overrides the mode and profile |
    | `--mcp-config PATH` | Explicit MCP config (skips discovery and merge) |
    | `--load-project-tools` | Import `.agent/tools/*.py` (runs repository code) |
    | `--runtime ID` | A trusted runtime ID or alias. Naming one, including `native`, pins it; omit the flag to let routing rules choose (native is the default) |

=== "Limits and output"

    | Flag | Use |
    |---|---|
    | `--max-turns N` | Agent turn limit |
    | `--deadline-sec N` | Wall-clock budget; the agent paces itself and reserves turns to finish |
    | `--json` | Print JSONL events instead of the human-readable result |
    | `--trajectory PATH` | Also save the run's events to a JSONL file |

=== "Advanced toggles"

    | Flag | Use |
    |---|---|
    | `--persistent-shell` | Keep one shell alive across `bash` calls (local workspace) |
    | `--no-post-edit-diagnostics` | Skip the syntax check after file edits |
    | `--no-post-edit-lint` | Skip the fast Python lint after file edits |
    | `--no-bootstrap` | Skip the session-start environment probe |
    | `--no-verifier` | Accept the model's first final answer without the completion gate |
    | `--no-three-step-summary` | Turn off the three-step summarizer used when context is compacted |

The network and unsandboxed flags are accepted only by `garuda run`.
`--docker-image` and `--docker-host` are also accepted by `garuda chat`,
`garuda serve`, and `garuda recipe run`. `--allow-unsandboxed` permits execution
without an OS sandbox when the backend is unavailable; it does not create an
isolation boundary.

## Other commands

=== "chat"

    `garuda chat` takes the model, workspace, agent, MCP, `--mode`, and
    `--permission-mode` flags of `run`, plus `--json`. It has no `-t`: type
    tasks at the `task>` prompt, and finish with an empty line or ++ctrl+d++.
    `--with`, `--with-id` and `--allow-cross-project-context` attach briefs to
    the first turn; `@name` in any message attaches that session's brief to
    that turn.

=== "sessions"

    `garuda sessions [--limit N]` lists recent sessions with ID prefix, status,
    turns, update time, and task.

=== "web"

    `garuda web` flags are listed in the
    [Web dashboard guide](../guides/web-dashboard.md#start-the-dashboard).

=== "serve"

    | Flag | Use |
    |---|---|
    | `--host HOST` | Bind address (default `127.0.0.1`); non-loopback requires a token |
    | `--port N` | Port (default `8765`) |
    | `--token TOKEN` | Bearer token (or `GARUDA_SERVE_TOKEN`); generated on loopback when unset |
    | `--max-jobs N` | Concurrent jobs (default `4`) |
    | `--model-max-concurrency N` | Cap concurrent model calls per provider across jobs (`0` = unlimited) |

    Plus the model, `--agent`, workspace, `--agents-dir`, and `--mcp-config`
    flags. Methods are listed in
    [Call Garuda over HTTP](../use-cases/automate.md#call-garuda-over-http).

=== "mcp list"

    `garuda mcp list [--workspace DIR] [--mcp-config PATH] [--no-connect]`
    shows the resolved config files and each server's tools. `--no-connect`
    lists servers without starting them. Untrusted project servers are listed
    but never started.

=== "mcp trust"

    `garuda mcp trust [NAME]... [--workspace DIR] [--mcp-config PATH] [--yes]` shows each untrusted
    project MCP server, what it would run or contact, and asks before trusting
    it. Without a terminal it refuses unless you pass `--yes`.

=== "recipe run"

    `garuda recipe run FILE [--param KEY=VALUE]...` runs a recipe. It takes the
    model, workspace, `--agents-dir`, and `--mcp-config` flags. See
    [Run a multi-step recipe](../use-cases/automate.md#run-a-multi-step-recipe).

## ACP runs and handoffs

`garuda run --runtime <id>` starts a trusted ACP catalog entry. A handoff without
`--confirm` is a preview; a confirmed handoff transfers session ownership.

ACP completion means the harness ended its turn, not that Garuda verified the
result. See [External harnesses](../guides/external-harnesses.md) for discovery,
authentication, approvals, transaction ordering, failure behavior, recovery,
reclaim, and support bundles.

## Paired dual-model reports

`garuda eval dual-model report` is an offline, read-only command. Supply:

- an exact task-mix manifest;
- one `--baseline TASK=SESSION` and one `--candidate TASK=SESSION` for every
  task;
- pinned `--model-version` values, `--price-source`, `--prompt-revision`, and
  `--output`.

It refuses incomplete or non-terminal input sessions, and existing output unless
`--overwrite` is set. It doesn't launch a model or read provider credentials.
Add `--require-passing-gates` when a valid report with failed rollout gates must
exit non-zero. See [dual-model evaluation](../evaluation/dual-model-routing.md)
for the manifest and a full example.
