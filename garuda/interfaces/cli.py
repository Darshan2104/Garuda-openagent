import asyncio
from pathlib import Path

from garuda.interfaces.runner import run_agent_task
from garuda.interfaces.session import AgentSession


async def stdin_approval(action: str) -> bool:
    print(f"\n[garuda] Approve {action}? [y/N]: ", end="", flush=True)
    answer = await asyncio.to_thread(input)
    return answer.strip().lower() in ("y", "yes")


async def chat_loop(args) -> int:
    agents_dir = Path(args.agents_dir) if args.agents_dir else None
    from garuda.agents.loader import load_profile

    profile = load_profile(args.agent, extra_dir=agents_dir)
    approval = stdin_approval if profile.permission_mode == "smart" else None

    session = await AgentSession.create(
        agent_name=args.agent,
        model=args.model,
        workspace=args.workspace,
        agents_dir=agents_dir,
        mcp_config_path=getattr(args, "mcp_config", None),
        mode=getattr(args, "mode", "standard"),
        approval_handler=approval,
        workspace_kind=getattr(args, "workspace_kind", "local"),
        docker_image=getattr(args, "docker_image", "ubuntu:22.04"),
        docker_host=getattr(args, "docker_host", None),
    )

    print(
        f"Garuda chat — agent={session.profile.name} model={args.model} "
        f"workspace={session.config.workspace_kind}"
    )
    print("Enter a task (empty line to quit).\n")

    while True:
        print("task> ", end="", flush=True)
        task = await asyncio.to_thread(input)
        if not task.strip():
            await session.close()
            print("Bye.")
            return 0

        context = session.prepare_context(task.strip())
        result = await run_agent_task(
            task=task.strip(),
            model=session.model,
            agent=session.agent,
            tools=session.tools,
            config=session.config,
            permissions=session.permissions,
            workspace=args.workspace,
            events=session.events,
            emit_json=args.json,
            workspace_kind=session.config.workspace_kind,
            docker_image=session.config.docker_image,
            docker_host=session.config.docker_host,
            mcp_manager=session.mcp_manager,
            agents_dir=session.agents_dir,
            context=context,
            close_mcp=False,
        )
        print(f"\n{result.final_message}\n")
