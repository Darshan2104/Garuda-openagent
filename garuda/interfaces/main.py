from pathlib import Path

from garuda.agents.loader import load_profile, resolve_system_prompt
from garuda.core.events import EventStore
from garuda.core.modes import MODE_CHOICES, apply_mode_preset
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.interfaces.cli import chat_loop
from garuda.interfaces.runner import (
    cleanup_workspace,
    resolve_environment,
    run_agent_task,
)
from garuda.interfaces.web.live import DEFAULT_MAX_PERMISSION as WEB_DEFAULT_MAX_PERMISSION
from garuda.interfaces.web.security import DEFAULT_PORT as WEB_DEFAULT_PORT
from garuda.mcp.config import resolve_mcp_config_paths
from garuda.model.litellm_model import LitellmModel
from garuda.model.protocol import DEFAULT_MODEL, MODEL_ENV_VAR
from garuda.tools import build_toolkit
from garuda.workspace.factory import WORKSPACE_KINDS


def build_parser():
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="garuda", description="Garuda Open Agent harness")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a single agent task (headless)")
    run_parser.add_argument("-t", "--task", help="Task description")
    run_parser.add_argument("-f", "--file", help="Read task from file")
    run_parser.add_argument("--model", default=os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL))
    run_parser.add_argument("--workspace", default=".", help="Workspace root directory")
    run_parser.add_argument(
        "--workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
        help="Execution environment type",
    )
    run_parser.add_argument("--docker-image", default="ubuntu:22.04")
    run_parser.add_argument("--docker-host", help="Remote Docker daemon host (DOCKER_HOST)")
    run_parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow network egress inside the OS sandbox (sandbox kind denies it by default)",
    )
    run_parser.add_argument(
        "--no-network",
        action="store_true",
        help="Disable network egress for docker/remote containers (default: bridged)",
    )
    run_parser.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="Run --workspace-kind sandbox unconfined if no OS sandbox backend is available "
        "(default: fail loudly)",
    )
    run_parser.add_argument("--docker-memory", default="2g", help="Container memory limit (e.g. 2g)")
    run_parser.add_argument("--docker-cpus", default="2", help="Container CPU limit (e.g. 2)")
    run_parser.add_argument("--agent", default="build", help="Agent profile name")
    run_parser.add_argument(
        "--runtime", default="native", help="Trusted global runtime id or project alias"
    )
    run_parser.add_argument("--agents-dir", help="Directory with custom agent YAML profiles")
    run_parser.add_argument(
        "--mcp-config",
        help="Path to MCP servers config (YAML or JSON); auto-discovered from "
        ".agent/mcp.json|yaml, .garuda/mcp.json|yaml or .cursor/mcp.json when omitted",
    )
    run_parser.add_argument(
        "--load-project-tools",
        action="store_true",
        default=None,
        help="Import custom tools from .agent/tools/*.py (runs repo code; "
        "overrides the load_project_tools setting)",
    )
    run_parser.add_argument("--permission-mode", choices=["auto", "smart", "readonly", "yolo"])
    run_parser.add_argument(
        "--mode",
        choices=list(MODE_CHOICES),
        default=None,
        help=(
            "Run posture, which picks a coherent set of completion gates. "
            "interactive (default): no gates that cost a model call. "
            "eval: full gate stack — LLM judge, acceptance contract, "
            "discriminating + stable evidence (~2 extra model calls per completion "
            "attempt); this is the benchmark configuration. "
            "rigorous: eval gates plus a plan/execute/critic agent. "
            "readonly: interactive gates with permissions forced read-only. "
            "standard is an alias for interactive. Omit to honor the profile's own."
        ),
    )
    run_parser.add_argument("--max-turns", type=int)
    run_parser.add_argument(
        "--deadline-sec",
        type=float,
        default=None,
        help=(
            "Wall-clock budget for the run. The agent paces itself against it and "
            "reserves turns to finish, which a turn count cannot express when one "
            "command may block for minutes."
        ),
    )
    run_parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high"],
        help="Enable extended thinking at this effort (cross-provider)",
    )
    run_parser.add_argument(
        "--thinking-budget",
        type=int,
        help="Anthropic extended-thinking budget in tokens (enables thinking)",
    )
    run_parser.add_argument(
        "--persistent-shell",
        action="store_true",
        help="Keep one shell alive across bash calls so cwd/env/venv persist (local env)",
    )
    run_parser.add_argument(
        "--no-post-edit-diagnostics",
        action="store_true",
        help="Disable the syntax check run after edit/write_file",
    )
    run_parser.add_argument(
        "--no-post-edit-lint",
        action="store_true",
        help="Disable the fast semantic lint (Python/ruff) run after edit/write_file",
    )
    run_parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Skip the session-start environment probe (cold start; the agent "
        "discovers the environment itself)",
    )
    run_parser.add_argument("--no-verifier", action="store_true")
    run_parser.add_argument("--no-three-step-summary", action="store_true")
    run_parser.add_argument("--json", action="store_true", help="Print JSONL events to stdout")
    run_parser.add_argument("--trajectory", help="Save event trajectory to JSONL file")
    run_parser.add_argument(
        "--resume",
        metavar="ID",
        help="Resume a saved session (full id, unique prefix, or 'latest')",
    )

    chat_parser = subparsers.add_parser("chat", help="Interactive agent session with permission prompts")
    chat_parser.add_argument("--model", default=os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL))
    chat_parser.add_argument("--workspace", default=".")
    chat_parser.add_argument(
        "--workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
    )
    chat_parser.add_argument("--docker-image", default="ubuntu:22.04")
    chat_parser.add_argument("--docker-host")
    chat_parser.add_argument("--agent", default="build")
    chat_parser.add_argument("--agents-dir")
    chat_parser.add_argument("--mcp-config")
    chat_parser.add_argument(
        "--mode",
        choices=list(MODE_CHOICES),
        default=None,
        help="Run posture (see `garuda run --help`); defaults to interactive.",
    )
    # No default, so an unset flag stays None and the profile/preset decides. Chat
    # is the interface built around permission prompts, so not being able to pick
    # the mode here was the one place the flag was most obviously missing.
    chat_parser.add_argument(
        "--permission-mode",
        choices=["auto", "smart", "readonly", "yolo"],
        default=None,
        help="Permission posture; overrides the profile and --mode.",
    )
    chat_parser.add_argument("--json", action="store_true")

    serve_parser = subparsers.add_parser("serve", help="Start JSON-RPC HTTP server for IDE integrations")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--model", default=os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL))
    serve_parser.add_argument("--agent", default="build")
    serve_parser.add_argument("--workspace", default=".")
    serve_parser.add_argument(
        "--workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
    )
    serve_parser.add_argument("--docker-image", default="ubuntu:22.04")
    serve_parser.add_argument("--docker-host")
    serve_parser.add_argument("--agents-dir")
    serve_parser.add_argument("--mcp-config")
    serve_parser.add_argument(
        "--token",
        default=os.environ.get("GARUDA_SERVE_TOKEN"),
        help="Bearer token required on requests (or set GARUDA_SERVE_TOKEN)",
    )
    serve_parser.add_argument(
        "--max-jobs",
        type=int,
        default=4,
        help="Max concurrent jobs for submit/status/result (default 4)",
    )
    serve_parser.add_argument(
        "--model-max-concurrency",
        type=int,
        default=0,
        help="Cap concurrent model calls per provider across jobs (0 = unlimited)",
    )

    # `web`, aliased `dashboard`: the name people reach for, while the module stays
    # `interfaces/web` so it does not collide with `garuda.eval.dashboard` (the
    # markdown cost table). Both spellings must be accepted in the dispatch below.
    web_parser = subparsers.add_parser(
        "web",
        aliases=["dashboard"],
        help="Serve the local web dashboard: read past runs and talk to an agent",
    )
    # No --host: the bind address is 127.0.0.1 and not configurable. See
    # interfaces/web/http.py::BIND_HOST for why.
    web_parser.add_argument("--port", type=int, default=WEB_DEFAULT_PORT)
    web_parser.add_argument(
        "--sessions-dir", help="Read runs from here instead of the configured sessions root"
    )
    web_parser.add_argument("--agents-dir", help="Directory with custom agent YAML profiles")
    web_parser.add_argument(
        "--read-only",
        action="store_true",
        help="Serve the run history only. Without this the dashboard can also talk to an "
        "agent, which runs tools as you — bounded by --max-permission, which defaults to "
        "asking before anything destructive.",
    )
    # Accepted and ignored: talking to an agent is the default now. Kept so an alias or a
    # shell history entry carrying it does not fail, rather than out of any real ambiguity.
    web_parser.add_argument("--allow-run", action="store_true", help=argparse.SUPPRESS)
    web_parser.add_argument(
        "--allow-workspace",
        action="append",
        default=[],
        metavar="DIR",
        help="A directory the agent may work in; repeatable. "
        "Defaults to the current directory. Requests name these by index, never by path.",
    )
    web_parser.add_argument(
        "--max-permission",
        choices=["readonly", "smart", "auto", "yolo"],
        default=WEB_DEFAULT_MAX_PERMISSION,
        help="The loosest permission posture a browser request may ask for "
        f"(default: {WEB_DEFAULT_MAX_PERMISSION}). A request may ask for this or stricter.",
    )
    web_parser.add_argument(
        "--web-model",
        default=os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL),
        help="Default model for dashboard conversations",
    )
    web_parser.add_argument(
        "--web-agent", default="build", help="Default agent profile for dashboard conversations"
    )
    web_parser.add_argument(
        "--web-workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
        help="Workspace kind for dashboard conversations (default: local)",
    )
    web_parser.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser automatically"
    )

    sessions_parser = subparsers.add_parser("sessions", help="List recent saved sessions")
    sessions_parser.add_argument("--limit", type=int, default=20)

    mcp_parser = subparsers.add_parser("mcp", help="Inspect MCP server configuration")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")
    mcp_list = mcp_sub.add_parser(
        "list", help="Show resolved MCP config path(s) and the tools each server exposes"
    )
    mcp_list.add_argument("--workspace", default=".")
    mcp_list.add_argument(
        "--mcp-config", help="Explicit config path (skips auto-discovery/merge)"
    )
    mcp_list.add_argument(
        "--no-connect",
        action="store_true",
        help="Only show configured servers; do not connect to enumerate tools",
    )

    recipe_parser = subparsers.add_parser("recipe", help="Run YAML workflow recipes")
    recipe_sub = recipe_parser.add_subparsers(dest="recipe_command")
    recipe_run = recipe_sub.add_parser("run", help="Execute a recipe file")
    recipe_run.add_argument("recipe", help="Path to recipe YAML")
    recipe_run.add_argument("--model", default=os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL))
    recipe_run.add_argument("--workspace", default=".")
    recipe_run.add_argument(
        "--workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
    )
    recipe_run.add_argument("--docker-image", default="ubuntu:22.04")
    recipe_run.add_argument("--docker-host")
    recipe_run.add_argument("--agents-dir")
    recipe_run.add_argument("--mcp-config")
    recipe_run.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Recipe parameter (repeatable)",
    )

    runtime_parser = subparsers.add_parser("runtime", help="Select and inspect runtimes")
    runtime_sub = runtime_parser.add_subparsers(dest="runtime_command")
    runtime_list = runtime_sub.add_parser("list", help="List configured runtimes")
    runtime_list.add_argument("--json", action="store_true", help="Print JSON health records")
    runtime_list.add_argument(
        "--workspace", default=".", help="Workspace whose project runtime refs apply"
    )
    runtime_inspect = runtime_sub.add_parser("inspect", help="Inspect one runtime")
    runtime_inspect.add_argument("runtime_id", help="Runtime id or alias")
    runtime_inspect.add_argument("--json", action="store_true", help="Print JSON health record")
    runtime_inspect.add_argument(
        "--workspace", default=".", help="Workspace whose project runtime refs apply"
    )
    runtime_handoff = runtime_sub.add_parser("handoff", help="Preview or execute a handoff")
    runtime_handoff.add_argument("--session", required=True, help="Source session id")
    runtime_handoff.add_argument("--to", required=True, help="Target runtime id")
    runtime_handoff.add_argument(
        "--workspace",
        default=None,
        help="Workspace root (default: the session's recorded workspace)",
    )
    runtime_handoff.add_argument(
        "--confirm",
        action="store_true",
        help="Execute the handoff transaction. Without it, only a preview prints.",
    )
    runtime_resume = runtime_sub.add_parser("resume", help="Resume a persisted session")
    runtime_resume.add_argument("--session", required=True, help="Session id to resume")
    runtime_resume.add_argument("-t", "--task", required=True, help="Continuation task")
    runtime_resume.add_argument("--workspace", default=".", help="Workspace root")
    runtime_resume.add_argument("--agent", default="build", help="Agent profile")
    runtime_resume.add_argument("--model", default=os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL))
    runtime_recover = runtime_sub.add_parser("recover", help="Classify and recover a session")
    runtime_recover.add_argument("--session", required=True, help="Session id")
    runtime_recover.add_argument("--json", action="store_true", help="Print JSON report")
    runtime_reclaim = runtime_sub.add_parser(
        "reclaim", help="Return a handed-off session whose target stopped to native"
    )
    runtime_reclaim.add_argument("--session", required=True, help="Session id")
    runtime_support = runtime_sub.add_parser("support", help="Print a redacted support bundle")
    runtime_support.add_argument("--session", required=True, help="Session id")

    return parser


