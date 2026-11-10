"""Source-version edit preconditions (#368)."""

import re

import pytest

from garuda.agents import spec
from garuda.agents.loader import load_profile
from garuda.tools.edit import EditTool
from garuda.tools.files import ReadFileTool
from garuda.tools.multi_edit import MultiEditTool
from garuda.tools.protocol import ToolContext
from garuda.tools.source_version import version_token
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


@pytest.mark.parametrize("tool", ["edit", "multi_edit"])
async def test_a_chained_version_is_what_the_next_read_reports(tmp_path, tool):
    # Writing "\r\n" or "\r" reads back as "\n", so the token must name the re-read text.
    (tmp_path / "b.txt").write_text("one\ntwo\n")
    env, ctx = LocalEnvironment(workspace_root=tmp_path), _ctx(options=VERSIONED)
    token, _ = await _version(env, ctx, "b.txt")
    change = {"old_string": "one", "new_string": "1\r\n1b\r1c"}
    if tool == "edit":
        first = await EditTool().execute({"path": "b.txt", **change, "expected_version": token}, env, ctx)
    else:
        first = await MultiEditTool().execute(
            {"path": "b.txt", "edits": [change], "expected_version": token}, env, ctx)
    assert not first.is_error
    chained = first.metadata["source_version"]
    assert f"(version: {chained})" in first.content
    assert chained == (await _version(env, ctx, "b.txt"))[0]
    second = await EditTool().execute({"path": "b.txt", "old_string": "two", "new_string": "2",
                                       "expected_version": chained}, env, ctx)
    assert not second.is_error


async def test_an_edit_read_back_as_other_content_reports_an_unknown_version(tmp_path):
    class Overwritten(LocalEnvironment):
        async def write_file(self, path, content):
            await super().write_file(path, "# another writer\n" + content)

    (tmp_path / "a.py").write_text("a = 1\n")
    env, ctx = Overwritten(workspace_root=tmp_path), _ctx(options=VERSIONED)
    token, _ = await _version(env, ctx, "a.py")
    result = await EditTool().execute({"path": "a.py", "old_string": "a = 1", "new_string": "a = 2",
                                       "expected_version": token}, env, ctx)
    assert not result.is_error
    assert result.metadata["source_version"] is None
    assert "(version: unknown" in result.content


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


# Bounded reads (#365) serve local read_file through a page that never holds the
# whole text; the version must still name the FULL content an edit compares.


