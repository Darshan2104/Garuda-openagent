"""YAML recipe loader and multi-step workflow runner."""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from garuda.agents.loader import resolve_system_prompt
from garuda.agents.setup import prepare_agent_run
from garuda.core.events import EventStore
from garuda.model.protocol import Model
from garuda.types import AgentResult
from garuda.workspace.protocol import Environment
from garuda.workspace.settlement import settle_task

logger = logging.getLogger(__name__)
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_PARAMETER_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")
_RECIPE_KEYS = ("name", "description", "parameters", "steps")
_PARAMETER_KEYS = ("name", "type", "required", "default")
_STEP_KEYS = ("agent", "prompt", "mode")
_MAX_ERROR_CHARS = 300


class RecipeError(ValueError):
    """A recipe refused before any step runs. ``code`` is stable (``recipe.*``)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class RecipeParameter:
    name: str
    type: str = "string"
    required: bool = False
    default: Any = None


@dataclass
class RecipeStep:
    agent: str
    prompt: str
    mode: str = "standard"


@dataclass
class Recipe:
    name: str
    description: str = ""
    parameters: list[RecipeParameter] = field(default_factory=list)
    steps: list[RecipeStep] = field(default_factory=list)


def render_template(template: str, params: dict[str, Any]) -> str:
    """Replace ``{{name}}`` placeholders with parameter values."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            raise KeyError(f"Missing recipe parameter: {key}")
        return str(params[key])

    return _TEMPLATE_PATTERN.sub(replace, template)


def _one_line(text: object) -> str:
    return " ".join(str(text).split())[:_MAX_ERROR_CHARS]


