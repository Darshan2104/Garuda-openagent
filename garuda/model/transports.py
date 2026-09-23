"""Direct model transport registry (P2.2, issue #48).

A direct transport joins this registry only after its vendor publishes the
supported public authentication and the request/session semantics Garuda
needs — no private endpoints, no reverse-engineered subscription paths, no
copied tokens, ever. Each record carries the six admission artifacts: a
vendor-support citation, a capability declaration, a test double, an opt-in
integration test, cost semantics, and migration notes. `admission_gaps`
refuses incomplete entries so a half-documented transport cannot slip in.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
from dataclasses import dataclass

#: Structural auth kinds a direct transport may declare. Free-text `auth`
#: remains as human guidance, but admission gates on this enum — a wording
#: tweak ("subscription OAuth, never audited") must not satisfy the gate.
SUPPORTED_AUTH_KINDS = frozenset({"api_key_env"})


@dataclass(frozen=True)
class TransportRecord:
    id: str
    vendor: str
    support_citation: tuple[str, ...]
    auth: str
    auth_kind: str = ""
    capabilities: tuple[str, ...] = ()
    test_double: str = ""
    integration_test: str = ""
    cost_semantics: str = ""
    migration_notes: str = ""

    def admission_gaps(self) -> list[str]:
        """Missing admission artifacts. Empty means admissible."""
        gaps = []
        if not self.support_citation:
            gaps.append("vendor-support citation")
        if not self.auth:
            gaps.append("auth description")
        if self.auth_kind not in SUPPORTED_AUTH_KINDS:
            gaps.append(f"auth kind {sorted(SUPPORTED_AUTH_KINDS)}")
        if not self.capabilities:
            gaps.append("capability declaration")
        if not self.test_double:
            gaps.append("test double")
        if not self.integration_test:
            gaps.append("opt-in integration test")
        if not self.cost_semantics:
            gaps.append("cost semantics")
        if not self.migration_notes:
            gaps.append("migration notes")
        return gaps

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "vendor": self.vendor,
            "support_citation": list(self.support_citation),
            "auth": self.auth,
            "auth_kind": self.auth_kind,
            "capabilities": list(self.capabilities),
            "test_double": self.test_double,
            "integration_test": self.integration_test,
            "cost_semantics": self.cost_semantics,
            "migration_notes": self.migration_notes,
        }


def _repo_root() -> pathlib.Path:
    # garuda/model/transports.py -> repo root (garuda/model -> garuda -> root).
    return pathlib.Path(__file__).resolve().parents[2]


def _assert_integration_test_exists(record: TransportRecord) -> None:
    """The named integration test must exist, not merely be a non-empty string.

    Accepts ``path/to/test.py::test_name`` (preferred) or a bare path. Fails
    closed on typos, missing files, and non-collectable nodeids.
    """
    nodeid = record.integration_test
    if "::" in nodeid:
        path_part, _, test_part = nodeid.partition("::")
    else:
        path_part, test_part = nodeid, ""
    candidate = pathlib.Path(path_part)
    if not candidate.is_absolute():
        candidate = _repo_root() / path_part
    if not candidate.is_file():
        raise ValueError(
            f"transport {record.id!r} integration test does not exist: {nodeid!r}"
        )
    if test_part:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"transport {record.id!r} cannot read integration test {nodeid!r}: {exc}"
            ) from exc
        try:
            tree = ast.parse(text, filename=str(candidate))
        except SyntaxError as exc:
            raise ValueError(
                f"transport {record.id!r} integration test {path_part!r} "
                f"does not parse: {exc}"
            ) from exc
        # A real function node: substring search accepts the name in a comment.
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if test_part not in defined:
            raise ValueError(
                f"transport {record.id!r} integration test {test_part!r} "
                f"is not a defined function in {path_part!r}"
            )


def _assert_test_double_resolves(record: TransportRecord) -> None:
    """The configured test double must import and resolve to something
    buildable — a dotted path pointing at nothing is not a test double."""
    dotted = record.test_double
    module_name, _, attr = dotted.rpartition(".")
    if not module_name or not attr:
        raise ValueError(
            f"transport {record.id!r} test double must be a dotted path: {dotted!r}"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(
            f"transport {record.id!r} test double module {module_name!r} "
            f"does not import: {exc}"
        ) from exc
    target: object = module
    for part in dotted[len(module_name) + 1 :].split("."):
        try:
            target = getattr(target, part)
        except AttributeError as exc:
            raise ValueError(
                f"transport {record.id!r} test double {dotted!r} "
                f"does not resolve: {exc}"
            ) from exc
    if not callable(target):
        raise ValueError(
            f"transport {record.id!r} test double {dotted!r} is not buildable"
        )


TRANSPORTS: tuple[TransportRecord, ...] = (
    TransportRecord(
        id="litellm",
        vendor="LiteLLM (multi-provider, e.g. OpenRouter, Anthropic, OpenAI)",
        support_citation=(
            "https://docs.litellm.ai/",
            "https://openrouter.ai/docs",
        ),
        auth="Provider API keys via environment (e.g. OPENROUTER_API_KEY); "
        "never subscription OAuth material.",
        auth_kind="api_key_env",
        capabilities=("streaming", "tool-calling", "reasoning", "prompt-caching", "retries"),
        test_double="garuda.model.script_model.ScriptModel",
        integration_test="tests/test_model_transports.py::test_live_transport_opt_in",
        cost_semantics="Provider-reported cost first, then explicit overrides, "
        "then the versioned in-repo price snapshot; unknown stays unknown.",
        migration_notes="Default transport since v1. New transports add a record "
        "here; nothing migrates away unless the vendor withdraws support.",
    ),
)


def registry() -> dict[str, TransportRecord]:
    return {record.id: record for record in TRANSPORTS}


def assert_admissible(record: TransportRecord) -> None:
    gaps = record.admission_gaps()
    if gaps:
        raise ValueError(
            f"transport {record.id!r} is missing admission artifacts: {', '.join(gaps)}"
        )
    for citation in record.support_citation:
        if not citation.startswith("https://"):
            raise ValueError(f"transport {record.id!r} citation must be https: {citation!r}")
    if record.auth_kind not in SUPPORTED_AUTH_KINDS:
        raise ValueError(
            f"transport {record.id!r} has unsupported auth kind {record.auth_kind!r} "
            f"(supported: {sorted(SUPPORTED_AUTH_KINDS)})"
        )
    lowered = record.auth.lower()
    if ("subscription" in lowered or "oauth" in lowered) and "never" not in lowered:
        raise ValueError(
            f"transport {record.id!r} auth must never claim subscription/OAuth use: {record.auth!r}"
        )
    _assert_integration_test_exists(record)
    _assert_test_double_resolves(record)
