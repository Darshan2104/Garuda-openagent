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
| Starters | `garuda starter list [--workspace DIR] [--json]` | Discover the five packaged starters and configured readiness |
| | `garuda starter show ID [--workspace DIR] [--json]` | Inspect fields, supported options, and readiness |
| | `garuda starter run ID ... [--preview] [--json]` | Compile current inputs and sources; preview or explicitly launch through existing flow/role owners |
| | `garuda starter result SESSION [--workspace DIR] [--json] [--limit N] [--offset N]` | Read selected session evidence, coverage, and an explicit next action |
| Sessions | `garuda sessions [--json]` | List recent sessions with state and queue position; `--json` is the same model the dashboard serves |
| | `garuda sessions show S [--json]` | One session in full: the four facts, queue, pending approvals, flow |
| | `garuda sessions cancel S` | Stop a background session: remove it from the queue before it starts, or stop its running worker |
| | `garuda sessions merge S --check CMD [--into BRANCH] [--image IMAGE] [--timeout SEC]` | Check a worktree session merged into a branch (in Docker, no network; default image `python:3.12-slim`, 600 s per check) and publish `refs/garuda/integration/<S>`; never changes your checkout |
| | `garuda sessions remove-worktree S [--force]` | Remove a worktree session's worktree; refuses unpublished work without `--force` |
| | `garuda approvals list S` | List a running session's parked approvals |
| | `garuda approvals answer S ID --allow` | Answer one from another terminal (`--deny` refuses); bound to that exact request, one answer only |
| | `garuda doctor [--runtime ID] [--json]` | Check configuration, the harnesses your roles use (executable, version, login), leases and worktrees; exits 1 on an error |
| | `garuda doctor --recover-project-ids` | Rebuild session project ids after the project key was lost |
| Flows | `garuda flow run NAME -t TASK` | Run a `garuda.yaml` flow: its steps in order, each as its own session under its role |
| | `garuda flow show FLOW` | A flow's steps, attempts, receipts and outputs |
| | `garuda flow resume FLOW` | Continue after the last completed step; an interrupted step is never replayed |
| Interfaces | `garuda web` | Serve the local dashboard (alias: `garuda dashboard`) |
| | `garuda serve` | Run the authenticated JSON-RPC job service |
| Agents | `garuda agent list` | Every agent, where it comes from, what it extends, and which ones a nearer file shadows |
| | `garuda agent show NAME [--json]` | Each effective field with its source (`packaged`, `user`, `project`, `extends:<agent>`, `default`); secrets redacted, unsupported fields labelled |
| | `garuda agent prompt NAME [--json] [--raw]` | The static system prompt by section, with bytes, characters, estimated tokens and a digest |
| | `garuda agent check NAME_OR_PATH [--json]` | Every diagnostic, each with its code, field path and fix; exits 1 on an error |
| | `garuda agent new NAME [--from AGENT] [--project]` | Write a minimal definition (your agents directory by default); never overwrites |
| | `garuda agent migrate PATH [--write] [--accept-tightening]` | Preview a legacy profile as a version 1 definition (and confirm it resolves the same); `--write` replaces it with a backup, and refuses if behaviour would change. Where version 1 is only stricter, it also needs `--accept-tightening` |
| Memory | `garuda memory list [--json]` | The note proposals waiting for your review in this project |
| | `garuda memory review` | Accept, edit or reject each proposal; needs a terminal, and is the only way a note becomes memory |
| Configuration | `garuda init [--project] [--model HARNESS=ID] [--yes]` | Propose a user `garuda.yaml` (roles) or, with `--project`, the project's checks; writes only after confirmation |
| | `garuda config show [--flow NAME]` | The effective `garuda.yaml` and where each value came from, or one flow to copy |
| | `garuda config migrate [--write]` | Preview or write the user `garuda.yaml` that carries `settings.yaml`'s runtimes and capacity |
| | `garuda config trust` | Review and trust this project's `garuda.yaml` checks and native models (exact bytes; needs a terminal) |
| | `garuda mcp list` | Resolve and inspect MCP configuration |
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
    | `--bg` | Queue the run and return at once; a detached worker waits without a workspace lease. Effective roles use their runtime’s lane; dynamic role fallback refuses. Activated or ambiguous worker death retains capacity pending cleanup. Prints the session id |
    | `--no-edits` | Refuse edits and commands, and withhold the output if the workspace changed anyway (exit 3); a guardrail, not confinement |
    | `--check COMMAND` | An acceptance check run after the session (repeatable); its result is the session's verification |
    | `--role NAME` | Run as a `garuda.yaml` role: its harness, exact model, effort and permission ceiling |
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
    | `--agent-file PATH` | Run (or chat with) a definition file instead of a named agent. It selects a source and grants no trust: a file in the repository stays under the project ceiling. the `agent` inspection commands also take a path |
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

    `garuda sessions [--limit N] [--json]` lists this project's recent
    sessions with ID prefix, name, state, queue position, update time, and
    task. `--json` prints the same model the dashboard serves. The
    subcommands (`show`, `cancel`, `merge`, `remove-worktree`) take
    `--workspace DIR` to pick the project.

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
    | `--allow-agent NAME` | An agent a request may select by name; repeatable (unset: any the operator defines). A request never supplies a definition |
    | `--permission-ceiling MODE` | Loosest permission mode a requested agent may run with (default: that of `--agent`) |
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
exit non-zero, and `--evidence-scores FILE` to include independent quality
scores (JSON). See [dual-model evaluation](../evaluation/dual-model-routing.md)
for the manifest and a full example.


