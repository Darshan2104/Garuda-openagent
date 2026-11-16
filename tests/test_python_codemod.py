"""Public preview/permission/apply contract against real workspace files."""

import asyncio
import hashlib
import json

import pytest

from garuda.core.permissions import PermissionEngine
from garuda.tools import build_toolkit
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment


@pytest.fixture
async def codemod(tmp_path):
    tools, _ = await build_toolkit(["python_codemod"])
    env = LocalEnvironment(tmp_path)
    ctx = ToolContext("codemod-test", permissions=PermissionEngine(workspace_root=tmp_path))
    return tools[0] if tools else None, env, ctx


async def preview(codemod, paths):
    tool, env, ctx = codemod
    assert tool is not None, "python_codemod must be available through the builtin toolkit"
    result = await tool.execute({
        "action": "preview", "paths": paths,
        "transformation": {"kind": "rename_import", "module": "api", "old": "old", "new": "fresh"},
    }, env, ctx)
    return result, json.loads(result.content)


async def apply(codemod, token):
    tool, env, ctx = codemod
    result = await tool.execute({"action": "apply", "preview_token": token}, env, ctx)
    return result, json.loads(result.content)


@pytest.mark.parametrize("newline", ["\r\n", "\r", "\n"])
async def test_preview_then_apply_preserves_exact_formatting_and_reports_skips(codemod, tmp_path, newline, monkeypatch):
    async def refuse_execution(*args, **kwargs):
        pytest.fail("Codemods must parse on the host without workspace execution")

    monkeypatch.setattr(codemod[1], "execute", refuse_execution)
    source = ('# old stays\u2028here\fstill a comment\r\nfrom api import (\r\n    old ,  # API\r\n)\r\n'
              'é = 1; result = old(  é )\r\nlabel = "old"; data = b"old"\r\nobj.old()\r\n'
              'def use():\r\n\treturn old()').replace('\r\n', newline)
    alias_source = 'from api import old as stable  # alias\nresult = stable()\n'
    (tmp_path / "app.py").write_bytes(source.encode())
    (tmp_path / "alias.py").write_text(alias_source)
    result, plan = await preview(codemod, ["app.py", "alias.py"])
    assert not result.is_error
    assert plan["version"] == 1
    assert plan["preview_token"].startswith("python-codemod-v1:")
    row = plan["files"][0]
    assert row["before_digest"] == hashlib.sha256(source.encode()).hexdigest()
    assert row["replacements"] == 3
    assert "\\ No newline at end of file" in row["diff"]
    assert '+    fresh ,  # API' + newline in row["diff"]
    assert {item["code"] for item in row["issues"]} == {
        "string_not_transformed", "attribute_not_transformed",
    }
    assert (tmp_path / "app.py").read_bytes() == source.encode()
    result, report = await apply(codemod, plan["preview_token"])
    assert not result.is_error
    assert [row["status"] for row in report["files"]] == ["applied", "applied"]
    expected = ('# old stays\u2028here\fstill a comment\r\nfrom api import (\r\n    fresh ,  # API\r\n)\r\n'
                'é = 1; result = fresh(  é )\r\nlabel = "old"; data = b"old"\r\nobj.old()\r\n'
                'def use():\r\n\treturn fresh()').replace('\r\n', newline)
    assert (tmp_path / "app.py").read_bytes() == expected.encode()
    assert (tmp_path / "alias.py").read_text() == 'from api import fresh as stable  # alias\nresult = stable()\n'
    result, _ = await apply(codemod, plan["preview_token"])
    assert result.is_error


