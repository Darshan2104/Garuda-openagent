"""Which agent a remote caller may select, and with what authority (plan task H.8, #166).

``serve`` and the web dashboard take an agent **name** from a caller. They never
take a definition: an inline mapping, a file path or an ``agents_dir`` supplied
over HTTP would let the caller write the policy it runs under, so those refuse
(``agent.inline_over_http``). A name is checked against the operator's allowlist
(``--allow-agent``, repeatable; unset allows any name the operator's own
directories define), and the run is capped at the operator's permission ceiling:
choosing a permissive packaged agent such as ``garuda/harbor`` can lower
nothing and raise nothing beyond what the operator started the server with.
"""

from __future__ import annotations

from garuda.agents.authority import PERMISSION_ORDER, PROJECT, profile_authority, project_ceiling
from garuda.model.config import ConfigError


class SelectionRefused(ConfigError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _rank(mode: str) -> int:
    return PERMISSION_ORDER.index(mode)


def check_named(selection, *, workspace: str, agents_dirs, allowed=None, ceiling=None,
                default_agent: str | None = None) -> str | None:
    """Vet a caller's agent selection; returns a permission mode to enforce, or None.

    ``allowed``: the names a caller may choose (``None``: no allowlist).
    ``ceiling``: the loosest mode a run may use. Unset, it is the mode of the
    operator's own default agent, so a caller can never run looser than the
    server's configured posture. The returned mode is passed to
    ``prepare_agent_run(permission_mode=...)`` only when it lowers the profile's own.
    """
    from garuda.agents.spec_api import AgentSpec

    if not isinstance(selection, str) or not selection:
        raise SelectionRefused("agent.inline_over_http",
                               "a caller selects an agent by name; definitions are not accepted "
                               "from a request")
    spec = AgentSpec.load(selection, workspace, agents_dirs)
    qualified = spec.resolved().source.qualified
    if allowed is not None and selection not in allowed and qualified not in allowed:
        raise SelectionRefused("agent.not_allowed",
                               f"{selection!r} is not one of the agents this server allows "
                               f"({', '.join(sorted(allowed)) or 'none'})")
    profile = spec.profile()
    if ceiling is None and default_agent is not None:
        ceiling = AgentSpec.load(default_agent, workspace, agents_dirs).profile().permission_mode
    ceiling = ceiling or "smart"
    limit = ceiling
    if profile_authority(profile.source_path, workspace) == PROJECT:
        from garuda.config.agent_home import resolve_agent_home

        project = project_ceiling(resolve_agent_home(workspace).global_settings)
        if _rank(project) < _rank(limit):
            limit = project
    declared = profile.permission_mode
    if declared in PERMISSION_ORDER and _rank(declared) <= _rank(limit):
        return None
    return limit