def _parse_params(pairs: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"Invalid --param (expected KEY=VALUE): {item}")
        key, value = item.split("=", 1)
        params[key.strip()] = value.strip()
    return params


def run_sessions(args) -> int:
    from garuda.core.sessions import SessionStore

    sessions = SessionStore().list_sessions(limit=args.limit)
    if not sessions:
        print("No saved sessions.")
        return 0
    print(f"{'ID':<10} {'STATUS':<8} {'TURNS':>5}  {'UPDATED':<32} TASK")
    for meta in sessions:
        task = " ".join((meta.get("task") or "").split())
        if len(task) > 60:
            task = task[:57] + "..."
        print(
            f"{meta.get('session_id', '')[:8]:<10} "
            f"{meta.get('status', '?'):<8} "
            f"{meta.get('turns', 0):>5}  "
            f"{meta.get('updated_at', ''):<32} "
            f"{task}"
        )
    return 0


async def run_mcp_list(args) -> int:
    """Show which MCP config file(s) resolve and the tools each server exposes."""
    from garuda.mcp.config import load_and_merge_mcp_configs, resolve_mcp_config_paths

    paths = resolve_mcp_config_paths(args.workspace, args.mcp_config)
    if not paths:
        print(
            "No MCP config found (looked for .agent/mcp.json|yaml, .garuda/mcp.json|yaml, "
            ".cursor/mcp.json, and the global ~/.agent/mcp.json)."
        )
        return 0

    print("Resolved MCP config path(s):")
    for path in paths:
        print(f"  {path}")

    servers = load_and_merge_mcp_configs(paths)
    if not servers:
        print("\nNo servers defined in the resolved config.")
        return 0

    print(f"\n{len(servers)} server(s) configured:")
    for server in servers:
        target = server.url or f"{server.command} {' '.join(server.args)}".strip()
        print(f"  - {server.name} [{server.transport}] {target}")

    if args.no_connect:
        return 0

    from garuda.tools import build_toolkit

    print("\nConnecting to enumerate tools...")
    tools, manager = await build_toolkit([], paths)
    try:
        mcp_tools = [t for t in tools if t.name.startswith("mcp__")]
        if not mcp_tools:
            print("  (no tools registered — servers may have failed to start; check logs)")
        for tool in mcp_tools:
            print(f"  {tool.name}")
    finally:
        if manager is not None:
            await manager.close()
    return 0


