import asyncio
from pathlib import Path

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.interfaces.main import run_agent_task
from garuda.model.litellm_model import LitellmModel
from garuda.tools import build_toolkit


async def stdin_approval(action: str) -> bool:
    print(f"\n[garuda] Approve {action}? [y/N]: ", end="", flush=True)
    answer = await asyncio.to_thread(input)
    return answer.strip().lower() in ("y", "yes")


async def chat_loop(args) -> int:
    model = LitellmModel(model_name=args.model)
    profile = load_profile(args.agent, extra_dir=Path(args.agents_dir) if args.agents_dir else None)
    config = profile.to_agent_config()
    config.workspace_kind = getattr(args, "workspace_kind", "local")
    config.docker_image = getattr(args, "docker_image", "ubuntu:22.04")
    mcp_path = getattr(args, "mcp_config", None) or config.mcp_config_path
    permissions = PermissionEngine(
        mode=profile.permission_mode,
        tool_rules=profile.tool_rules,
        approval_handler=stdin_approval if profile.permission_mode == "smart" else None,
    )
    tools, mcp_manager = await build_toolkit(profile.tools, mcp_path)
    agent = DefaultAgent(profile_name=profile.name)

    print(f"Garuda chat — agent={profile.name} model={args.model} workspace={config.workspace_kind}")
    print("Enter a task (empty line to quit).\n")

    while True:
        print("task> ", end="", flush=True)
        task = await asyncio.to_thread(input)
        if not task.strip():
            if mcp_manager:
                await mcp_manager.close()
            print("Bye.")
            return 0

        result = await run_agent_task(
            task=task.strip(),
            model=model,
            agent=agent,
            tools=tools,
            config=config,
            permissions=permissions,
            workspace=args.workspace,
            events=EventStore(),
            emit_json=args.json,
            workspace_kind=config.workspace_kind,
            docker_image=config.docker_image,
            mcp_manager=mcp_manager,
        )
        print(f"\n{result.final_message}\n")