@pytest.mark.parametrize(("tail", "code"), [
    ('def use(old):\n    return old()\n', "ambiguous_binding"),
    ('fresh = 1\n', "ambiguous_binding"),
    ('old = 1\n', "ambiguous_binding"),
    ('[old for old in []]\n', "ambiguous_binding"),
    ('match thing:\n    case {"x": old}: pass\n', "ambiguous_binding"),
    ('from other import *\n', "star_import"),
    ('getattr(obj, "old")()\n', "dynamic_access"),
    ('lookup = getattr\nlookup(obj, "old")\n', "dynamic_access"),
    ('from builtins import getattr as lookup\nlookup(obj, "old")\n', "dynamic_access"),
    ('return old()\n', "syntax_error"),
    ('def duplicate(x, x): pass\n', "syntax_error"),
    ('def broken(\n', "syntax_error"),
])
async def test_ambiguity_blocks_entire_plan_without_mutation(codemod, tmp_path, tail, code):
    source = 'from api import old\n' + tail
    (tmp_path / "bad.py").write_text(source)
    (tmp_path / "good.py").write_text('from api import old\nold()\n')
    result, plan = await preview(codemod, ["good.py", "bad.py"])
    assert result.is_error
    assert plan["preview_token"] is None
    assert code in {issue["code"] for issue in plan["files"][1]["issues"]}
    assert plan["files"][1]["diff"] == ""
    assert (tmp_path / "good.py").read_text() == 'from api import old\nold()\n'
    assert (tmp_path / "bad.py").read_text() == source


async def test_all_digests_rechecked_before_first_write_including_unchanged_files(codemod, tmp_path):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text('from api import old\nold()\n')
    (tmp_path / "unchanged.py").write_text('x = 1\n')
    _, plan = await preview(codemod, ["a.py", "b.py", "unchanged.py"])
    (tmp_path / "unchanged.py").write_text('x = 2\n')
    result, report = await apply(codemod, plan["preview_token"])
    assert result.is_error
    assert [row["status"] for row in report["files"]] == ["unattempted", "unattempted", "stale"]
    assert (tmp_path / "a.py").read_text() == 'from api import old\nold()\n'
    assert (tmp_path / "b.py").read_text() == 'from api import old\nold()\n'
    assert (tmp_path / "unchanged.py").read_text() == 'x = 2\n'


async def test_live_path_denial_blocks_every_write_and_preview_honors_read_denial(codemod, tmp_path):
    tool, env, ctx = codemod
    for name in ("a.py", "secret.py"):
        (tmp_path / name).write_text('from api import old\nold()\n')
    _, plan = await preview(codemod, ["a.py", "secret.py"])
    ctx.permissions = PermissionEngine(path_rules={"deny": ["secret.py"]}, workspace_root=tmp_path)
    result, report = await apply(codemod, plan["preview_token"])
    assert result.is_error
    assert [row["status"] for row in report["files"]] == ["unattempted", "denied"]
    assert (tmp_path / "a.py").read_text() == 'from api import old\nold()\n'
    assert (tmp_path / "secret.py").read_text() == 'from api import old\nold()\n'
    result, report = await preview(codemod, ["secret.py"])
    assert result.is_error
    assert report["files"][0]["before_digest"] is None
    assert report["files"][0]["issues"][0]["code"] == "permission_denied"


async def test_write_ask_uses_normal_approval_handler_and_readonly_refuses_apply(codemod, tmp_path):
    _, _, ctx = codemod
    (tmp_path / "app.py").write_text('from api import old\nold()\n')
    _, plan = await preview(codemod, ["app.py"])
    approvals = []

    async def approve(action):
        approvals.append(action)
        return True

    ctx.permissions = PermissionEngine(tool_rules={"write_file": "ask"}, approval_handler=approve)
    result, _ = await apply(codemod, plan["preview_token"])
    assert not result.is_error
    assert len(approvals) == 1
    assert "write_file" in approvals[0] and "app.py" in approvals[0]
    (tmp_path / "app.py").write_text('from api import old\nold()\n')
    _, plan = await preview(codemod, ["app.py"])
    ctx.permissions = PermissionEngine(mode="readonly")
    result, _ = await apply(codemod, plan["preview_token"])
    assert result.is_error
    assert (tmp_path / "app.py").read_text() == 'from api import old\nold()\n'


