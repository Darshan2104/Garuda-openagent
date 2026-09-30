# Using Garuda

Garuda runs a task through an execution runtime, inside a selected workspace,
with an agent profile, permission posture, tools, completion policy, and durable
session record. The native runtime owns Garuda's model-and-tool loop. ACP
runtimes launch external coding harnesses under a different verification and
authority model.

Start with the [first-run guide](getting-started.md) if you have not yet created
a session.

## Choose an interface

| Interface | Best for | Start with |
|---|---|---|
| Headless CLI | One task with a process exit status | `garuda run -t TASK` |
| Interactive CLI | A conversation with terminal permission prompts | `garuda chat` |
| Web dashboard | Local session inspection, approvals, and conversations | `garuda web` |
| Recipe runner | Repeatable YAML workflows with parameters | `garuda recipe run workflow.yaml` |
| Python SDK | Embedding the native agent in an application | `SoftwareAgent` |
| JSON-RPC service | Authenticated local job submission and polling | `garuda serve` |

Use `garuda run --json` when another program needs JSONL events instead of the
human-readable result. Use `--trajectory run.jsonl` to write an evaluation
trajectory for a native run.

## Choose a run mode

Run mode selects a coherent completion and permission posture. It is separate
from the model and workspace backend.

| Mode | Purpose | Additional model work |
|---|---|---|
| `interactive` | Normal task execution with local structural checks | None required by the mode |
| `readonly` | Inspection with writes denied and shell reads screened | Same completion posture as interactive |
| `eval` | Benchmark-grade acceptance, judging, evidence, and re-verification | Roughly two calls per completion attempt, plus checks |
| `rigorous` | Eval gates plus plan, execution, critique, and repair | More than eval; use deliberately |

```bash
garuda run --mode readonly -t "Map the authentication flow"
garuda run --mode rigorous -t "Fix the failing tests and prove the result"
```

`standard` remains a compatibility alias for `interactive`. An explicit
`--permission-mode` can further select `readonly`, `smart`, `auto`, or `yolo`.
Permission rules screen requested operations; they are not a filesystem or
network sandbox. Project hooks and Python tools execute code and remain opt-in.
`yolo` is deliberately permissive and belongs only in an isolated, trusted
workspace. See [Configuration](configuration.md#permissions-and-hooks).

## Workspaces

Select the project with `--workspace` and its execution backend with
`--workspace-kind`:

| Kind | Use | Boundary |
|---|---|---|
| `local` | Work directly in the selected directory | Host process; permission guardrails only |
| `sandbox` | Reduce write and network blast radius with the OS backend | Not general host-read confinement |
| `tmux` | Keep terminal work visible in a tmux session | Host process; not confinement |
| `docker` | Run against a local Docker container | Isolation boundary for untrusted work |
| `remote` | Use a configured remote Docker daemon | Docker boundary on the remote host |

```bash
garuda run --workspace /path/to/project --workspace-kind docker --no-network -t "Run the test suite"
```

Docker defaults to bridged networking unless `--no-network` is set. The
`sandbox` kind denies network egress unless `--allow-network` is passed to
`garuda run`. If the requested OS sandbox is unavailable, Garuda refuses by
default; `--allow-unsandboxed` explicitly permits an unconfined fallback and
must not be used as a safety boundary. See the
[CLI reference](../reference/cli.md#common-run-flags) for container resource
and remote-host flags.

## Sessions and continuation

Sessions persist task metadata, messages, events, runtime ownership, and
workspace evidence. List them with:

```bash
garuda sessions --limit 10
```

Resume a native session using its full ID, unique prefix, or `latest`:

```bash
garuda run --workspace . --resume latest -t "Continue from the previous findings"
```

Garuda records a new session linked to the previous one rather than overwriting
the old record. Runtime commands use the same session-reference forms. A session
owned by an external runtime must be recovered or reclaimed according to the
[external harness guide](external-harnesses.md); native resume refuses while
external ownership is still active.

## Profiles, project instructions, and skills

Built-in profiles are `build`, `plan`, `explore`, `reviewer`, and `harbor`:

```bash
garuda run --agent reviewer --mode readonly -t "Review the current changes"
```

Add project profiles under `.agent/agents/` as YAML or `agent.md` files. Garuda
also reads root `AGENTS.md` or `GARUDA.md` as project instructions. Skills use
the portable `.agent/skills/<name>/SKILL.md` layout.

The native agent can call `invoke_subagent` when its profile exposes that tool.
Child context handoff can be `none`, `brief`, or `full`; `brief` carries bounded
working state and references instead of copying the full parent transcript.

## MCP, recipes, hooks, and project tools

Inspect MCP configuration without opening server connections first:

```bash
garuda mcp list --no-connect
```

Then use `garuda mcp list` to connect and enumerate tools. Garuda merges global
and project MCP configuration by default, with project entries winning name
collisions. Remote tools are permission-screened, but a declared
read-only effect is a trusted assertion rather than proof about the server.

Recipes run named YAML steps and accept repeated parameters:

```bash
garuda recipe run workflow.yaml --param target=tests
```

Project hooks and `.agent/tools/*.py` can execute repository-controlled code.
A cloned repository cannot authorize them by itself; trust must come from
global settings or an explicit per-run choice. Review that code before enabling
it.

## Native and external runtimes

The default runtime is `native`. It uses the configured reasoning model, native
tools, permission engine, and completion gates described above.

```bash
garuda runtime list
garuda run --runtime opencode -t "Inspect the failing tests"
```

An ACP runtime launches a separately installed, user-authenticated harness.
Garuda keeps the workspace lease, session trail, approvals, baseline, and
workspace delta, but the external harness owns its edits and commands. A
completed ACP turn is not a Garuda-verified result. Installation, routing,
handoff, recovery, and support workflows are covered in
[External harnesses](external-harnesses.md).

## Evidence, trajectories, and evaluation

Native completion is not accepted solely because a model says it is done. The
selected mode decides which structural, acceptance, judge, evidence, stability,
and side-effect gates apply. Event logs remain with the session, while
`--trajectory` writes the run's raw event stream as JSONL. Harbor evaluation
converts recorded events to ATIF-v1.7 trajectories.

Dual-model collection is optional and disabled unless trusted configuration
enables and binds it. The reasoning model remains the controller; collection
workers are bounded, read-only investigators and cannot complete the parent
task. See [Configuration](configuration.md#bounded-collection-model) and
[Evaluation](../evaluation/index.md) rather than enabling a second model merely
for routine work.

## SDK and service

Embed a one-shot native task with the asynchronous SDK:

```python
import asyncio

from garuda.sdk import SoftwareAgent


async def main():
    agent = SoftwareAgent(workspace=".", agent="build")
    result = await agent.run("Fix the failing test")
    print(result.final_message)


asyncio.run(main())
```

Run `garuda serve` for the authenticated JSON-RPC job queue. A bearer token is
required; set `GARUDA_SERVE_TOKEN` or use the token Garuda generates for a
loopback server. Use `garuda web` for the local browser dashboard. See the
[web dashboard guide](web-dashboard.md) and [CLI reference](../reference/cli.md).

## Control time and cost

- Start with `interactive` or `readonly`; `eval` and `rigorous` deliberately do
  more model work.
- Bound the loop with `--max-turns` and wall time with `--deadline-sec`.
- Leave the collection role disabled unless parallel investigation is useful.
- Confirm the selected reasoning and collection models printed at startup.
- Use provider billing and quota data as the authority; Garuda does not invent
  missing provider or external-harness cost information.
