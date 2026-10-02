import logging
from dataclasses import dataclass, field, fields
from importlib import resources
from pathlib import Path

from garuda.types import DEFAULT_SYSTEM_PROMPT, AgentConfig

logger = logging.getLogger(__name__)


@dataclass
class AgentProfile:
    name: str
    description: str = ""
    permission_mode: str = "smart"
    mode: str = "standard"
    tools: list[str] | None = None
    system_prompt: str | None = None
    tool_rules: dict[str, str] | None = None
    path_rules: dict[str, list[str]] | None = None
    bash_rules: dict[str, list[str]] | None = None
    max_turns: int = 200
    enable_tmux: bool = True
    marker_polling: bool = True
    enable_three_step_summary: bool = True
    # Read-only/advisory profiles turn this off: deriving acceptance criteria
    # costs a model call, and a gate demanding workspace evidence is meaningless
    # for an agent whose output is a report rather than a change.
    enable_acceptance_contract: bool = True
    max_context_tokens: int = 128_000
    proactive_summarize_threshold: int = 8000
    max_output_bytes: int = 30_720
    # Context-budget knobs. Authorable per profile for the same reason
    # max_output_bytes is: a read-only explore agent and a long build agent want
    # very different amounts of the window held back and spent per observation.
    #
    # Defaults are read off AgentConfig rather than repeated. Repeating them meant
    # retuning the reserve in types.py changed nothing that ran: every profile
    # passes its own value through to_agent_config(), so the copy here silently won.
    reserved_output_tokens: int = AgentConfig.reserved_output_tokens
    context_safety_margin_tokens: int = AgentConfig.context_safety_margin_tokens
    enable_request_preflight: bool = AgentConfig.enable_request_preflight
    enable_adaptive_output: bool = AgentConfig.enable_adaptive_output
    min_output_bytes: int = AgentConfig.min_output_bytes
    # Cap on the model's own response. Sent to the provider, and the figure the
    # context reserve is derived from — the two must not drift apart.
    max_tokens: int | None = AgentConfig.max_tokens
    enable_working_state_card: bool = AgentConfig.enable_working_state_card
    # Compilation owners (H.12a): the condenser, a wall-clock deadline, the
    # completion verifier, and Docker limits that may only narrow the grant.
    condenser: str = AgentConfig.condenser
    deadline_sec: float | None = AgentConfig.deadline_sec
    enable_verifier: bool = True
    docker_network: bool | None = None
    docker_memory: str | None = None
    docker_cpus: float | None = None
    # Memory sources (H.4). The defaults are what a legacy profile always had:
    # the first of AGENTS.md / GARUDA.md, no user memory, no context pack.
    memory_user: bool = False
    memory_project: list[str] | None = None
    memory_project_mode: str = "first"
    memory_max_chars: int = 8000
    memory_max_total_chars: int = 32_000
    memory_context_pack: bool = False
    workspace_kind: str = "local"
    docker_image: str = "ubuntu:22.04"
    mcp_config_path: str | None = None
    mcp_servers: list[str] | None = None
    skills: list[str] | None = None
    skills_dirs: list[str] | None = None
    subagent: bool = False
    reasoning_effort: str | None = None
    thinking_budget_tokens: int | None = None
    # Dual-model role binding: a reference to a globally authorized
    # `model_bindings` alias (resolved in agents/setup.py). A profile may only
    # reference an alias and narrow collection limits — never define providers,
    # endpoints, or wider ceilings.
    model_binding: str | None = None
    # Raw collection narrowing block (parsed into a CollectionPolicy by setup).
    # Kept as a plain dict so profile YAML stays declarative and trust checks
    # see the profile as the source.
    collection: dict | None = None
    source_path: Path | None = None
    # Field names this profile set explicitly, so a mode preset can leave authored
    # intent alone. Diffing against dataclass defaults would not do: a profile
    # declaring a value that happens to equal the default still chose it.
    declared_fields: set[str] = field(default_factory=set)
    # 1 when the definition was a version 1 file (H.1); None for legacy profiles.
    spec_version: int | None = None

    def to_agent_config(self) -> AgentConfig:
        return AgentConfig(
            max_turns=self.max_turns,
            mode=self.mode,
            permission_mode=self.permission_mode,
            system_prompt=self.system_prompt or DEFAULT_SYSTEM_PROMPT,
            allowed_tools=self.tools,
            enable_verifier=self.enable_verifier,
            condenser=self.condenser,
            deadline_sec=self.deadline_sec,
            enable_tmux=self.enable_tmux,
            marker_polling=self.marker_polling,
            enable_three_step_summary=self.enable_three_step_summary,
            enable_acceptance_contract=self.enable_acceptance_contract,
            max_context_tokens=self.max_context_tokens,
            proactive_summarize_threshold=self.proactive_summarize_threshold,
            max_output_bytes=self.max_output_bytes,
            reserved_output_tokens=self.reserved_output_tokens,
            context_safety_margin_tokens=self.context_safety_margin_tokens,
            enable_request_preflight=self.enable_request_preflight,
            enable_adaptive_output=self.enable_adaptive_output,
            min_output_bytes=self.min_output_bytes,
            max_tokens=self.max_tokens,
            enable_working_state_card=self.enable_working_state_card,
            workspace_kind=self.workspace_kind,
            docker_image=self.docker_image,
            mcp_config_path=self.mcp_config_path,
            skills=self.skills,
            skills_dirs=self.skills_dirs,
            reasoning_effort=self.reasoning_effort,
            thinking_budget_tokens=self.thinking_budget_tokens,
        )


