"""YAML recipe loader and multi-step workflow runner."""

import re
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

_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


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


def load_recipe(path: str | Path) -> Recipe:
    """Load a recipe YAML file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    parameters = [
        RecipeParameter(
            name=entry["name"],
            type=entry.get("type", "string"),
            required=entry.get("required", False),
            default=entry.get("default"),
        )
        for entry in data.get("parameters", [])
    ]
    steps = [
        RecipeStep(
            agent=step["agent"],
            prompt=step["prompt"],
            mode=step.get("mode", "standard"),
        )
        for step in data.get("steps", [])
    ]
    return Recipe(
        name=data.get("name", Path(path).stem),
        description=data.get("description", ""),
        parameters=parameters,
        steps=steps,
    )


def resolve_recipe_params(recipe: Recipe, supplied: dict[str, Any]) -> dict[str, Any]:
    """Merge supplied parameters with recipe defaults and validate required fields."""
    resolved = dict(supplied)
    for param in recipe.parameters:
        if param.name in resolved:
            continue
        if param.default is not None:
            resolved[param.name] = param.default
        elif param.required:
            raise ValueError(f"Required recipe parameter missing: {param.name}")
    return resolved


async def run_recipe(
    recipe: Recipe,
    params: dict[str, Any],
    *,
    model: Model,
    env: Environment,
    workspace: str,
    events: EventStore | None = None,
    agents_dir: Path | None = None,
    mcp_config_path: str | None = None,
) -> list[AgentResult]:
    """Execute each recipe step sequentially, passing context forward."""
    from garuda.plugins.hooks import build_hook_registry

    events = events or EventStore()
    # Recipes must enforce the same lifecycle/safety hooks as run/chat/serve — a
    # repo's before_tool guard was previously ignored under `garuda recipe run`.
    hooks = build_hook_registry(workspace)
    resolved = resolve_recipe_params(recipe, params)
    results: list[AgentResult] = []
    prior_context = ""

    for index, step in enumerate(recipe.steps, start=1):
        prompt = render_template(step.prompt, resolved)
        if prior_context:
            prompt = f"{prompt}\n\n## Prior step output\n{prior_context}"

        profile, config, permissions, tools, agent, mcp_manager = await prepare_agent_run(
            step.agent,
            workspace=workspace,
            agents_dir=agents_dir,
            mcp_config_path=mcp_config_path,
            mode=step.mode,
        )
        config.enable_verifier = step.agent != "plan"
        config.system_prompt = resolve_system_prompt(profile, workspace)

        result = await agent.run(
            task=prompt,
            model=model,
            env=env,
            tools=tools,
            config=config,
            events=events,
            permissions=permissions,
            hooks=hooks,
            agents_dir=agents_dir,
        )
        if mcp_manager is not None:
            await mcp_manager.close()

        results.append(result)
        prior_context = result.final_message
        if not result.success:
            break

    return results