@pytest.mark.parametrize("restore_fails", [False, True])
@pytest.mark.parametrize("failure", ["exception", "cancelled", "silent_corruption"])
async def test_partial_write_failure_restores_even_failing_file_or_reports_unknown(codemod, tmp_path, restore_fails, failure):
    tool, _, ctx = codemod

    class FailingEnvironment(LocalEnvironment):
        failed = False

        async def write_file(self, path, content):
            if path == "b.py" and not self.failed:
                self.failed = True
                await super().write_file(path, "truncated")
                if failure == "cancelled":
                    raise asyncio.CancelledError("transport cancelled")
                if failure == "silent_corruption":
                    return
                raise OSError("disk failure")
            if self.failed and restore_fails:
                raise OSError("restoration unavailable")
            await super().write_file(path, content)

    env = FailingEnvironment(tmp_path)
    run = tool, env, ctx
    source = 'from api import old\nold()\n'
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text(source)
    _, plan = await preview(run, ["a.py", "b.py", "c.py"])
    result, report = await apply(run, plan["preview_token"])
    assert result.is_error
    assert [row["write"] for row in report["files"]] == [
        "completed", "completed" if failure == "silent_corruption" else "failed", "unattempted",
    ]
    assert report["files"][2]["status"] == "unattempted"
    assert (tmp_path / "c.py").read_text() == source
    if restore_fails:
        assert report["status"] == "restoration_failed"
        assert [row["restoration"] for row in report["files"][:2]] == ["unknown", "unknown"]
        assert (tmp_path / "b.py").read_text() == "truncated"
    else:
        assert report["status"] == "rolled_back"
        assert [row["status"] for row in report["files"][:2]] == ["restored", "restored"]
        assert (tmp_path / "a.py").read_text() == source
        assert (tmp_path / "b.py").read_text() == source


async def test_cancellation_settles_inflight_write_and_restores(codemod, tmp_path):
    tool, _, ctx = codemod
    started, release = asyncio.Event(), asyncio.Event()

    class SlowEnvironment(LocalEnvironment):
        async def write_file(self, path, content):
            if "fresh" in content:
                started.set()
                await release.wait()
            await super().write_file(path, content)

    env = SlowEnvironment(tmp_path)
    source = 'from api import old\nold()\n'
    (tmp_path / "app.py").write_text(source)
    _, plan = await preview((tool, env, ctx), ["app.py"])
    task = asyncio.create_task(apply((tool, env, ctx), plan["preview_token"]))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "app.py").read_text() == source


async def test_bounds_foreign_tokens_and_declarative_validation(codemod, tmp_path):
    tool, env, ctx = codemod
    source = 'from api import old\n' + '# padding\n' * 15000
    (tmp_path / "large.py").write_text(source)
    result, report = await preview(codemod, ["large.py"])
    assert result.is_error
    assert report["files"][0]["replacements"] is None
    assert "byte limit" in report["files"][0]["issues"][0]["reason"]
    result, _ = await preview(codemod, ["large.py"] * 17)
    assert result.is_error
    (tmp_path / "app.py").write_text('from api import old\nold()\n')
    for paths in (["app.py", "./app.py"], ["../app.py"]):
        result, _ = await preview(codemod, paths)
        assert result.is_error
    _, plan = await preview(codemod, ["app.py"])
    other_ctx = ToolContext("another", permissions=PermissionEngine())
    result, _ = await apply((tool, env, other_ctx), plan["preview_token"])
    assert result.is_error
    result, _ = await apply((tool, LocalEnvironment(tmp_path), ctx), plan["preview_token"])
    assert result.is_error
    result = await tool.execute({"action": "apply", "preview_token": plan["preview_token"],
                                 "paths": ["app.py"]}, env, ctx)
    assert result.is_error
    result = await tool.execute({"action": "preview", "paths": ["app.py"], "transformation": {
        "kind": "execute", "module": "api", "old": "old", "new": "fresh",
    }}, env, ctx)
    assert result.is_error
    assert (tmp_path / "app.py").read_text() == 'from api import old\nold()\n'


@pytest.mark.parametrize("limit", ["per_file", "total", "output"])
async def test_replacement_and_output_caps_refuse_before_writes(codemod, tmp_path, limit):
    if limit == "output":
        source = 'from api import old\nold()\n#'
        source += 'x' * (128 * 1024 - len(source) - 1)
        paths = ["app.py"]
    else:
        source = 'from api import old\n' + 'old()\n' * (1000 if limit == "per_file" else 500)
        paths = ["app.py"] if limit == "per_file" else ["app.py", "other.py"]
    for path in paths:
        (tmp_path / path).write_text(source)
    result, report = await preview(codemod, paths)
    assert result.is_error
    assert report["preview_token"] is None
    if limit != "total":
        codes = {item["code"] for item in report["files"][0]["issues"]}
        assert ("replacement_limit" if limit == "per_file" else "result_size_limit") in codes
    else:
        assert "1000" in report["reason"]
    assert all((tmp_path / path).read_text() == source for path in paths)
