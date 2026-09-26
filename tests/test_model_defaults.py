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

Parsers deliberately default the model flags to None rather than this constant:
an eager built-in default would mask profile, project, and global bindings below
it. The constant is the *resolution* default (provenance `builtin`), applied in
shared setup when nothing more specific resolves.
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


def test_cli_model_flags_default_to_none_so_bindings_are_honored(monkeypatch):
    """Omitted flags stay None: the parser must not mask lower-precedence config."""
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv("GARUDA_REASONING_MODEL", raising=False)
    parser = build_parser()
    for command, extra in MODEL_SUBCOMMANDS.items():
        args = parser.parse_args([command, *extra])
        assert args.model is None, f"{command} eagerly defaults --model"
        assert args.reasoning_model is None, f"{command} eagerly defaults --reasoning-model"
        assert args.collection_model is None, f"{command} eagerly defaults --collection-model"
        assert args.no_collection is False


def test_recipe_run_model_flags_default_to_none(monkeypatch):
    """Parsed separately because `recipe` is a nested subparser with a required path."""
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv("GARUDA_REASONING_MODEL", raising=False)
    args = build_parser().parse_args(["recipe", "run", "some.yaml"])
    assert args.model is None
    assert args.reasoning_model is None
    assert args.collection_model is None


async def test_unset_flags_resolve_to_the_shared_constant(tmp_path, monkeypatch):
    """Resolution (not parsing) applies the built-in default with builtin provenance."""
    from garuda.agents.setup import prepare_agent_run

    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv("GARUDA_REASONING_MODEL", raising=False)
    monkeypatch.delenv("GARUDA_COLLECTION_MODEL", raising=False)
    prepared = await prepare_agent_run("build", workspace=str(tmp_path))
    try:
        assert prepared.bindings.reasoning.model == DEFAULT_MODEL
        assert prepared.provenance["reasoning"].provenance.value == "builtin"
        assert prepared.collection is None
    finally:
        if prepared.mcp_manager is not None:
            await prepared.mcp_manager.close()


async def test_the_env_var_overrides_the_default(tmp_path, monkeypatch):
    """GARUDA_MODEL is honored at resolution time (legacy reasoning fallback)."""
    from garuda.agents.setup import prepare_agent_run

    monkeypatch.delenv("GARUDA_REASONING_MODEL", raising=False)
    monkeypatch.setenv(MODEL_ENV_VAR, "anthropic/claude-sonnet-5")
    prepared = await prepare_agent_run("build", workspace=str(tmp_path))
    try:
        assert prepared.bindings.reasoning.model == "anthropic/claude-sonnet-5"
        assert prepared.provenance["reasoning"].provenance.value == "legacy_env"
    finally:
        if prepared.mcp_manager is not None:
            await prepared.mcp_manager.close()


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
