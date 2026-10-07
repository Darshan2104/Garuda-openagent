"""One role-request bridge to Garuda's existing execution and queue owners."""

from __future__ import annotations

import shlex
import uuid

from garuda.core.sessions import SessionStore
from garuda.scenarios.types import LaunchPlan


async def execute_role_request(plan: LaunchPlan, store: SessionStore) -> dict:
    from garuda.interfaces.bg_sessions import launch
    from garuda.interfaces.main import build_parser, run_task_guarded

    # Parse the already quoted existing command as argv, never execute a shell.
    # The same parser supplies every existing native/ACP flag and default.
    args = build_parser().parse_args(shlex.split(plan.equivalent_command)[1:])
    args._store = store
    args.starter_record = plan.launch_metadata()
    if args.bg:
        session_id = launch(args, store=store)
        return {"session_id": session_id, "exit_code": 0}
    args._session_id = str(uuid.uuid4())
    code = await run_task_guarded(args)
    session_id = getattr(args, "_completed_session_id", args._session_id)
    if not (store.session_dir(session_id) / "meta.json").is_file():
        session_id = None
    return {"session_id": session_id, "exit_code": code}
