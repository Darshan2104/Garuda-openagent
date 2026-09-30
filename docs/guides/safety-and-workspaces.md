# Safety and workspaces

Garuda combines several controls, but they do not provide the same guarantee.
Choose the workspace boundary first, then choose run and permission modes for
the behavior you want inside that boundary.

## Start with the trust decision

| Situation | Recommended posture |
|---|---|
| Inspect a repository you trust | `--mode readonly` in a local workspace |
| Modify a repository you trust | Local workspace with `smart` permissions and Git |
| Run untrusted repository code | Docker workspace, preferably without network |
| Keep a visible host terminal | tmux workspace; treat it like local execution |
| Use another Docker host | Remote workspace only after verifying the daemon and mount path |
| Launch an external coding harness | Review the ACP authority limits before selecting it |

```bash
garuda run --workspace /path/to/project --mode readonly -t "Map the project without changing it"
```

Model access is a separate trust decision. Task text and tool results can reach
the selected provider even when workspace writes and command networking are
disabled.

## Controls and guarantees

| Control | What it does | What it does not do |
|---|---|---|
| Docker workspace | Runs workspace commands in a resource-limited container | It does not remove provider traffic from the host control plane |
| Remote workspace | Runs commands in a container through another Docker daemon | It does not establish trust in that daemon or provision its mount path |
| `sandbox` workspace | Uses Bubblewrap or macOS Seatbelt to reduce write/network blast radius | It is not general host-read confinement |
| Permission rules | Screen tool names and literal path/command arguments | They cannot see shell expansion or all content returned by a broad read |
| `readonly` mode | Denies write tools and shell commands not classified as side-effect-free | It is a guardrail, not a sandbox |
| Workspace lease | Refuses overlapping session ownership that could corrupt attribution | It does not isolate the process from the host |

Docker is the isolation boundary Garuda documents for untrusted work. Permission
rules and OS sandbox policies remain useful defense in depth, but should not be
described or treated as equivalent containment.

## Local and tmux workspaces

`local` executes tools as the current user in the selected directory. `tmux`
does the same through a visible tmux session. Both can reach anything allowed to
that user unless a permission rule refuses the literal request.

Keep the workspace in version control, inspect existing changes before a run,
and avoid using a broad directory such as a home directory as the workspace.
Garuda records the starting state separately so session work can be
distinguished from pre-existing changes.

```bash
garuda run --workspace /path/to/project --workspace-kind tmux --permission-mode smart -t "Run the tests and fix the failure"
```

## OS sandbox workspace

The `sandbox` kind uses the available OS backend and refuses startup when none
is available:

```bash
garuda run --workspace . --workspace-kind sandbox --mode readonly -t "Inspect this project"
```

Network egress for sandboxed commands is denied by default. Permit it for a
specific run only when required:

```bash
garuda run --workspace . --workspace-kind sandbox --allow-network -t "Fetch dependencies and run the tests"
```

`--allow-unsandboxed` changes an unavailable-backend refusal into unconfined
host execution. It is an availability escape hatch, not a fallback security
boundary. On macOS, Seatbelt restricts writes and network according to policy,
but cannot safely confine general host reads. On Linux, backend availability can
also depend on user-namespace and host security configuration.

## Docker workspace

Use a local Docker workspace for untrusted code:

```bash
garuda run --workspace . --workspace-kind docker --docker-image python:3.12 --no-network -t "Run the test suite"
```

The workspace is mounted at `/workspace` in the container. The default limits
are 2 GiB of memory and 2 CPUs; override them with `--docker-memory` and
`--docker-cpus`. Container networking is bridged by default, and
`--no-network` changes it to Docker's `none` network.

These network flags govern commands inside the workspace environment. They do
not prevent Garuda itself from calling the configured model provider, and they
should not be used as a claim that the entire host process is offline.

## Remote Docker workspace

The `remote` kind sends Docker commands to `--docker-host` or `DOCKER_HOST`:

```bash
garuda run --workspace /srv/project --workspace-kind remote --docker-host ssh://builder.example --no-network -t "Run the test suite"
```

Garuda passes the resolved workspace path as a bind mount to the remote daemon.
That path must exist from the daemon host's point of view; Garuda does not copy
the local workspace to the remote machine. Docker authentication and transport
trust remain the Docker CLI's responsibility. A Docker daemon is a privileged
control surface, so use only a daemon and SSH/TLS configuration you trust.

## Run modes and permission modes

Run mode chooses completion gates. Permission mode decides how tool requests
are screened. The normal order is configuration defaults, the run-mode preset,
explicit profile fields, and finally explicit CLI flags. `readonly` mode forces
read-only permissions over a profile, but a later explicit
`--permission-mode` still wins.

| Permission mode | Behavior |
|---|---|
| `smart` | Applies tool, path, and command rules and asks where configured |
| `readonly` | Allows inspection tools and side-effect-free shell commands; denies writes |
| `auto` | Broadly allows requests without interactive asks |
| `yolo` | Deliberately permissive; use only inside an independently trusted boundary |

Never combine `--mode readonly` with a more permissive explicit permission mode
and still describe the result as read-only.

Permission path checks inspect literal arguments. Shell wildcards, indirection,
and broad commands can return content from paths that were not literally named.
Use a read-only mount, a minimal workspace copy, or a container when that
distinction matters.

## Trusted configuration and executable extensions

A repository may provide profiles, skills, project instructions, and MCP
configuration under `.agent/`, but it may not authorize its own Python tools or
hooks. Enabling either executes repository-controlled code:

```yaml
# ~/.agent/settings.yaml
trust_project_hooks: true
load_project_tools: true
```

Review `.agent/tools/`, hook commands, profile permissions, project MCP server
commands, and root `AGENTS.md` or `GARUDA.md` before enabling a cloned project.
Trusted global settings own runtime launch commands and routing policy; project
settings can reference approved runtime IDs but cannot define executable runtime
commands.

## Dashboard and external runtimes

The dashboard binds loopback, uses a capability token, and selects workspaces
from a server-side allowlist. Start it read-only when you need only history:

```bash
garuda web --read-only
```

For live conversations, repeat `--allow-workspace` for each allowed directory
and use `--max-permission` as the browser's ceiling. Browser requests may choose
that posture or a stricter one, never a looser one.

ACP runtimes are different from native workspace execution. Garuda launches the
user-authenticated harness, records approvals, session state, leases, and
workspace evidence, but does not enforce native tool permissions or completion
verification inside that harness. The ACP process performs its own edits and
commands with its own authority. See [External harnesses](external-harnesses.md)
before using one with untrusted code.

## Pre-run checklist

1. Select the smallest intentional workspace.
2. Check existing Git changes and secrets in that workspace.
3. Decide whether host execution is acceptable; otherwise choose Docker.
4. Decide whether workspace commands need network access.
5. Start with `readonly` or `smart`, not a permissive override.
6. Review project hooks, Python tools, MCP commands, and instructions.
7. Confirm the selected native or ACP runtime and its credential boundary.
8. Inspect the resulting session, workspace delta, and verification status.
