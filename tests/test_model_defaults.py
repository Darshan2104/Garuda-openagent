"""Every entry point defaults to the same model, and it is an OpenRouter one.

The string was copied into seven places — `garuda run`, `chat`, `serve`, `recipe run`,
`ServerConfig`, `SoftwareAgent` and `Conversation` — so changing "the default model"
meant changing seven literals and a review had no way to notice the one that was
missed. A CLI and an SDK disagreeing about the default is the kind of drift that only
shows up as a surprise bill or a 401 from a provider the user never chose.

The OpenRouter prefix is load-bearing, not cosmetic: `_apply_usage_accounting` only
asks for real per-call costs on `openrouter/` models, so a default that lost the
prefix would silently downgrade every default run's cost figure from the provider's
own number to a pricing-table estimate.
"""

import inspect

from garuda.interfaces.main import build_parser
from garuda.interfaces.server import ServerConfig
from garuda.model.litellm_model import LitellmModel
from garuda.model.protocol import DEFAULT_MODEL, MODEL_ENV_VAR
from garuda.sdk.conversation import Conversation
from garuda.sdk.software_agent import SoftwareAgent

# Subcommands that take --model, with the extra args each needs to parse.
MODEL_SUBCOMMANDS = {
    "run": ["-t", "task"],
    "chat": [],
    "serve": [],
}


def test_default_model_is_routed_through_openrouter():
    """The prefix is what turns on exact cost accounting — see the module docstring."""
    assert DEFAULT_MODEL.startswith("openrouter/")
    # Two slashes: openrouter/<vendor>/<model>. A bare `openrouter/x` would be a
    # model id OpenRouter does not serve.
    assert DEFAULT_MODEL.count("/") == 2, DEFAULT_MODEL


def test_every_cli_subcommand_defaults_to_the_shared_constant(monkeypatch):
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    parser = build_parser()
    for command, extra in MODEL_SUBCOMMANDS.items():
        args = parser.parse_args([command, *extra])
        assert args.model == DEFAULT_MODEL, f"{command} disagrees"


def test_recipe_run_defaults_to_the_shared_constant(monkeypatch):
    """Parsed separately because `recipe` is a nested subparser with a required path."""
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    args = build_parser().parse_args(["recipe", "run", "some.yaml"])
    assert args.model == DEFAULT_MODEL


def test_the_env_var_overrides_the_default(monkeypatch):
    monkeypatch.setenv(MODEL_ENV_VAR, "anthropic/claude-sonnet-5")
    # Read at parser-build time, so the parser has to be rebuilt under the new env.
    args = build_parser().parse_args(["run", "-t", "task"])
    assert args.model == "anthropic/claude-sonnet-5"


def test_server_and_sdk_agree_with_the_cli():
    assert ServerConfig().model == DEFAULT_MODEL
    for cls in (SoftwareAgent, Conversation):
        default = inspect.signature(cls.__init__).parameters["model"].default
        assert default == DEFAULT_MODEL, f"{cls.__name__} disagrees"


def test_the_default_model_asks_openrouter_for_its_real_cost():
    """Ties the constant to the behaviour that motivates the prefix.

    `usage.include` is what makes `usage["cost_usd"]` the provider's own invoice,
    which `eval/costs.py` prefers over every pricing table — and why the snapshot
    deliberately carries no rate for this model.
    """
    kwargs: dict = {}
    LitellmModel(model_name=DEFAULT_MODEL)._apply_usage_accounting(kwargs)
    assert kwargs["extra_body"]["usage"] == {"include": True}

    # The negative case, so the assertion above is not vacuous.
    other: dict = {}
    LitellmModel(model_name="openai/gpt-4o-mini")._apply_usage_accounting(other)
    assert other == {}
