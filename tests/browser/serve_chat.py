"""Serve a dashboard whose chat uses a ScriptModel that trips a `smart` ask.

`AgentSession.create` is patched to build the same session it always does but with a scripted
model, so every layer under test — the permission engine, the approval broker, the routes, the
heartbeat reaper — is the real one. The grace window is shortened so the abandoned-tab check
does not take 30 seconds.

`tools/web.py`'s fetcher is stubbed as well, so the grounding check exercises the route, the
panel and the saved file without any check in this suite reaching the network.
"""

import asyncio
import sys
from pathlib import Path

from garuda.interfaces.web import DashboardConfig, build_context, launch_url
from garuda.interfaces.web import http as http_module
from garuda.interfaces.web.approvals import ApprovalBroker
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.types import ToolCall

#: `smart` mode asks about this rather than allowing it, which is the whole point.
ASK_COMMAND = "rm -rf build/"


class ArmedAskModel(ScriptModel):
    """Asks for the screened command once per *submitted turn*, then finishes.

    Three shapes were tried; the first two are recorded because both failed in ways that
    looked like product bugs.

    A fixed `ScriptModel` list runs dry — once exhausted it returns a bare "Done." forever,
    so the second chat turn produced no tool call and there was no approval to test.

    Alternating ask/report per *call* is worse: the loop makes more than two calls per
    submission, so every odd call asked again and one user message produced an unbounded
    chain of approvals.

    Deriving it from the message list fails too, and this is the interesting one: after a
    tool result the newest user-role message is not the user's — the harness re-pins its
    working-state card as a user message — so "the newest user message changed" fires again
    inside the same turn.

    So the turn boundary comes from the only place that actually knows it: `chat_turn` arms
    the model as it submits. One ask per submission, and a content-only response ends it.
    """

    def __init__(self):
        super().__init__([], model_name="script/chat-check")
        self._armed = False
        self._calls = 0

    def arm(self) -> None:
        self._armed = True

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self._calls += 1
        if self._armed:
            self._armed = False
            return ModelResponse(
                content="I'll remove the build directory.",
                tool_calls=[
                    ToolCall(id=f"c{self._calls}", name="bash",
                             arguments={"command": ASK_COMMAND})
                ],
            )
        return ModelResponse(content="Done with the build directory.", tool_calls=[])

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        return await self.complete(messages, tools, temperature, max_tokens)


#: Stands in for the real page body. The fetcher itself is covered by unit tests; what a
#: browser check adds is the panel, the route and the provenance line in the saved file.
async def main() -> None:
    port = int(sys.argv[1])
    sessions = Path(sys.argv[2])
    workspace = Path(sys.argv[3])
    token = sys.argv[4]

    from garuda.interfaces import session as session_module

    original_create = session_module.AgentSession.create.__func__

    async def create_with_script(cls, **kwargs):
        session = await original_create(cls, **kwargs)
        session.model = ArmedAskModel()
        # Two model calls per turn is all this needs; a cap keeps a runaway loop from
        # filling the transcript if the script ever changes.
        session.config.max_turns = 4
        return session

    session_module.AgentSession.create = classmethod(create_with_script)

    # No check in this suite touches the network. The vetting itself is unit-tested; here the
    # subject is the route, the panel and the file that lands in the workspace.
    from garuda.tools import web as web_module

    web_module._blocking_fetch = lambda url, max_bytes: (
        None, f"Retry documentation for {url}.\n\nThe ceiling is 120 seconds."
    )

    config = DashboardConfig(
        port=port,
        token=token,
        sessions_dir=sessions,
        allow_run=True,
        workspaces=(workspace,),
        open_browser=False,
    )
    ctx = build_context(config, loop=asyncio.get_running_loop())
    # Short windows so the abandoned-tab check runs in seconds rather than minutes. The
    # values are published by /api/approvals, so the client and the check both read them
    # rather than assuming the defaults.
    ctx.live.approvals = ApprovalBroker(timeout=60.0, grace=3.0)
    ctx.live.reap_interval_seconds = 1.0

    # Arm the model as the turn is submitted: the only place that unambiguously knows a new
    # user turn has begun.
    original_turn = ctx.live.chat_turn

    async def armed_turn(chat_id, task):
        chat = ctx.live.chats.get(chat_id)
        if chat is not None and hasattr(chat.session.model, "arm"):
            chat.session.model.arm()
        return await original_turn(chat_id, task)

    ctx.live.chat_turn = armed_turn
    server, bound = http_module.bind(ctx, port)
    print(f"scripted chat dashboard on {launch_url(ctx)}", flush=True)
    try:
        await asyncio.to_thread(server.serve_forever)
    finally:
        server.shutdown()
        server.server_close()
        await ctx.live.aclose()


if __name__ == "__main__":
    asyncio.run(main())