def _configured_catalog(workspace: str = "."):
    """The single trusted runtime catalog for CLI runtime commands."""
    from garuda.interfaces.runtime_cli import configured_catalog

    return configured_catalog(workspace)


def _print_runtime_refusal(exc: Exception) -> int:
    import sys

    print(f"Error: runtime selection refused: {exc}", file=sys.stderr)
    print(
        "Check `runtimes`/`disabled_runtimes` in your global settings and the "
        "project's `runtime_refs`, or pass `--runtime native`.",
        file=sys.stderr,
    )
    return 2


async def run_runtime_command(args) -> int:
    """Dispatch `garuda runtime ...`. Preview paths never mutate."""
    from garuda.acp.catalog import RuntimeSettingsError
    from garuda.runtime.registry import RegistryError

    try:
        return await _run_runtime_command(args)
    except (RegistryError, RuntimeSettingsError) as exc:
        return _print_runtime_refusal(exc)


async def _run_runtime_command(args) -> int:
    from garuda.context.pack import ContextPackManager
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runtime_cli import (
        cmd_handoff_confirm,
        cmd_handoff_preview,
        cmd_inspect_registry,
        cmd_list_registry,
        cmd_reclaim,
        cmd_recover,
    )
    from garuda.runtime.registry import RegistryError

    command = args.runtime_command
    if command == "list":
        catalog = _configured_catalog(args.workspace)
        print(
            cmd_list_registry(
                catalog.registry, as_json=args.json, project_disabled=catalog.project_disabled
            ),
            end="",
        )
        return 0
    if command == "inspect":
        catalog = _configured_catalog(args.workspace)
        try:
            print(
                cmd_inspect_registry(
                    catalog.registry,
                    args.runtime_id,
                    as_json=args.json,
                    project_disabled=catalog.project_disabled,
                ),
                end="",
            )
        except KeyError as exc:
            print(f"Error: {exc}")
            return 2
        return 0
    store = SessionStore()
    if command == "handoff":
        if not args.confirm:
            print(cmd_handoff_preview(store, args.session, args.to), end="")
            return 0
        from garuda.interfaces.run_guard import interactive_approval

        manager = ContextPackManager(store.session_dir(args.session))
        try:
            print(
                await cmd_handoff_confirm(
                    store,
                    args.session,
                    args.to,
                    workspace=args.workspace,
                    pack_manager=manager,
                    approval=interactive_approval(),
                ),
                end="",
            )
        except RegistryError:
            raise
        except Exception as exc:
            print(f"Error: {exc}")
            return 1
        return 0
    if command == "resume":
        from garuda.agents.setup import prepare_agent_run
        from garuda.interfaces.runtime_cli import cmd_resume
        from garuda.model.litellm_model import LitellmModel

        profile, config, permissions, tools, agent, mcp_manager = await prepare_agent_run(
            args.agent, workspace=args.workspace
        )
        model = LitellmModel(
            model_name=args.model,
            reasoning_effort=config.reasoning_effort,
            thinking_budget_tokens=config.thinking_budget_tokens,
        )
        try:
            print(
                await cmd_resume(
                    store=store,
                    session_id=args.session,
                    task=args.task,
                    model=model,
                    agent=agent,
                    tools=tools,
                    config=config,
                    permissions=permissions,
                    workspace=args.workspace,
                    mcp_manager=mcp_manager,
                ),
                end="",
            )
        except Exception as exc:
            print(f"Error: {exc}")
            return 1
        finally:
            if mcp_manager is not None:
                try:
                    await mcp_manager.close()
                except Exception:
                    pass
        return 0
    if command == "reclaim":
        from garuda.runtime.recovery import RecoveryError
        from garuda.workspace.lease import LeaseStore

        try:
            print(cmd_reclaim(store, args.session, leases=LeaseStore()), end="")
        except RecoveryError as exc:
            print(f"Error: reclaim refused: {exc}")
            return 1
        return 0
    if command == "recover":
        from garuda.runtime.recovery import RecoveryError

        try:
            print(cmd_recover(store, args.session, as_json=args.json), end="")
        except RecoveryError as exc:
            print(f"Error: recovery refused: {exc}")
            return 1
        return 0
    if command == "support":
        from garuda.interfaces.runtime_cli import cmd_support_bundle

        try:
            print(cmd_support_bundle(store, args.session), end="")
        except ValueError as exc:
            print(f"Error: {exc}")
            return 2
        return 0
    build_parser().parse_args(["runtime", "--help"])
    return 1


