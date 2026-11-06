"""Source-version edit preconditions (#368)."""

import re

import pytest

from garuda.agents import spec
from garuda.agents.loader import load_profile
from garuda.tools.edit import EditTool
from garuda.tools.files import ReadFileTool
from garuda.tools.multi_edit import MultiEditTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment

VERSIONED = {"edit": {"contract": "versioned"}, "multi_edit": {"contract": "versioned"}}


def _ctx(session="s1", options=None):
    return ToolContext(session_id=session, tool_options=options or {},
                       post_edit_diagnostics=False)


async def _version(env, ctx, path):
    result = await ReadFileTool().execute({"path": path}, env, ctx)
    return result.metadata["source_version"], result.content


async def test_legacy_edits_need_no_version_and_reads_stay_quiet(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    env, ctx = LocalEnvironment(workspace_root=tmp_path), _ctx()
    token, shown = await _version(env, ctx, "a.py")
    assert token.startswith("v1:") and "version:" not in shown
    result = await EditTool().execute({"path": "a.py", "old_string": "1", "new_string": "2"}, env, ctx)
    assert not result.is_error and (tmp_path / "a.py").read_text() == "x = 2\n"


async def test_a_still_matching_string_cannot_apply_to_changed_content(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n")
    env, ctx = LocalEnvironment(workspace_root=tmp_path), _ctx()
    token, _ = await _version(env, ctx, "a.py")
    # Someone else changes the file; the old string still matches exactly once.
    (tmp_path / "a.py").write_text("# rewritten by another writer\nvalue = 1\n")
    result = await EditTool().execute({"path": "a.py", "old_string": "value = 1",
                                       "new_string": "value = 2", "expected_version": token},
                                      env, ctx)
    assert result.is_error and "not the version you read" in result.content
    assert (tmp_path / "a.py").read_text() == "# rewritten by another writer\nvalue = 1\n"


async def test_tokens_for_another_file_or_session_never_match(tmp_path):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("same = 1\n")
    env = LocalEnvironment(workspace_root=tmp_path)
    other_file, _ = await _version(env, _ctx(), "b.py")
    other_session, _ = await _version(env, _ctx(session="s2"), "a.py")
    for token in (other_file, other_session):
        result = await EditTool().execute({"path": "a.py", "old_string": "1", "new_string": "2",
                                           "expected_version": token}, env, _ctx())
        assert result.is_error
    assert (tmp_path / "a.py").read_text() == "same = 1\n"
    # The same file reached by another spelling of its path is the same version.
    token, _ = await _version(env, _ctx(), "a.py")
    result = await EditTool().execute({"path": "./a.py", "old_string": "1", "new_string": "2",
                                       "expected_version": token}, env, _ctx())
    assert not result.is_error


async def test_the_versioned_contract_requires_and_chains_versions(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\nb = 1\n")
    env, ctx = LocalEnvironment(workspace_root=tmp_path), _ctx(options=VERSIONED)
    missing = await EditTool().execute({"path": "a.py", "old_string": "a = 1", "new_string": "a = 2"},
                                       env, ctx)
    assert missing.is_error and "requires expected_version" in missing.content
    token, shown = await _version(env, ctx, "a.py")
    assert f"(version: {token})" in shown
    first = await EditTool().execute({"path": "a.py", "old_string": "a = 1", "new_string": "a = 2",
                                      "expected_version": token}, env, ctx)
    chained = re.search(r"\(version: (v1:\w+)\)", first.content)[1]
    assert chained == first.metadata["source_version"]
    stale = await EditTool().execute({"path": "a.py", "old_string": "b = 1", "new_string": "b = 2",
                                      "expected_version": token}, env, ctx)
    assert stale.is_error
    batch = await MultiEditTool().execute(
        {"path": "a.py", "expected_version": chained,
         "edits": [{"old_string": "b = 1", "new_string": "b = 3"}]}, env, ctx)
    assert not batch.is_error and (tmp_path / "a.py").read_text() == "a = 2\nb = 3\n"
    refused = await MultiEditTool().execute(
        {"path": "a.py", "edits": [{"old_string": "b = 3", "new_string": "b = 4"}]}, env, ctx)
    assert refused.is_error


def test_an_unknown_contract_refuses_the_definition(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "strict.yaml").write_text(
        "version: 1\nextends: garuda/build\ntools:\n  options:\n    edit: {contract: optimistic}\n")
    with pytest.raises(spec.AgentSpecError, match="contract"):
        load_profile("strict", extra_dir=agents)
    (agents / "strict.yaml").write_text(
        "version: 1\nextends: garuda/build\ntools:\n  options:\n    edit: {contract: versioned}\n")
    assert load_profile("strict", extra_dir=agents).tool_options == {"edit": {"contract": "versioned"}}
