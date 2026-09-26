"""Model factory: two providers in one run, SDK identity, safe errors."""

import pytest

from garuda.model.config import ConfigError, ModelBindings, ModelSpec
from garuda.model.factory import ModelFactory, safe_model_identity
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel


def test_two_litellm_providers_produce_two_clients():
    factory = ModelFactory()
    resolved = factory.build(
        ModelBindings(
            reasoning=ModelSpec(model="provider-a/reasoning"),
            collection=ModelSpec(model="provider-b/collector"),
        )
    )
    assert resolved.reasoning.model_name == "provider-a/reasoning"
    assert resolved.collection.model_name == "provider-b/collector"


def test_sdk_models_retained_by_identity():
    factory = ModelFactory()
    reasoning = ScriptModel([], model_name="sdk/reasoning")
    collection = ScriptModel([], model_name="sdk/collector")
    resolved = factory.build(
        ModelBindings(reasoning=ModelSpec(model="provider-a/r")),
        reasoning_model=reasoning,
        collection_model=collection,
    )
    assert resolved.reasoning is reasoning
    assert resolved.collection is collection


def test_unknown_transport_fails_actionably():
    factory = ModelFactory()
    with pytest.raises(ConfigError, match="unsupported transport"):
        factory.build(ModelBindings(reasoning=ModelSpec(transport="nope", model="x/m")))


def test_non_tool_calling_collection_rejected():
    class NoTools:
        model_name = "x/m"

        @property
        def supports_tool_calling(self):
            return False

        async def complete(self, *a, **k):
            raise AssertionError

        def count_tokens(self, messages):
            return 0

    factory = ModelFactory()
    with pytest.raises(ConfigError, match="tool calling"):
        factory.build(
            ModelBindings(reasoning=ModelSpec(model="a/m")),
            collection_model=NoTools(),
        )


def test_same_provider_shares_governor_bucket():
    from garuda.model.governor import provider_of

    assert provider_of("openrouter/a/x") == provider_of("openrouter/b/y")
    assert provider_of("provider-a/x") != provider_of("provider-b/y")


def test_identity_hides_credentials_and_query():
    model = ScriptModel([ModelResponse(content="hi", tool_calls=[])], model_name="x/m?key=secret")
    assert "secret" not in safe_model_identity(model)


def test_legacy_from_config_construction_still_works():
    from garuda.model.litellm_model import LitellmModel
    from garuda.types import AgentConfig

    config = AgentConfig(reasoning_effort="high", thinking_budget_tokens=5000)
    model = LitellmModel.from_config("provider-a/m", config)
    assert model.model_name == "provider-a/m"
    assert model._reasoning_effort == "high"
    assert model._thinking_budget_tokens == 5000


def test_no_api_key_survives_build_or_repr():
    import dataclasses

    from garuda.model.litellm_model import LitellmModel

    factory = ModelFactory()
    resolved = factory.build(ModelBindings(reasoning=ModelSpec(model="provider-a/m")))
    assert isinstance(resolved.reasoning, LitellmModel)
    assert resolved.reasoning._api_key is None
    assert "sk-" not in repr(dataclasses.astuple(resolved.bindings.reasoning))