async def run_acp_command(args, task: str, catalog) -> int:
    """Run one task on a named ACP runtime via loud, explicit selection."""
    from garuda.interfaces.run_guard import interactive_approval
    from garuda.interfaces.runtime_cli import run_acp_task

    try:
        summary = await run_acp_task(
            task,
            runtime_id=args.runtime,
            workspace=args.workspace,
            catalog=catalog,
            approval=interactive_approval(),
        )
    except Exception as exc:
        print(f"Error: {exc}")
        return 1
    print(
        f"session {summary['session_id']}: {summary['status']} "
        f"(turn {summary['turn']}, {summary['events']} normalized events; "
        "the ACP result is not verified by Garuda)"
    )
    return 0 if summary["status"] == "completed" else 1


async def run_task(args) -> int:
    import sys

    task = args.task
    if args.file:
        task = Path(args.file).read_text(encoding="utf-8")
    if not task:
        print("Error: provide -t/--task or -f/--file", file=sys.stderr)
        return 1

    # Resolve policy before constructing a model, toolkit, workspace, or
    # provider adapter. The selected id is then handed to the matching
    # executor below; the native default is not an implicit bypass.
    from garuda.agents.setup import select_runtime

    args.runtime = select_runtime(args.workspace, args.runtime)
    if getattr(args, "runtime", "native") != "native":
        # Resolved before constructing any model, tools, or workspace state,
        # preserving the fail-closed disabled/alias policy on `garuda run`.
        # An ACP selection launches through the ACP path with the same
        # catalog; an alias of the native loop continues below. A refused
        # selection raises `RegistryError`, which `main()` reports as a
        # message with exit status 2 (`_run_with_runtime_gate`).
        from garuda.runtime.protocol import RuntimeKind

        catalog = _configured_catalog(args.workspace)
        selected = catalog.registry.get(args.runtime)
        if selected.kind is RuntimeKind.ACP:
            return await run_acp_command(args, task, catalog)

    # Native selection still goes through the common trusted boundary before
    # any toolkit or workspace startup. ACP selection is resolved by
    # ``run_acp_command`` through the same trusted registry service.
    from garuda.agents.setup import prepare_runtime_catalog

    runtime_catalog = prepare_runtime_catalog(args.workspace)
    runtime_catalog.select_for_native_facade(args.runtime)

    from garuda.config.agent_home import resolve_agents_dirs

    agents_dir = resolve_agents_dirs(args.workspace, args.agents_dir)
    profile = load_profile(args.agent, extra_dir=agents_dir)
    config = profile.to_agent_config()
    if args.mode:  # else keep the profile's own mode
        config.mode = args.mode
    # Preset first, explicit flags after: every `if args.x` below is a narrower
    # statement of intent than the posture and must win over it.
    apply_mode_preset(config, declared_fields=profile.declared_fields)
    if args.max_turns is not None:
        config.max_turns = args.max_turns
    if getattr(args, "deadline_sec", None) is not None:
        config.deadline_sec = args.deadline_sec
    if args.permission_mode:
        config.permission_mode = args.permission_mode
    if args.no_verifier:
        config.enable_verifier = False
    if args.no_three_step_summary:
        config.enable_three_step_summary = False
    if getattr(args, "reasoning_effort", None):
        config.reasoning_effort = args.reasoning_effort
    if getattr(args, "thinking_budget", None):
        config.thinking_budget_tokens = args.thinking_budget
    if getattr(args, "persistent_shell", False):
        config.persistent_shell = True
    if getattr(args, "no_post_edit_diagnostics", False):
        config.post_edit_diagnostics = False
    if getattr(args, "no_post_edit_lint", False):
        config.post_edit_lint = False
    if getattr(args, "no_bootstrap", False):
        config.bootstrap_environment = False
    config.workspace_kind = args.workspace_kind
    config.docker_image = args.docker_image
    config.docker_host = args.docker_host
    config.sandbox_allow_network = getattr(args, "allow_network", False)
    config.sandbox_require = not getattr(args, "allow_unsandboxed", False)
    config.docker_network = "none" if getattr(args, "no_network", False) else "bridge"
    config.docker_memory = getattr(args, "docker_memory", "2g")
    config.docker_cpus = getattr(args, "docker_cpus", "2")
    config.system_prompt = resolve_system_prompt(profile, args.workspace)
    mcp_paths = resolve_mcp_config_paths(args.workspace, args.mcp_config or config.mcp_config_path)

    model = LitellmModel(
        model_name=args.model,
        reasoning_effort=config.reasoning_effort,
        thinking_budget_tokens=config.thinking_budget_tokens,
    )
    permissions = PermissionEngine(
        mode=config.permission_mode,
        tool_rules=profile.tool_rules,
        path_rules=profile.path_rules,
        bash_rules=profile.bash_rules,
    )
    agent = create_agent(profile.name, mode=config.mode)
    events = EventStore()
    tools, mcp_manager = await build_toolkit(
        profile.tools,
        mcp_paths,
        workspace=args.workspace,
        load_project_tools=getattr(args, "load_project_tools", None),
        mcp_servers=profile.mcp_servers,
    )

    result = await run_agent_task(
        task=task,
        model=model,
        agent=agent,
        tools=tools,
        config=config,
        permissions=permissions,
        workspace=args.workspace,
        events=events,
        emit_json=args.json,
        workspace_kind=args.workspace_kind,
        docker_image=args.docker_image,
        docker_host=args.docker_host,
        mcp_manager=mcp_manager,
        agents_dir=agents_dir,
        resume=args.resume,
        runtime_catalog=runtime_catalog,
        runtime_ref=args.runtime,
    )

    if args.trajectory:
        events.save(args.trajectory)
    if not args.json:
        print(result.final_message)
    return 0 if result.success else 1


