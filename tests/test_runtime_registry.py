"""Registry tests for issue #12 (P0.5).

Precedence, malformed files, allowlist denial, inspection, per-instance
isolation, and the guarantee that configuration carries no secrets.
"""

import pytest

from garuda.runtime import RegistryError, RuntimeKind
from garuda.runtime.registry import (
    BUILTIN_NATIVE_ID,
    RuntimeRegistry,
    parse_global_manifests,
    parse_project_refs,
)


def _global(*items: dict) -> list:
    return parse_global_manifests(list(items))


def test_builtin_native_is_always_present():
    registry = RuntimeRegistry()
    resolved = registry.default()
    assert resolved.runtime_id == BUILTIN_NATIVE_ID
    assert resolved.kind is RuntimeKind.NATIVE
    assert resolved.command is None


def test_global_manifest_resolves_with_command_and_capabilities():
    manifests = _global(
        {
            "runtime_id": "codex",
            "kind": "acp",
            "command": ["codex", "acp"],
            "version": "1.2.3",
            "capabilities": ["prompt", "cancel"],
            "description": "Codex ACP adapter.",
        }
    )
    resolved = RuntimeRegistry(manifests).get("codex")
    assert resolved.command == ("codex", "acp")
    assert resolved.version == "1.2.3"
    assert resolved.capabilities.supports("prompt")
    assert "Codex" in resolved.description


def test_project_alias_narrows_but_never_widens():
    manifests = _global(
        {
            "runtime_id": "codex",
            "kind": "acp",
            "command": ["codex", "acp"],
            "version": "1",
            "capabilities": ["prompt", "cancel"],
        }
    )
    refs = parse_project_refs(
        [{"alias": "fast", "runtime_id": "codex", "capabilities": ["prompt"]}]
    )
    resolved = RuntimeRegistry(manifests, refs).get("fast")
    assert resolved.runtime_id == "codex"
    assert resolved.via_alias == "fast"
    assert resolved.capabilities.supports("prompt")
    assert not resolved.capabilities.supports("cancel")

    with pytest.raises(RegistryError, match="widens"):
        RuntimeRegistry(
            manifests,
            parse_project_refs(
                [
                    {
                        "alias": "greedy",
                        "runtime_id": "codex",
                        "capabilities": ["prompt", "root-access"],
                    }
                ]
            ),
        )


def test_project_command_is_self_authorization_and_fails():
    with pytest.raises(RegistryError, match="cannot authorize an executable"):
        parse_project_refs([{"alias": "evil", "runtime_id": "native", "command": ["rm"]}])
    with pytest.raises(RegistryError, match="unknown runtime"):
        RuntimeRegistry([], parse_project_refs([{"alias": "ghost", "runtime_id": "nope"}]))


@pytest.mark.parametrize(
    "manifest",
    [
        {"kind": "acp", "command": ["x"], "version": "1"},
        {"runtime_id": "", "kind": "acp", "command": ["x"], "version": "1"},
        {"runtime_id": "x", "kind": "http", "command": ["x"], "version": "1"},
        {"runtime_id": "x", "kind": "acp", "version": "1"},
        {"runtime_id": "x", "kind": "acp", "command": [], "version": "1"},
        {"runtime_id": "x", "kind": "native", "command": ["x"], "version": "1"},
        {"runtime_id": "x", "kind": "acp", "command": ["x"], "version": "1", "bogus": 1},
    ],
)
def test_malformed_global_manifests_fail_closed(manifest):
    with pytest.raises(RegistryError):
        parse_global_manifests([manifest])


def test_duplicate_ids_and_aliases_fail():
    manifests = _global(
        {"runtime_id": "a", "kind": "acp", "command": ["a"], "version": "1"},
        {"runtime_id": "a", "kind": "acp", "command": ["a"], "version": "1"},
    )
    with pytest.raises(RegistryError, match="duplicate runtime_id"):
        RuntimeRegistry(manifests)
    good = _global({"runtime_id": "a", "kind": "acp", "command": ["a"], "version": "1"})
    with pytest.raises(RegistryError, match="duplicate runtime alias"):
        RuntimeRegistry(
            good,
            parse_project_refs(
                [
                    {"alias": "x", "runtime_id": "a"},
                    {"alias": "x", "runtime_id": "a"},
                ]
            ),
        )


@pytest.mark.parametrize("key", ["env", "environment", "token", "api_key", "secrets", "credentials"])
def test_secret_carrying_keys_are_rejected(key):
    with pytest.raises(RegistryError, match="forbidden"):
        parse_global_manifests(
            [
                {
                    "runtime_id": "x",
                    "kind": "acp",
                    "command": ["x"],
                    "version": "1",
                    key: {"ANYTHING": "1"},
                }
            ]
        )


def test_resolved_descriptors_carry_no_secret_surface():
    resolved = RuntimeRegistry().default()
    assert not hasattr(resolved, "env")
    assert "token" not in resolved.__dict__


def test_registries_are_per_instance_with_no_shared_state():
    first = RuntimeRegistry(
        _global({"runtime_id": "a", "kind": "acp", "command": ["v1"], "version": "1"})
    )
    second = RuntimeRegistry(
        _global({"runtime_id": "a", "kind": "acp", "command": ["v2"], "version": "2"})
    )
    assert first.get("a").command == ("v1",)
    assert second.get("a").command == ("v2",)


def test_inspection_lists_everything_deterministically():
    manifests = _global(
        {"runtime_id": "b", "kind": "acp", "command": ["b"], "version": "1"},
        {"runtime_id": "a", "kind": "acp", "command": ["a"], "version": "1"},
    )
    refs = parse_project_refs([{"alias": "quick", "runtime_id": "a"}])
    listed = [r.via_alias or r.runtime_id for r in RuntimeRegistry(manifests, refs).list()]
    assert listed == ["a", "b", "native", "quick"]


def test_unknown_refs_fail_closed():
    registry = RuntimeRegistry()
    with pytest.raises(RegistryError, match="unknown runtime"):
        registry.get("does-not-exist")
