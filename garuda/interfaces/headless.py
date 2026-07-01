import argparse
import asyncio
import json
import os
import sys

from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.model.litellm_model import LitellmModel
from garuda.tools import default_tools
from garuda.types import AgentConfig
from garuda.workspace.local import LocalEnvironment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="garuda", description="Garuda Open Agent harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run an agent task")
    run_parser.add_argument("-t", "--task", help="Task description")
    run_parser.add_argument("-f", "--file", help="Read task from file")
    run_parser.add_argument("--model", default=os.environ.get("GARUDA_MODEL", "openai/gpt-4o-mini"))
    run_parser.add_argument("--workspace", default=".", help="Workspace root directory")
    run_parser.add_argument("--max-turns", type=int, default=30)
    run_parser.add_argument("--json", action="store_true", help="Print JSONL events to stdout")
    run_parser.add_argument(
        "--trajectory",
        help="Save event trajectory to JSONL file",
    )
    return parser


async def run_task(args: argparse.Namespace) -> int:
    task = args.task
    if args.file:
        task = open(args.file, encoding="utf-8").read()
    if not task:
        print("Error: provide -t/--task or -f/--file", file=sys.stderr)
        return 1

    model = LitellmModel(model_name=args.model)
    env = LocalEnvironment(workspace_root=args.workspace)
    agent = DefaultAgent()
    events = EventStore()
    tools = default_tools()
    config = AgentConfig(max_turns=args.max_turns)

    result = await agent.run(
        task=task,
        model=model,
        env=env,
        tools=tools,
        config=config,
        events=events,
    )

    if args.trajectory:
        events.save(args.trajectory)

    if args.json:
        for event in events.get_all():
            print(json.dumps(event))
    else:
        print(result.final_message)

    return 0 if result.success else 1


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        raise SystemExit(asyncio.run(run_task(args)))
    parser.print_help()
    raise SystemExit(1)


if __name__ == "__main__":
    main()