def _defaults_dir() -> Path:
    return Path(resources.files("garuda.agents")) / "defaults"


def _profile_names_in_dir(directory: Path) -> set[str]:
    names: set[str] = set()
    if not directory.exists():
        return names
    for path in directory.glob("*.yaml"):
        names.add(path.stem)
    for path in directory.glob("*.md"):
        names.add(path.stem)
    if (directory / "agent.md").is_file():
        names.add(directory.name)
    for path in directory.glob("**/agent.md"):
        names.add(path.parent.name)
    return names


def _as_dir_list(extra_dir: Path | list[Path] | None) -> list[Path]:
    """Normalize an ``extra_dir`` (single, list, or None) to a list of dirs."""
    if extra_dir is None:
        return []
    if isinstance(extra_dir, (list, tuple)):
        return [Path(d) for d in extra_dir]
    return [Path(extra_dir)]


def list_profiles(extra_dir: Path | list[Path] | None = None) -> list[str]:
    from garuda.agents.resolve import user_agents_dir

    names = _profile_names_in_dir(_defaults_dir()) | _profile_names_in_dir(user_agents_dir())
    for directory in _as_dir_list(extra_dir):
        names.update(_profile_names_in_dir(directory))
    return sorted(names)


_PROFILE_FIELD_NAMES = {f.name for f in fields(AgentProfile)}


#: Older spelling accepted for ``model_binding``.
_FIELD_ALIASES = {"model_bindings": "model_binding"}
#: Fields a file never sets itself.
_INTERNAL_FIELDS = frozenset({"declared_fields", "source_path", "spec_version"})
#: List fields a file may also give as a single string.
_LIST_FIELDS = ("tools", "skills", "skills_dirs", "mcp_servers")

_PERMISSION_MODES = ("readonly", "smart", "auto", "yolo")
_TOOL_RULE_VALUES = ("allow", "deny", "ask")
_RULE_KEYS = {"path_rules": ("deny", "ask"), "bash_rules": ("deny", "ask", "allow_prefixes")}


def _refuse(source: Path | None, message: str) -> None:
    from garuda.model.config import ConfigError

    raise ConfigError(f"profile {source or '(inline)'}: {message}")


