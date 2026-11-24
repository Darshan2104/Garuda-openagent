"""What an installed ACP adapter offers a role, asked without a prompt (#714).

One probe launches the adapter exactly as discovery accepted it, in an empty
scratch directory, runs ``initialize`` and ``session/new``, and checks the
role's options the way a launch does: the model first, because the effort and
mode values an agent offers depend on the model, then each other option
against what the agent reports after the last set. ``session/prompt`` is never
sent, so no model turn or quota is spent. The adapter and its scratch
directory are removed afterwards.

A probe answers :class:`Offered` (the agent's version, and the refusal a launch
would meet, if any) or ``None`` when it could not tell: the adapter did not
start, did not answer in time, or broke the protocol. Unknown is never a
reason to skip a harness; a launch then decides as before.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass

from garuda.runtime.roles import (
    PROVEN_MODE_OPTION,
    PROVEN_OPTIONS,
    RolePlan,
    RoleRefused,
    acp_options,
    acp_permission,
    check_mode,
    check_offered,
    harness_mode,
    not_applied,
    reported_mode,
)


@dataclass(frozen=True)
class Offered:
    #: The version the agent reported at ``initialize``.
    version: str
    #: The refusal a launch with these options would meet; ``None`` when all were taken.
    refusal: RoleRefused | None = None


async def probe(argv: list[str], options_for: Callable[[str], dict[str, str]], *,
                runtime_id: str, select: bool = True, required_mode: str | None = None,
                permission: str = "smart") -> Offered | None:
    """Check ``options_for(version)`` against the agent at ``argv``.

    With ``select`` false only what ``session/new`` offers is checked and
    nothing is set (enough for a model). With ``required_mode``, the mode the
    agent then reports must be no broader than it, as before a prompt
    (:func:`~garuda.runtime.roles.check_mode`). Handshake deadlines are the
    launch's own (``garuda.acp.client.HANDSHAKE_TIMEOUT``).
    """
    from garuda.acp.client import AcpProcess

    workspace = tempfile.mkdtemp(prefix="garuda-options-probe-")
    process = AcpProcess(list(argv), cwd=workspace)
    try:
        await process.launch()
        hello = await process.initialize()
        version = (hello.get("agentInfo") or {}).get("version")
        version = version if isinstance(version, str) and version else "unknown"
        try:
            options = options_for(version)
        except RoleRefused as exc:
            return Offered(version, exc)
        if not options and required_mode is None:
            return Offered(version)
        session_id = await process.session_new(workspace, [])
        model = options.get("model")
        for config_id, value in sorted(options.items(), key=lambda item: item[0] != "model"):
            try:
                check_offered({config_id: value}, process.session_config_options,
                              model=model, runtime_id=runtime_id)
            except RoleRefused as exc:
                return Offered(version, exc)
            if not select:
                continue
            current = await process.session_set_config_option(session_id, config_id, value)
            now = next((o.get("currentValue") for o in current
                        if isinstance(o, dict) and o.get("id") == config_id), None)
            if now != value:
                return Offered(version, not_applied(runtime_id, config_id, value, now, model=model))
        mode = options.get(PROVEN_MODE_OPTION)
        now = reported_mode(process.session_config_options, process.session_modes)
        if select and mode is not None and now != mode:  # a later set moved the mode again
            return Offered(version, not_applied(runtime_id, PROVEN_MODE_OPTION, mode, now,
                                                model=model))
        try:
            check_mode(runtime_id, required_mode, now, adapter_version=version,
                       permission=permission, set_by_garuda=select and mode is not None)
        except RoleRefused as exc:
            return Offered(version, exc)
        return Offered(version)
    except Exception:
        return None  # could not tell
    finally:
        try:
            await process.close()
        except Exception:
            pass
        shutil.rmtree(workspace, ignore_errors=True)


def run(argv: list[str], options_for: Callable[[str], dict[str, str]], **kwargs) -> Offered | None:
    """:func:`probe` from synchronous code, inside or outside a running event loop."""
    answer: list[Offered | None] = [None]

    def target() -> None:
        try:
            answer[0] = asyncio.run(probe(argv, options_for, **kwargs))
        except BaseException:
            answer[0] = None

    thread = threading.Thread(target=target, name="garuda-options-probe", daemon=True)
    thread.start()
    thread.join()
    return answer[0]


def _argv(manifest, record) -> list[str]:
    from garuda.acp.catalog import require_acp_argv

    return require_acp_argv(manifest, executable=record.executable)


def model_offered(manifest, record, model_id: str) -> bool | None:
    """Whether the agent offers ``model_id`` in a new session; ``None`` when unknown.

    Only a proven adapter's model option is read (its id comes from
    :data:`~garuda.runtime.roles.PROVEN_OPTIONS`); any other answer is unknown.
    """
    runtime_id = manifest.runtime_id

    def options_for(version: str) -> dict[str, str]:
        config_id = PROVEN_OPTIONS.get((runtime_id, version), {}).get("model_id")
        if config_id is None:
            raise RoleRefused("role.options_unproven", f"{runtime_id} {version}")
        return {config_id: model_id}

    try:
        argv = _argv(manifest, record)
    except Exception:
        return None
    answer = run(argv, options_for, runtime_id=runtime_id, select=False)
    if answer is None or answer.refusal is None:
        return None if answer is None else True
    return False if answer.refusal.code == "role.model_unavailable" else None


def check_plan(manifest, record, plan: RolePlan) -> Offered | None:
    """Select ``plan``'s model, effort and mode on a scratch session, as a launch
    would, then check the mode the agent reports against the one the role allows."""
    return run(_argv(manifest, record), lambda version: acp_options(plan, version),
               runtime_id=manifest.runtime_id, required_mode=harness_mode(plan),
               permission=acp_permission(plan))