async def test_versioned_edit_after_a_local_paged_read_succeeds(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\nb = 1\nc = 1\n")
    env, ctx = LocalEnvironment(workspace_root=tmp_path), _ctx(options=VERSIONED)
    read = await ReadFileTool().execute({"path": "a.py", "offset": 2, "limit": 1}, env, ctx)
    assert read.metadata["bounded_read"] is True and "2\tb = 1" in read.content
    token = read.metadata["source_version"]
    assert f"(version: {token})" in read.content
    result = await EditTool().execute({"path": "a.py", "old_string": "b = 1", "new_string": "b = 2",
                                       "expected_version": token}, env, ctx)
    assert not result.is_error, result.content
    batch = await MultiEditTool().execute(
        {"path": "a.py", "expected_version": result.metadata["source_version"],
         "edits": [{"old_string": "c = 1", "new_string": "c = 3"}]}, env, ctx)
    assert not batch.is_error and (tmp_path / "a.py").read_text() == "a = 1\nb = 2\nc = 3\n"


async def test_a_large_page_reports_the_full_content_version(tmp_path, monkeypatch):
    import garuda.workspace.text_reads as reads

    # Small blocks put CRLF pairs, lone CRs and multi-byte characters across
    # block boundaries; the digest must follow read_file's universal newlines.
    monkeypatch.setattr(reads, "SCAN_BLOCK_BYTES", 7)
    body = "".join(f"line {i} é\r\n" if i % 3 else f"line {i}\r" for i in range(4000))
    path = tmp_path / "big.txt"
    path.write_bytes((body + "target = 1\n").encode("utf-8"))
    env, ctx = LocalEnvironment(workspace_root=tmp_path), _ctx(options=VERSIONED)
    read = await ReadFileTool().execute({"path": "big.txt", "offset": 10, "limit": 2}, env, ctx)
    assert not read.is_error and read.metadata["bounded_read"] is True
    token = read.metadata["source_version"]
    assert token == version_token(ctx, env, "big.txt", await env.read_file("big.txt"))
    result = await EditTool().execute({"path": "big.txt", "old_string": "target = 1",
                                       "new_string": "target = 2", "expected_version": token},
                                      env, ctx)
    assert not result.is_error, result.content


async def test_legacy_large_pages_stay_bounded_and_never_report_a_page_version(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("x = 1\n" * 400000)  # several scan blocks
    env, ctx = LocalEnvironment(workspace_root=tmp_path), _ctx()
    read = await ReadFileTool().execute({"path": "big.txt", "offset": 1, "limit": 2}, env, ctx)
    assert read.metadata["bytes_read"] < path.stat().st_size
    assert read.metadata["source_version"] is None and "version" not in read.content


async def test_a_bounded_tail_reports_an_unknown_version_that_edits_refuse(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("x = 1\n" * 400000)  # past the exact-tail rescan threshold
    env, ctx = LocalEnvironment(workspace_root=tmp_path), _ctx(options=VERSIONED)
    read = await ReadFileTool().execute({"path": "big.txt", "tail": True, "limit": 2}, env, ctx)
    assert not read.is_error and read.metadata["bytes_read"] < path.stat().st_size
    assert read.metadata["source_version"] is None
    assert "version: unknown" in read.content
    before = path.read_bytes()
    for expected in ("unknown", "sha256:" + "0" * 64):
        refused = await EditTool().execute({"path": "big.txt", "old_string": "x = 1\nx = 1\n",
                                            "new_string": "y\n", "expected_version": expected},
                                           env, ctx)
        assert refused.is_error and "version unknown" in refused.content
    assert path.read_bytes() == before


async def test_an_outline_under_the_versioned_contract_reads_the_full_source(tmp_path):
    (tmp_path / "big.py").write_text("x = 0\n" * 3000 + "def late():\n    return 1\n")
    env, ctx = LocalEnvironment(workspace_root=tmp_path), _ctx(options=VERSIONED)
    result = await ReadFileTool().execute({"path": "big.py", "outline": True}, env, ctx)
    assert not result.is_error and "late" in result.content


class _WholeReads(LocalEnvironment):
    """A backend without the optional bounded page reader (as Docker/remote)."""

    async def read_text_page(self, path, **kwargs):
        raise NotImplementedError("Bounded text reads are not supported by this backend")


@pytest.mark.parametrize("backend", [LocalEnvironment, _WholeReads], ids=["paged", "whole-read"])
async def test_an_empty_file_reports_its_version_under_the_versioned_contract(tmp_path, backend):
    path = tmp_path / "empty.py"
    path.write_text("")
    env, ctx = backend(workspace_root=tmp_path), _ctx(options=VERSIONED)
    read = await ReadFileTool().execute({"path": "empty.py"}, env, ctx)
    assert not read.is_error and read.content.startswith("(empty file)")
    token = read.metadata["source_version"]
    assert token == version_token(ctx, env, "empty.py", "")
    assert f"(version: {token})" in read.content
    # The token names the empty content: once another writer fills the file it is stale.
    path.write_text("value = 1\n")
    refused = await EditTool().execute({"path": "empty.py", "old_string": "value = 1",
                                        "new_string": "value = 2", "expected_version": token},
                                       env, ctx)
    assert refused.is_error and "not the version you read" in refused.content
    assert path.read_text() == "value = 1\n"


@pytest.mark.parametrize("tool", ["edit", "multi_edit"])
async def test_a_versioned_edit_result_carries_diagnostics_and_its_version(tmp_path, tool):
    # Post-edit diagnostics (#369) and the chained version (#368) share one result.
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="s1", tool_options=VERSIONED)
    token, _ = await _version(env, ctx, "app.py")
    change = {"old_string": "return 1", "new_string": "return ("}
    if tool == "edit":
        result = await EditTool().execute({"path": "app.py", **change, "expected_version": token},
                                          env, ctx)
    else:
        result = await MultiEditTool().execute(
            {"path": "app.py", "edits": [change], "expected_version": token}, env, ctx)
    assert not result.is_error and "Syntax check failed" in result.content
    syntax = result.metadata["diagnostics"]["checkers"]["syntax"]
    assert syntax["status"] == "violations"
    assert syntax["diagnostics"][0]["attribution"] == "introduced"
    chained = result.metadata["source_version"]
    assert chained == (await _version(env, ctx, "app.py"))[0]
    assert f"(version: {chained})" in result.content
