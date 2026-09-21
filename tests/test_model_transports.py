"""Transport admission tests for issue #48 (P2.2).

Every registered transport proves its six admission artifacts; incomplete
records are refused; the opt-in live test spends nothing unless asked.
"""

import importlib
import os
import pathlib

import pytest

from garuda.model.config import SUPPORTED_TRANSPORTS
from garuda.model.factory import ModelFactory
from garuda.model.transports import (
    SUPPORTED_AUTH_KINDS,
    TransportRecord,
    assert_admissible,
    registry,
)


def test_registered_transports_are_fully_admitted():
    transports = registry()
    assert "litellm" in transports
    assert set(transports) == set(SUPPORTED_TRANSPORTS), (
        "config allowlist and transport registry diverged; "
        "they must share one source of truth"
    )
    for record in transports.values():
        assert_admissible(record)
        assert record.admission_gaps() == []
        assert record.auth_kind in SUPPORTED_AUTH_KINDS
        assert record.migration_notes, record.id
        module_name, _, class_name = record.test_double.rpartition(".")
        module = importlib.import_module(module_name)
        assert hasattr(module, class_name), record.test_double
        # Structural gate (auth_kind) plus fail-closed wording check.
        assert record.auth_kind == "api_key_env"
        assert "subscription" not in record.auth.lower() or "never" in record.auth.lower()
        # Integration test must exist, not merely be a non-empty string.
        nodeid = record.integration_test
        path_part, _, test_part = nodeid.partition("::")
        assert pathlib.Path(path_part).is_file(), nodeid
        if test_part:
            text = pathlib.Path(path_part).read_text(encoding="utf-8")
            assert f"def {test_part}" in text, nodeid


def test_incomplete_records_are_refused():
    record = TransportRecord(
        id="shady",
        vendor="???",
        support_citation=(),
        auth="trust me",
        capabilities=(),
        test_double="",
        integration_test="",
        cost_semantics="",
    )
    # citation, auth-kind, capabilities, double, integration test,
    # cost semantics, migration notes.
    assert len(record.admission_gaps()) == 7
    with pytest.raises(ValueError, match="admission artifacts"):
        assert_admissible(record)
    with pytest.raises(ValueError, match="https"):
        assert_admissible(
            TransportRecord(
                id="x", vendor="x", support_citation=("http://insecure",),
                auth="a", auth_kind="api_key_env",
                capabilities=("c",), test_double="d",
                integration_test="tests/test_model_transports.py::test_live_transport_opt_in",
                cost_semantics="s", migration_notes="m",
            )
        )
    with pytest.raises(ValueError, match="auth kind|admission artifacts"):
        assert_admissible(
            TransportRecord(
                id="y", vendor="y", support_citation=("https://example.com",),
                auth="a", auth_kind="subscription-oauth",
                capabilities=("c",), test_double="d",
                integration_test="tests/test_model_transports.py::test_live_transport_opt_in",
                cost_semantics="s", migration_notes="m",
            )
        )
    with pytest.raises(ValueError, match="subscription|OAuth|admission artifacts"):
        assert_admissible(
            TransportRecord(
                id="z", vendor="z", support_citation=("https://example.com",),
                auth="subscription OAuth via device flow",
                auth_kind="api_key_env",
                capabilities=("c",), test_double="d",
                integration_test="tests/test_model_transports.py::test_live_transport_opt_in",
                cost_semantics="s", migration_notes="m",
            )
        )
    with pytest.raises(ValueError, match="does not exist|not found"):
        assert_admissible(
            TransportRecord(
                id="w", vendor="w", support_citation=("https://example.com",),
                auth="api keys via env; never subscription",
                auth_kind="api_key_env",
                capabilities=("c",), test_double="d",
                integration_test="tests/test_no_such_file.py::test_missing",
                cost_semantics="s", migration_notes="m",
            )
        )


def test_factory_refuses_unadmitted_transports():
    from garuda.model.config import ConfigError, ModelBindings, ModelSpec

    factory = ModelFactory()
    # Unknown transport: no admission record.
    with pytest.raises(ConfigError, match="no admission record|unsupported transport"):
        factory.build_spec(ModelSpec(transport="nope", model="x/m"), role="reasoning")
    # Registration without a record fails closed.
    with pytest.raises(ConfigError, match="no admission record"):
        from garuda.model.factory import register_transport

        register_transport("ghost", lambda spec: object())
    # Known transport builds (admission passes at build time).
    built = factory.build_spec(ModelSpec(transport="litellm", model="x/m"), role="reasoning")
    assert built is not None


def test_citations_and_cost_rules():
    for record in registry().values():
        assert all(c.startswith("https://") for c in record.support_citation)
        assert "unknown" in record.cost_semantics.lower() or "Unknown" in record.cost_semantics


@pytest.mark.skipif(
    not os.environ.get("GARUDA_LIVE_MODEL"), reason="set GARUDA_LIVE_MODEL to run"
)
async def test_live_transport_opt_in():
    """One tiny completion against the named model. Opt-in spend only."""
    from garuda.model.litellm_model import LitellmModel
    from garuda.types import Message, Role

    model = LitellmModel(model_name=os.environ["GARUDA_LIVE_MODEL"])
    response = await model.complete(
        [Message(role=Role.USER, content="Reply with exactly: ok")],
        max_tokens=16,
    )
    assert response is not None