async def run_recipe_command(args) -> int:
    import sys

    from garuda.config.agent_home import resolve_agents_dirs
    from garuda.config.recipes import load_recipe, run_recipe

    try:
        params = _parse_params(args.param)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    recipe = load_recipe(args.recipe)
    model = LitellmModel(model_name=args.model)
    env, handle = await resolve_environment(
        args.workspace_kind,
        args.workspace,
        args.docker_image,
        docker_host=args.docker_host,
    )
    events = EventStore()
    try:
        results = await run_recipe(
            recipe,
            params,
            model=model,
            env=env,
            workspace=args.workspace,
            events=events,
            agents_dir=resolve_agents_dirs(args.workspace, args.agents_dir),
            mcp_config_path=args.mcp_config,
        )
    finally:
        await cleanup_workspace(handle)

    for index, result in enumerate(results, start=1):
        print(f"--- Step {index} ({'ok' if result.success else 'failed'}) ---")
        print(result.final_message)
    # `run_recipe` stops at the first failed step. Say so, and name what did not run:
    # otherwise the output ends on a failed step and a 3-step recipe that stopped at
    # step 1 looks identical to a 1-step recipe that failed.
    skipped = len(recipe.steps) - len(results)
    if skipped > 0:
        print(
            f"--- Recipe stopped at step {len(results)}; {skipped} later step(s) "
            f"were not run ---",
            file=sys.stderr,
        )
    return 0 if results and results[-1].success else 1


