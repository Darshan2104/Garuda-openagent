import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.interfaces.cli import chat_loop
from garuda.model.litellm_model import LitellmModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, AgentResult
from garuda.workspace.local import LocalEnvironment


async def run_agent_task(
    task: str,
    model,
    agent: DefaultAgent,
    tools,
    config: AgentConfig,
    permissions: PermissionEngine,
    workspace: str,
    events: EventStore,
    emit_json: bool = False,
) -> AgentResult:
    env = LocalEnvironment(workspace_root=workspace)
    result = await agent.run(
        task=task,
        model=model,
        env=env,
        tools=tools,
        config=config,
        events=events,
        permissions=permissions,
    )
    if emit_json:
        for event in events.get_all():
            print(json.dumps(event))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="garuda", description="Garuda Open Agent harness")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a single agent task (headless)")
    run_parser.add_argument("-t", "--task", help="Task description")
    run_parser.add_argument("-f", "--file", help="Read task from file")
    run_parser.add_argument("--model", default=os.environ.get("GARUDA_MODEL", "openai/gpt-4o-mini"))
    run_parser.add_argument("--workspace", default=".", help="Workspace root directory")
    run_parser.add_argument("--agent", default="build", help="Agent profile name")
    run_parser.add_argument("--agents-dir", help="Directory with custom agent YAML profiles")
    run_parser.add_argument("--permission-mode", choices=["auto", "smart", "readonly", "yolo"])
    run_parser.add_argument("--max-turns", type=int)
    run_parser.add_argument("--no-verifier", action="store_true")
    run_parser.add_argument("--json", action="store_true", help="Print JSONL events to stdout")
    run_parser.add_argument("--trajectory", help="Save event trajectory to JSONL file")

    chat_parser = subparsers.add_parser("chat", help="Interactive agent session with permission prompts")
    chat_parser.add_argument("--model", default=os.environ.get("GARUDA_MODEL", "openai/gpt-4o-mini"))
    chat_parser.add_argument("--workspace", default=".")
    chat_parser.add_argument("--agent", default="build")
    chat_parser.add_argument("--agents-dir")
    chat_parser.add_argument("--json", action="store_true")

    return parser


async def run_task(args: argparse.Namespace) -> int:
    task = args.task
    if args.file:
        task = Path(args.file).read_text(encoding="utf-8")
    if not task:
        print("Error: provide -t/--task or -f/--file", file=sys.stderr)
        return 1

    profile = load_profile(args.agent, extra_dir=Path(args.agents_dir) if args.agents_dir else None)
    config = profile.to_agent_config()
    if args.max_turns is not None:
        config.max_turns = args.max_turns
    if args.permission_mode:
        config.permission_mode = args.permission_mode
    if args.no_verifier:
        config.enable_verifier = False

    model = LitellmModel(model_name=args.model)
    permissions = PermissionEngine(mode=config.permission_mode, tool_rules=profile.tool_rules)
    agent = DefaultAgent(profile_name=profile.name)
    events = EventStore()
    tools = tools_for_names(profile.tools)

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
    )

    if args.trajectory:
        events.save(args.trajectory)
    if not args.json:
        print(result.final_message)
    return 0 if result.success else 1


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        raise SystemExit(asyncio.run(run_task(args)))
    if args.command == "chat":
        raise SystemExit(asyncio.run(chat_loop(args)))
    parser.print_help()
    raise SystemExit(1)
