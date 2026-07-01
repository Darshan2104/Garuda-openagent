from pathlib import Path

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.interfaces.cli import chat_loop
from garuda.interfaces.runner import cleanup_workspace, resolve_environment, run_agent_task
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
    run_parser.add_argument("--agent", default="build", help="Agent profile name")
    run_parser.add_argument("--agents-dir", help="Directory with custom agent YAML profiles")
    run_parser.add_argument("--mcp-config", help="Path to MCP servers YAML config")
    run_parser.add_argument("--permission-mode", choices=["auto", "smart", "readonly", "yolo"])
    run_parser.add_argument("--mode", choices=["standard", "rigorous", "readonly"], default="standard")
    run_parser.add_argument("--max-turns", type=int)
    run_parser.add_argument("--no-verifier", action="store_true")
    run_parser.add_argument("--no-three-step-summary", action="store_true")
    run_parser.add_argument("--json", action="store_true", help="Print JSONL events to stdout")
    run_parser.add_argument("--trajectory", help="Save event trajectory to JSONL file")

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


async def run_task(args) -> int:
    import sys

    task = args.task
    if args.file:
        task = Path(args.file).read_text(encoding="utf-8")
    if not task:
        print("Error: provide -t/--task or -f/--file", file=sys.stderr)
        return 1

    profile = load_profile(args.agent, extra_dir=Path(args.agents_dir) if args.agents_dir else None)
    config = profile.to_agent_config()
    config.mode = args.mode
    if args.max_turns is not None:
        config.max_turns = args.max_turns
    if args.permission_mode:
        config.permission_mode = args.permission_mode
    if args.no_verifier:
        config.enable_verifier = False
    if args.no_three_step_summary:
        config.enable_three_step_summary = False
    config.workspace_kind = args.workspace_kind
    config.docker_image = args.docker_image
    config.docker_host = args.docker_host
    mcp_path = args.mcp_config or config.mcp_config_path

    model = LitellmModel(model_name=args.model)
    permissions = PermissionEngine(mode=config.permission_mode, tool_rules=profile.tool_rules)
    agent = create_agent(profile.name, mode=config.mode)
    events = EventStore()
    tools, mcp_manager = await build_toolkit(profile.tools, mcp_path)

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
    )

    if args.trajectory:
        events.save(args.trajectory)
    if not args.json:
        print(result.final_message)
    return 0 if result.success else 1


async def run_recipe_command(args) -> int:
    import sys

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
            agents_dir=Path(args.agents_dir) if args.agents_dir else None,
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
    )
    await serve(config)
    return 0


def main() -> None:
    import asyncio
    import sys

    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        raise SystemExit(asyncio.run(run_task(args)))
    if args.command == "chat":
        raise SystemExit(asyncio.run(chat_loop(args)))
    if args.command == "serve":
        raise SystemExit(asyncio.run(run_serve(args)))
    if args.command == "recipe" and args.recipe_command == "run":
        raise SystemExit(asyncio.run(run_recipe_command(args)))
    parser.print_help()
    raise SystemExit(1)