def load_recipe(path: str | Path) -> Recipe:
    """Load and validate a recipe YAML file; refuses with :class:`RecipeError`."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RecipeError("recipe.not_found", f"no recipe file at {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise RecipeError("recipe.unreadable", f"cannot read {path}: {_one_line(exc)}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeError("recipe.invalid", f"{path} is not valid recipe YAML: {_one_line(exc)}") from None
    return parse_recipe(data, default_name=path.stem)


def _invalid(where: str, message: str) -> RecipeError:
    return RecipeError("recipe.invalid", f"{where}: {message}")


def _check_keys(entry: dict, allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(str(key) for key in entry if key not in allowed)
    if unknown:
        raise _invalid(where, f"unknown key(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}")


def _text(value: object, where: str, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _invalid(where, "must be a non-empty string")
    return value


def parse_recipe(data: object, *, default_name: str = "recipe") -> Recipe:
    """Validate a whole recipe document before any step can run.

    Strict like other Garuda configuration: unknown keys, a missing or empty
    ``steps`` list, a step without ``agent`` or ``prompt``, an unknown ``mode``
    and a ``{{placeholder}}`` that names no declared parameter all refuse.
    """
    from garuda.core.modes import MODE_CHOICES

    if not isinstance(data, dict):
        raise _invalid("recipe", "must be a mapping with name, parameters and steps")
    _check_keys(data, _RECIPE_KEYS, "recipe")
    name = _text(data.get("name"), "name", required=False) or default_name
    description = data.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise _invalid("description", "must be a string")

    raw_parameters = data.get("parameters", [])
    if raw_parameters is None:
        raw_parameters = []
    if not isinstance(raw_parameters, list):
        raise _invalid("parameters", "must be a list")
    parameters: list[RecipeParameter] = []
    for index, entry in enumerate(raw_parameters):
        where = f"parameters[{index}]"
        if not isinstance(entry, dict):
            raise _invalid(where, "must be a mapping")
        _check_keys(entry, _PARAMETER_KEYS, where)
        param_name = entry.get("name")
        if not isinstance(param_name, str) or not _PARAMETER_NAME.match(param_name):
            raise _invalid(f"{where}.name", "must be a name of letters, digits and underscores")
        if any(param.name == param_name for param in parameters):
            raise _invalid(f"{where}.name", f"duplicate parameter {param_name!r}")
        param_type = entry.get("type", "string")
        if not isinstance(param_type, str):
            raise _invalid(f"{where}.type", "must be a string")
        required = entry.get("required", False)
        if not isinstance(required, bool):
            raise _invalid(f"{where}.required", "must be true or false")
        parameters.append(RecipeParameter(name=param_name, type=param_type, required=required,
                                          default=entry.get("default")))

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _invalid("steps", "must be a non-empty list of steps")
    declared = {param.name for param in parameters}
    steps: list[RecipeStep] = []
    for index, entry in enumerate(raw_steps):
        where = f"steps[{index}]"
        if not isinstance(entry, dict):
            raise _invalid(where, "must be a mapping with agent and prompt")
        _check_keys(entry, _STEP_KEYS, where)
        agent = _text(entry.get("agent"), f"{where}.agent", required=True)
        prompt = _text(entry.get("prompt"), f"{where}.prompt", required=True)
        mode = entry.get("mode", "standard")
        if mode not in MODE_CHOICES:
            raise _invalid(f"{where}.mode", f"must be one of {', '.join(MODE_CHOICES)}")
        for placeholder in _TEMPLATE_PATTERN.findall(prompt):
            if placeholder not in declared:
                raise _invalid(f"{where}.prompt",
                               f"{{{{{placeholder}}}}} is not a declared parameter")
        steps.append(RecipeStep(agent=agent, prompt=prompt, mode=mode))
    return Recipe(name=name, description=description, parameters=parameters, steps=steps)


def resolve_recipe_params(recipe: Recipe, supplied: dict[str, Any], *,
                          allow_undeclared: bool = True) -> dict[str, Any]:
    """Merge supplied parameters with recipe defaults and validate required fields.

    Refuses (:class:`RecipeError`, a ``ValueError``) a missing required
    parameter and any ``{{placeholder}}`` left without a value, so no step runs
    before a later one would fail. ``allow_undeclared=False`` (the CLI) also
    refuses a supplied parameter the recipe does not declare.
    """
    declared = [param.name for param in recipe.parameters]
    if not allow_undeclared:
        unknown = sorted(name for name in supplied if name not in declared)
        if unknown:
            raise RecipeError(
                "recipe.param_unknown",
                f"unknown parameter(s) {', '.join(unknown)}; this recipe declares "
                f"{', '.join(declared) or 'none'}")
    resolved = dict(supplied)
    for param in recipe.parameters:
        if param.name in resolved:
            continue
        if param.default is not None:
            resolved[param.name] = param.default
        elif param.required:
            raise RecipeError("recipe.param_missing",
                              f"Required recipe parameter missing: {param.name} "
                              f"(pass --param {param.name}=VALUE)")
    for index, step in enumerate(recipe.steps):
        for placeholder in _TEMPLATE_PATTERN.findall(step.prompt):
            if placeholder not in resolved:
                raise RecipeError("recipe.param_missing",
                                  f"steps[{index}].prompt uses {{{{{placeholder}}}}}, which has "
                                  f"no value (pass --param {placeholder}=VALUE)")
    return resolved


def check_recipe_agents(recipe: Recipe, workspace: str,
                        agents_dir: Path | list[Path] | None = None) -> None:
    """Refuse a step agent that does not resolve, before any step runs.

    Resolves exactly as each step will (``resolve_profile``); nothing starts.
    """
    from garuda.agents.setup import resolve_profile
    from garuda.model.config import ConfigError

    for index, step in enumerate(recipe.steps):
        try:
            resolve_profile(step.agent, workspace, agents_dir)
        except FileNotFoundError:
            raise RecipeError("recipe.agent_not_found",
                              f"steps[{index}].agent: no agent named {step.agent!r} "
                              "(`garuda agent list` shows them)") from None
        except (ConfigError, OSError, UnicodeDecodeError) as exc:
            raise _invalid(f"steps[{index}].agent", f"{step.agent!r}: {_one_line(exc)}") from None


async def run_recipe(
    recipe: Recipe,
    params: dict[str, Any],
    *,
    model: Model | str | None = None,
    reasoning_model: Model | str | None = None,
    collection_model: Model | str | None = None,
    no_collection: bool = False,
    model_binding: str | None = None,
    env: Environment,
    workspace: str,
    events: EventStore | None = None,
    agents_dir: Path | None = None,
    mcp_config_path: str | None = None,
) -> list[AgentResult]:
    """Execute each recipe step sequentially, passing context forward.

    Model overrides are scoped to this recipe run: every step resolves through
    shared setup from the same inputs, so one recipe's models never leak into
    another run. ``model`` is the legacy reasoning alias for the run.
    """
    from garuda.agents.setup import coerce_reasoning_flag
    from garuda.plugins.hooks import build_hook_registry

    events = events or EventStore()
    # Recipes must enforce the same lifecycle/safety hooks as run/chat/serve — a
    # repo's before_tool guard was previously ignored under `garuda recipe run`.
    hooks = build_hook_registry(workspace)
    resolved = resolve_recipe_params(recipe, params)
    # Fail closed once for the run, not once per step with partial work done.
    run_reasoning = coerce_reasoning_flag(model, reasoning_model)
    results: list[AgentResult] = []
    prior_context = ""

    # One workspace lease for the whole recipe (B.4): its steps edit the same
    # tree in sequence, and nobody else may edit it in between.
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard

    lease = WorkspaceLeaseGuard(str(workspace), events.session_id, capacity_key="native")
    lease.acquire()
    lease.start_heartbeat()
    try:
        return await _run_steps(
            recipe, resolved, lease, results, prior_context,
            workspace=workspace, agents_dir=agents_dir, mcp_config_path=mcp_config_path,
            run_reasoning=run_reasoning, collection_model=collection_model,
            no_collection=no_collection, model_binding=model_binding,
            env=env, events=events, hooks=hooks,
        )
    finally:
        await lease.release()


@asynccontextmanager
async def _settle_step_resources(resources):
    if resources is None:
        yield
        return
    step_failed = True
    try:
        yield
        step_failed = False
    finally:
        closing = asyncio.create_task(resources.close())
        try:
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError as cancelled:
                try:
                    await settle_task(closing)
                except Exception as exc:
                    raise cancelled from exc
                raise
        except (Exception, asyncio.CancelledError):
            if not step_failed:
                raise
            logger.warning("Closing recipe step resources failed", exc_info=True)


async def _run_steps(
    recipe, resolved, lease, results, prior_context, *, workspace, agents_dir,
    mcp_config_path, run_reasoning, collection_model, no_collection, model_binding,
    env, events, hooks,
) -> list[AgentResult]:
    for step in recipe.steps:
        prompt = render_template(step.prompt, resolved)
        if prior_context:
            prompt = f"{prompt}\n\n## Prior step output\n{prior_context}"

        prepared = await prepare_agent_run(
            step.agent,
            workspace=workspace,
            agents_dir=agents_dir,
            mcp_config_path=mcp_config_path,
            mode=step.mode,
            model=run_reasoning,
            collection_model=collection_model,
            no_collection=no_collection,
            model_binding=model_binding,
        )
        profile, config, permissions, tools, agent, mcp_manager = prepared
        async with _settle_step_resources(mcp_manager):
            config.enable_verifier = step.agent != "plan"
            config.system_prompt = resolve_system_prompt(profile, workspace)
            result = await lease.race(agent.run(
                task=prompt,
                model=prepared.reasoning,
                env=env,
                tools=tools,
                config=config,
                events=events,
                permissions=permissions,
                hooks=hooks,
                agents_dir=agents_dir,
                collection_model=prepared.collection,
                collection_policy=prepared.collection_policy,
            ))

        results.append(result)
        prior_context = result.final_message
        if not result.success:
            break

    return results
