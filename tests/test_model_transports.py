"""Transport admission tests for issue #48 (P2.2).

Every registered transport proves its six admission artifacts; incomplete
records are refused; the opt-in live test spends nothing unless asked.
"""

import importlib
import json
import os
import pathlib
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from garuda.model.config import SUPPORTED_TRANSPORTS
from garuda.model.factory import ModelFactory
from garuda.model.transports import (
    SUPPORTED_AUTH_KINDS,
    TransportRecord,
    assert_admissible,
    assert_repository_admission,
    registry,
)
from garuda.types import Message, Role


@contextmanager
def _openai_tool_endpoint():
    requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            tool_call = {
                "index": 0,
                "id": "call_local",
                "type": "function",
                "function": {"name": "local_tool", "arguments": '{"value":7}'},
            }
            if request.get("stream"):
                chunks = [
                    {
                        "id": "local-stream",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "transport-fixture",
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "tool_calls": [tool_call]},
                            "finish_reason": None,
                        }],
                    },
                    {
                        "id": "local-stream",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "transport-fixture",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                ]
                raw = b"".join(
                    f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks
                ) + b"data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                raw = json.dumps({
                    "id": "local-completion",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "transport-fixture",
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [tool_call],
                        },
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
async def test_litellm_transports_function_schemas_without_owning_tools(stream):
    """Ordinary tools cross real LiteLLM HTTP while Garuda retains execution."""
    from garuda.model.litellm_model import LitellmModel

    tools = [{
        "type": "function",
        "function": {
            "name": "local_tool",
            "description": "Return a local value",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }]
    with _openai_tool_endpoint() as (server, requests):
        model = LitellmModel(
            "openai/transport-fixture",
            api_key="local-protocol-fixture",
            api_base=f"http://127.0.0.1:{server.server_port}/v1",
            max_retries=1,
        )
        messages = [Message(role=Role.USER, content="Call the local tool")]
        if stream:
            response = await model.complete_streaming(messages, tools=tools)
        else:
            response = await model.complete(messages, tools=tools)

    assert len(requests) == 1
    assert requests[0].get("stream", False) is stream
    assert "_skip_mcp_handler" not in requests[0]
    assert requests[0]["tools"] == tools
    assert requests[0]["tool_choice"] == "auto"
    assert response.content in (None, "")
    assert [(call.id, call.name, call.arguments) for call in response.tool_calls] == [
        ("call_local", "local_tool", {"value": 7})
    ]


def test_registered_transports_are_fully_admitted():
    transports = registry()
    assert "litellm" in transports
    assert set(transports) == set(SUPPORTED_TRANSPORTS), (
        "config allowlist and transport registry diverged; "
        "they must share one source of truth"
    )
    for record in transports.values():
        assert_repository_admission(record)
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
                auth="a", auth_kind="subscription-account",
                capabilities=("c",), test_double="d",
                integration_test="tests/test_model_transports.py::test_live_transport_opt_in",
                cost_semantics="s", migration_notes="m",
            )
        )
    with pytest.raises(ValueError, match="subscription|admission artifacts"):
        assert_admissible(
            TransportRecord(
                id="z", vendor="z", support_citation=("https://example.com",),
                auth="subscription account via device flow",
                auth_kind="api_key_env",
                capabilities=("c",), test_double="d",
                integration_test="tests/test_model_transports.py::test_live_transport_opt_in",
                cost_semantics="s", migration_notes="m",
            )
        )
    with pytest.raises(ValueError, match="does not exist|not found"):
        assert_repository_admission(
            TransportRecord(
                id="w", vendor="w", support_citation=("https://example.com",),
                auth="api keys via env; never subscription",
                auth_kind="api_key_env",
                capabilities=("c",), test_double="garuda.model.script_model.ScriptModel",
                integration_test="tests/test_no_such_file.py::test_missing",
                cost_semantics="s", migration_notes="m",
            )
        )


def test_integration_test_must_define_a_real_function(tmp_path):
    import dataclasses

    (valid,) = [r for r in registry().values() if r.id == "litellm"]
    planted = tmp_path / "test_planted.py"
    planted.write_text(
        "# def test_live_transport_opt_in lives only in this comment\n"
        "def test_something_else():\n    pass\n",
        encoding="utf-8",
    )
    from garuda.model.transports import _assert_integration_test_exists

    with pytest.raises(ValueError, match="not a defined function"):
        _assert_integration_test_exists(
            dataclasses.replace(
                valid, id="planted",
                integration_test=f"{planted}::test_live_transport_opt_in",
            )
        )


def test_test_double_must_import_and_resolve():
    import dataclasses

    (valid,) = [r for r in registry().values() if r.id == "litellm"]
    for bad_double in (
        "not-a-dotted-path",
        "garuda.no_such_module.ScriptModel",
        "garuda.model.script_model.NoSuchClass",
    ):
        with pytest.raises(ValueError, match="test double"):
            assert_admissible(dataclasses.replace(valid, id="bad", test_double=bad_double))
    with pytest.raises(ValueError, match="not buildable"):
        assert_admissible(
            dataclasses.replace(
                valid, id="bad", test_double="garuda.model.transports.TRANSPORTS"
            )
        )
    from garuda.model.config import ConfigError, ModelSpec

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


@pytest.mark.parametrize(
    "nodeid",
    ["/abs/tests/test_x.py::test_y", "tests/../../etc/x.py::t", "tests/test_x.txt", "tests/test_x.py::1bad"],
)
def test_runtime_admission_still_refuses_a_malformed_integration_test(nodeid):
    import dataclasses

    (valid,) = [r for r in registry().values() if r.id == "litellm"]
    with pytest.raises(ValueError, match="relative"):
        assert_admissible(dataclasses.replace(valid, id="bad", integration_test=nodeid))


def test_runtime_admission_does_not_read_the_source_checkout(tmp_path, monkeypatch):
    """Import-time admission must work where there is no ``tests/`` directory (#145)."""
    import dataclasses

    from garuda.model import transports

    (valid,) = [r for r in registry().values() if r.id == "litellm"]
    monkeypatch.setattr(transports, "_repo_root", lambda: tmp_path)
    assert_admissible(valid)
    with pytest.raises(ValueError, match="does not exist"):
        assert_repository_admission(dataclasses.replace(valid, id="moved"))