async def run_serve(args) -> int:
    from garuda.interfaces.server import ServerConfig, serve

    config = ServerConfig(
        host=args.host,
        port=args.port,
        model=args.model,
        agent=args.agent,
        workspace=args.workspace,
        workspace_kind=args.workspace_kind,
        docker_image=args.docker_image,
        docker_host=args.docker_host,
        agents_dir=args.agents_dir,
        mcp_config=args.mcp_config,
        token=args.token,
        max_jobs=getattr(args, "max_jobs", 4),
        model_max_concurrency=getattr(args, "model_max_concurrency", 0),
    )
    await serve(config)
    return 0


async def run_web(args) -> int:
    """Serve the dashboard until interrupted."""
    from garuda.interfaces.web import DashboardConfig, serve_dashboard

    config = DashboardConfig(
        port=args.port,
        sessions_dir=Path(args.sessions_dir) if args.sessions_dir else None,
        agents_dir=Path(args.agents_dir) if args.agents_dir else None,
        allow_run=not args.read_only,
        workspaces=tuple(Path(p) for p in (args.allow_workspace or [])),
        max_permission=args.max_permission,
        model=args.web_model,
        agent=args.web_agent,
        workspace_kind=args.web_workspace_kind,
        open_browser=not args.no_browser,
    )
    try:
        await serve_dashboard(config)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    return 0