def _validate_security_fields(data: dict, source: Path | None) -> None:
    """Refuse an ambiguous permission policy instead of guessing what it meant."""
    mode = data.get("permission_mode")
    if mode is not None and mode not in _PERMISSION_MODES:
        _refuse(source, f"permission_mode must be one of {', '.join(_PERMISSION_MODES)}; got {mode!r}")
    tool_rules = data.get("tool_rules")
    if tool_rules is not None:
        if not isinstance(tool_rules, dict):
            _refuse(source, "tool_rules must map tool names to allow, deny or ask")
        for tool, value in tool_rules.items():
            if value not in _TOOL_RULE_VALUES:
                _refuse(source, f"tool_rules.{tool} must be allow, deny or ask; got {value!r}")
    for field_name, keys in _RULE_KEYS.items():
        rules = data.get(field_name)
        if rules is None:
            continue
        if not isinstance(rules, dict):
            _refuse(source, f"{field_name} must be a mapping with keys {', '.join(keys)}")
        for key, values in rules.items():
            if key not in keys:
                _refuse(source, f"{field_name}.{key} is not a rule; use {', '.join(keys)}")
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                _refuse(source, f"{field_name}.{key} must be a list of strings")


def _warn_unknown_keys(data: dict, source: Path | None) -> None:
    import difflib

    known = (_PROFILE_FIELD_NAMES - _INTERNAL_FIELDS) | set(_FIELD_ALIASES)
    for key in data:
        if key in known:
            continue
        close = difflib.get_close_matches(str(key), sorted(known), n=1)
        hint = f"; did you mean {close[0]!r}?" if close else ""
        logger.warning("Profile %s: unknown key %r is ignored%s", source or "(inline)", key, hint)


def profile_from_mapping(
    data: dict,
    name: str,
    source: Path | None = None,
    *,
    system_prompt: str | None = None,
) -> AgentProfile:
    """Build a profile from parsed YAML or ``agent.md`` front matter.

    One parser for both formats, driven by the ``AgentProfile`` fields
    themselves, so a field cannot be readable in one format and silently
    dropped in the other. ``system_prompt`` overrides the mapping's own value
    (an ``agent.md`` body).
    """
    if data is None:
        data = {}
    if not isinstance(data, dict):
        _refuse(source, "must be a mapping of profile fields")
    _warn_unknown_keys(data, source)
    _validate_security_fields(data, source)
    values: dict = {}
    for key, value in data.items():
        target = _FIELD_ALIASES.get(key, key)
        if target in _INTERNAL_FIELDS or target not in _PROFILE_FIELD_NAMES:
            continue
        if target in values and key != target:
            continue  # the canonical spelling wins over the alias
        values[target] = value
    for list_field in _LIST_FIELDS:
        if isinstance(values.get(list_field), str):
            values[list_field] = [values[list_field]]
    if system_prompt:
        values["system_prompt"] = system_prompt
    values.setdefault("name", name)
    return AgentProfile(
        declared_fields={key for key in values if key != "name" or "name" in data},
        source_path=source,
        **values,
    )


def _profile_from_yaml(data: dict, name: str, source: Path | None = None) -> AgentProfile:
    return profile_from_mapping(data, name, source)


def load_profile(name: str, extra_dir: Path | list[Path] | None = None) -> AgentProfile:
    """Load an agent by name through the one resolver (H.1).

    ``extra_dir`` holds the project agent directories, earliest first (e.g.
    ``.agent/agents`` then ``.garuda/agents``); the user's agents directory
    and the packaged defaults follow. ``garuda/<name>``, ``user/<name>`` and
    ``project/<name>`` pick a location explicitly. Legacy profiles and
    version 1 definitions (with ``extends``) resolve the same way.
    """
    from garuda.agents.resolve import activate, resolve_agent

    return activate(resolve_agent(name, _as_dir_list(extra_dir)))


# Maximum characters of AGENTS.md/GARUDA.md content injected into the system prompt.
PROJECT_MEMORY_MAX_CHARS = 8000

# Project-memory file names checked in the workspace root; first found wins.
PROJECT_MEMORY_FILENAMES = ("AGENTS.md", "GARUDA.md")