## Starters

`plan-change --goal TEXT` and `plan-feedback --feedback TEXT` run the packaged
`plan-only` flow. They accept requirements, exclusions (`--exclude`), constraints,
and repeated `--source` references. Feedback also accepts `--current` and
`--desired`. `build-review --goal TEXT` runs `plan-build-review`; its `--variant pair` requires an explicit `--plan-artifact FLOW:STEP:ATTEMPT`. Feedback can use the
same reference. Full validated plans and earlier approved constraints are
included in the actual task; neither preview nor result reading starts work.

```bash
garuda starter run plan-change --goal "Explain reconnect status" --constraints "Keep retry behavior" --workspace . --preview
garuda starter run plan-feedback --feedback "Explain the unavailable state" --workspace . --preview
garuda starter run build-review --goal "Add reconnect status" --workspace . --preview
garuda starter run run-with-role --role coder --goal "Add reconnect status" --check "pytest -q" --workspace . --preview
garuda starter run ask-role --question "Where is reconnect behavior defined?" --workspace . --preview
```

`run-with-role --goal TEXT` defaults to the configured coder and accepts `--role`,
`--name`, `--isolation shared|worktree|auto`, `--bg`, and repeated `--check`.
It uses existing native/ACP execution, capacity, background queue, worktree, and
acceptance owners. A worktree starts from committed source history; uncommitted
source edits are retained in the source checkout. `ask-role --question TEXT`
defaults to the reviewer, uses the existing no-edits guard in the live checkout,
and accepts `--role`, `--name`, and repeated `--source` references. Output withheld
by that guard stays withheld in the result. Use an idle checkout for questions.

Sources are repository-relative paths (optionally `#section`), `session:NAME`,
or `session-id:FULL_ID`. Cross-project briefs require the explicit
`--allow-cross-project-context` flag; full-plan handoffs remain same-project.
Files are named context references with digests: supplied context does not prove
that a runtime read a file. `--preview` performs no process, model, network,
admission, or store write. Start recompiles and revalidates before dispatch. Background workers revalidate
again after waiting for capacity, using the recorded explicit context grant.
Unsupported options refuse, including flow `--check` in this release.

JSON runs put runtime progress on stderr and one result document on stdout.
Results separate stored process/work/outcome, review and its independence policy,
and verification. Flow verification is `unavailable`; a review is not an
acceptance receipt. Role runs include their existing inline acceptance receipts. Missing or
disagreeing receipt evidence remains unknown; the recorded execution outcome
stays separate. This is historical evidence, without a current workspace probe.
Stored summaries may be clipped by the existing session owner; validated plan
artifacts are shown in full after redaction. Artifact validation describes
historical bytes, without probing the current workspace or active process.
Use a full session ID, or a project session name with `--workspace DIR`.
Coverage concerns the selected session only. Missing or damaged records and a
limited page remain incomplete and offer inspection rather than an all-clear.
An implement-plan command is offered only after the existing handoff owner
validates the producer; invoking it remains an explicit user action.

`garuda starter example reconnect DIR` is registered; materialization arrives
with the release/examples follow-up (#309). Until then it returns a typed
unavailable error without creating files.
