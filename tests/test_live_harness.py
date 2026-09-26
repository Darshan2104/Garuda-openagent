"""Opt-in real-harness tests for issue #43 (P1.11).

Gated on GARUDA_LIVE_HARNESS (harness id or `all`): without it everything
skips and CI spends nothing. With it, each selected harness gets a handshake
plus one trivial prompt in a fixture workspace, with the exact harness,
binary, version probe, auth outcome, and elapsed time reported.
"""

import json
import os
import sys

import pytest

from garuda.acp.catalog import builtin_manifest_dicts
from garuda.eval.live_harness import PROMPT_TEXT, run_smoke, selected_harnesses
from garuda.runtime.registry import parse_global_manifests

LIVE = bool(os.environ.get("GARUDA_LIVE_HARNESS"))
WANTED = selected_harnesses()
ALL_IDS = sorted(
    m.runtime_id
    for m in parse_global_manifests(builtin_manifest_dicts(), source="live test")
    if m.runtime_id != "native"
)
TARGETS = ALL_IDS if "all" in WANTED else [h for h in WANTED if h != "all"]

requires_live = pytest.mark.skipif(not LIVE, reason="set GARUDA_LIVE_HARNESS to run")


def test_gating_reports_empty_by_default(monkeypatch):
    monkeypatch.delenv("GARUDA_LIVE_HARNESS", raising=False)
    assert selected_harnesses() == []


def test_prompt_text_is_trivial_and_bounded():
    assert len(PROMPT_TEXT) < 100


@requires_live
@pytest.mark.parametrize("harness", TARGETS or ["__none__"])
async def test_live_smoke_per_harness(tmp_path, harness):
    assert harness != "__none__", "GARUDA_LIVE_HARNESS names no known harness"
    workspace = tmp_path / "fixture-ws"
    workspace.mkdir()
    report = await run_smoke(harness, workspace=workspace, timeout=180.0)
    print(json.dumps(report.to_dict(), indent=2))
    if report.skipped:
        assert report.ok is False
        assert "not installed" in report.detail
        return
    assert report.ok is True
    assert report.turn >= 1
    assert report.events >= 1
    assert report.binary
    assert report.elapsed_sec < 180.0


@requires_live
async def test_unknown_harness_is_actionable(tmp_path):
    with pytest.raises(ValueError, match="Unknown harness"):
        await run_smoke("not-a-harness", workspace=tmp_path)


async def test_smoke_runs_in_fixture_workspace_against_fake(tmp_path):
    """The gating shape without the spend: fake argv, fixture workspace.

    The fake runs from its resolved script path, not `python -m`: the smoke
    cwd is an isolated fixture dir, and in a linked checkout without an
    install that cwd cannot import the `garuda` package. The script itself
    has no package imports, so a direct path executes anywhere.
    """
    import garuda.acp.fake_agent as fake_agent_mod

    workspace = tmp_path / "fixture-ws"
    workspace.mkdir()
    report = await run_smoke(
        "claude",
        argv=[
            sys.executable, str(fake_agent_mod.__file__),
            "--profile", "success",
        ],
        workspace=workspace,
        timeout=30.0,
    )
    assert report.ok is True
    assert report.harness == "claude"
    assert report.turn == 1


async def test_missing_harness_is_an_explicit_skip(tmp_path, monkeypatch):
    class Missing:
        runtime_id = "claude"
        available = False
        executable = None
        version = "unknown"

        class _Auth:
            value = "unknown"

        auth = _Auth()

    monkeypatch.setattr("garuda.acp.catalog.discover", lambda manifests: [Missing()])
    report = await run_smoke("claude", workspace=tmp_path)
    assert report.skipped is True
    assert report.ok is False
    assert report.detail == "not installed or unavailable"


def test_fake_agent_script_has_no_package_imports():
    """The fixture-cwd trick depends on this: fake_agent.py must import
    nothing beyond the standard library, or the resolved-path launch
    breaks exactly like `python -m` did."""
    import ast

    import garuda.acp.fake_agent as fake_agent_mod

    tree = ast.parse(open(fake_agent_mod.__file__, encoding="utf-8").read())
    stdlib = {"__future__", "argparse", "json", "os", "sys", "time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in stdlib, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in stdlib, node.module