MEMORY_TRUNCATED = "memory.truncated"


def _project_memory_block(
    workspace_root: str | Path, diagnostics: list[dict] | None = None
) -> str:
    """The legacy project-memory block: the first of AGENTS.md / GARUDA.md in the
    workspace root, cut at :data:`PROJECT_MEMORY_MAX_CHARS` with a
    ``memory.truncated`` diagnostic. Assembled by :mod:`garuda.agents.prompt`."""
    from garuda.agents.prompt import _project_memory

    found: list[dict] = []
    sections = _project_memory(AgentProfile(name="memory"), Path(workspace_root), found)
    if diagnostics is not None:
        diagnostics.extend(found)
    return "".join(section.text for section in sections)


def _warn_unsatisfiable_skill_tools(skills, granted_tools: list[str] | None) -> None:
    """Warn when a skill's ``allowed-tools`` reference tools the profile doesn't grant.

    ``granted_tools=None`` means the profile grants all built-ins, so nothing to
    check. ``mcp__*`` names are skipped (MCP tool names aren't known statically).
    """
    if granted_tools is None:
        return
    granted = set(granted_tools)
    for skill in skills:
        for tool in skill.allowed_tools or []:
            if tool.startswith("mcp__"):
                continue
            if tool not in granted:
                logger.warning(
                    "Skill %r lists allowed tool %r which the agent profile does not grant",
                    skill.name,
                    tool,
                )


def system_prompt_sections(
    profile: AgentProfile,
    workspace_root: str | Path | None = None,
    *,
    diagnostics: list[dict] | None = None,
) -> list[tuple[str, str, str]]:
    """The static system prompt as ``(section, source, text)`` in order.

    Joined, the texts are exactly :func:`resolve_system_prompt`'s result, so
    ``garuda agent prompt`` shows what a run sends before any runtime block.
    """
    from garuda.skills.loader import discover_skills, format_skills_prompt

    base = profile.system_prompt or DEFAULT_SYSTEM_PROMPT
    skill_dirs: list[Path] = []
    if workspace_root:
        # Standard discovery: the `.agent/skills` (and back-compat `.garuda/skills`)
        # dirs resolved from the same agent-home used for profiles/tools/MCP.
        from garuda.config.agent_home import resolve_agent_home

        skill_dirs.extend(resolve_agent_home(workspace_root).skills_dirs)
    if profile.skills_dirs:
        # A relative skills dir means "relative to the workspace", not to whatever
        # cwd the process happens to have. Under `serve` or the SDK those differ, so
        # the configured skills were looked for in the wrong place and silently not
        # found. Falls back to the plain path when no workspace is known.
        workspace_dir = Path(workspace_root) if workspace_root else None
        for raw in profile.skills_dirs:
            candidate = Path(raw)
            if not candidate.is_absolute() and workspace_dir is not None:
                candidate = workspace_dir / candidate
            skill_dirs.append(candidate)

    discovered = discover_skills(*skill_dirs)
    if profile.skills:
        allowed = set(profile.skills)
        discovered = [s for s in discovered if s.name in allowed]
    _warn_unsatisfiable_skill_tools(discovered, profile.tools)
    from garuda.agents.prompt import build_plan

    plan = build_plan(profile, workspace_root, base=base,
                      skills_block=format_skills_prompt(discovered),
                      skills_source=", ".join(str(d) for d in skill_dirs) or "-",
                      diagnostics=diagnostics)
    return [(s.kind, s.source, s.text) for s in plan.sections]


def resolve_system_prompt(
    profile: AgentProfile,
    workspace_root: str | Path | None = None,
    *,
    diagnostics: list[dict] | None = None,
) -> str:
    """Build system prompt with optional skill injection and project memory.

    ``diagnostics`` collects anything the user should know about what was
    left out (for example a truncated ``AGENTS.md``).
    """
    return "".join(text for _name, _source, text in
                   system_prompt_sections(profile, workspace_root, diagnostics=diagnostics))
