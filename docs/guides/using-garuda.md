# Using Garuda

## Run modes

| Mode | Purpose |
|---|---|
| `interactive` | Default, low-overhead task execution with structural completion evidence. |
| `eval` | Acceptance contract, LLM judge, discriminating evidence, stable re-verification, and side-effect sweep. |
| `rigorous` | `eval` plus plan, execution, critique, and repair rounds. |
| `readonly` | Inspection posture with write operations denied and shell reads screened. |

```bash
garuda run -t "Fix the failing tests" --mode rigorous
garuda run -t "Map the authentication flow" --mode readonly
```

## Sessions

Every run persists a session under `~/.agent/sessions/` unless overridden by `GARUDA_SESSIONS_DIR`.

```bash
garuda sessions
garuda run -t "Continue the investigation" --resume latest
```

## Workspaces

Use `--workspace-kind` to choose `local`, `sandbox`, `tmux`, `docker`, or `remote`. Docker is the appropriate isolation boundary for untrusted work; macOS Seatbelt reduces write/network blast radius but does not confine host reads.

## Profiles, skills, and subagents

Use built-in profiles `build`, `plan`, `explore`, `reviewer`, and `harbor`, or add YAML/`agent.md` profiles in `.agent/agents/`. Skills use the portable `SKILL.md` layout in `.agent/skills/`. The `invoke_subagent` tool runs a child with `none`, `brief`, or `full` context handoff.

## SDK and service

```python
from garuda.sdk import SoftwareAgent

agent = SoftwareAgent(workspace=".", agent="build")
result = await agent.run("Fix the failing test")
```

Run `garuda serve` for the JSON-RPC job queue or `garuda web` for the local dashboard. See the [CLI reference](../reference/cli.md) and [web dashboard guide](web-dashboard.md).
