from pathlib import Path

from garuda.agents.loader import load_profile, resolve_system_prompt
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.interfaces.cli import chat_loop
from garuda.interfaces.runner import cleanup_workspace, resolve_environment, run_agent_task
from garuda.mcp.config import resolve_mcp_config_paths
from garuda.model.litellm_model import LitellmModel
from garuda.tools import build_toolkit


def build_parser():
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="garuda", description="Garuda Open Agent harness")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a single agent task (headless)")
    run_parser.add_argument("-t", "--task", help="Task description")
    run_parser.add_argument("-f", "--file", help="Read task from file")
    run_parser.add_argument("--model", default=os.environ.get("GARUDA_MODEL", "openai/gpt-4o-mini"))
    run_parser.add_argument("--workspace", default=".", help="Workspace root directory")
    run_parser.add_argument(
        "--workspace-kind",
        choices=["local", "sandbox", "tmux", "docker", "remote"],
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
        choices=["standard", "rigorous", "readonly"],
        default=None,
        help="Override the agent profile's mode (defaults to the profile's own)",
    )
    run_parser.add_argument("--max-turns", type=int)
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
    chat_parser.add_argument("--model", default=os.environ.get("GARUDA_MODEL", "openai/gpt-4o-mini"))
    chat_parser.add_argument("--workspace", default=".")
    chat_parser.add_argument(
        "--workspace-kind",
        choices=["local", "sandbox", "tmux", "docker", "remote"],
        default="local",
    )
    chat_parser.add_argument("--docker-image", default="ubuntu:22.04")
    chat_parser.add_argument("--docker-host")
    chat_parser.add_argument("--agent", default="build")
    chat_parser.add_argument("--agents-dir")
    chat_parser.add_argument("--mcp-config")
    chat_parser.add_argument("--mode", choices=["standard", "rigorous", "readonly"], default=None)
    chat_parser.add_argument("--json", action="store_true")

    serve_parser = subparsers.add_parser("serve", help="Start JSON-RPC HTTP server for IDE integrations")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--model", default=os.environ.get("GARUDA_MODEL", "openai/gpt-4o-mini"))
    serve_parser.add_argument("--agent", default="build")
    serve_parser.add_argument("--workspace", default=".")
    serve_parser.add_argument(
        "--workspace-kind",
        choices=["local", "sandbox", "tmux", "docker", "remote"],
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
    recipe_run.add_argument("--model", default=os.environ.get("GARUDA_MODEL", "openai/gpt-4o-mini"))
    recipe_run.add_argument("--workspace", default=".")
    recipe_run.add_argument(
        "--workspace-kind",
        choices=["local", "sandbox", "tmux", "docker", "remote"],
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


async def run_task(args) -> int:
    import sys

    task = args.task
    if args.file:
        task = Path(args.file).read_text(encoding="utf-8")
    if not task:
        print("Error: provide -t/--task or -f/--file", file=sys.stderr)
        return 1

    from garuda.config.agent_home import resolve_agents_dirs

    agents_dir = resolve_agents_dirs(args.workspace, args.agents_dir)
    profile = load_profile(args.agent, extra_dir=agents_dir)
    config = profile.to_agent_config()
    if args.mode:  # else keep the profile's own mode
        config.mode = args.mode
    if args.max_turns is not None:
        config.max_turns = args.max_turns
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


def main() -> None:
    import asyncio

    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        raise SystemExit(asyncio.run(run_task(args)))
    if args.command == "chat":
        raise SystemExit(asyncio.run(chat_loop(args)))
    if args.command == "serve":
        raise SystemExit(asyncio.run(run_serve(args)))
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
