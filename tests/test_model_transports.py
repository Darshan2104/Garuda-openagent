"""Transport admission tests for issue #48 (P2.2).

Every registered transport proves its six admission artifacts; incomplete
records are refused; the opt-in live test spends nothing unless asked.
"""

import importlib
import os

import pytest

from garuda.model.transports import (
    TransportRecord,
    assert_admissible,
    registry,
)


def test_registered_transports_are_fully_admitted():
    transports = registry()
    assert "litellm" in transports
    for record in transports.values():
        assert_admissible(record)
        assert record.admission_gaps() == []
        module_name, _, class_name = record.test_double.rpartition(".")
        module = importlib.import_module(module_name)
        assert hasattr(module, class_name), record.test_double
        assert "subscription" not in record.auth.lower() or "never" in record.auth.lower()


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
    assert len(record.admission_gaps()) == 5
    with pytest.raises(ValueError, match="admission artifacts"):
        assert_admissible(record)
    with pytest.raises(ValueError, match="https"):
        assert_admissible(
            TransportRecord(
                id="x", vendor="x", support_citation=("http://insecure",),
                auth="a", capabilities=("c",), test_double="d",
                integration_test="t", cost_semantics="s",
            )
        )


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