def _run_with_runtime_gate(args) -> int:
    """`garuda run`, with a refused runtime selection as a message, not a traceback."""
    import asyncio

    from garuda.acp.catalog import RuntimeSettingsError
    from garuda.runtime.registry import RegistryError

    try:
        return asyncio.run(run_task(args))
    except (RegistryError, RuntimeSettingsError) as exc:
        return _print_runtime_refusal(exc)


def main() -> None:
    import asyncio

    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        raise SystemExit(_run_with_runtime_gate(args))
    if args.command == "chat":
        raise SystemExit(asyncio.run(chat_loop(args)))
    if args.command == "serve":
        raise SystemExit(asyncio.run(run_serve(args)))
    if args.command in ("web", "dashboard"):
        raise SystemExit(asyncio.run(run_web(args)))
    if args.command == "runtime":
        raise SystemExit(asyncio.run(run_runtime_command(args)))
    if args.command == "sessions":
        raise SystemExit(run_sessions(args))
    if args.command == "mcp":
        if args.mcp_command == "list":
            raise SystemExit(asyncio.run(run_mcp_list(args)))
        parser.parse_args(["mcp", "--help"])
        raise SystemExit(1)
    if args.command == "recipe" and args.recipe_command == "run":
        raise SystemExit(asyncio.run(run_recipe_command(args)))
    parser.print_help()
    raise SystemExit(1)


if __name__ == "__main__":
    main()
