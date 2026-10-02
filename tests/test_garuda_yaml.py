"""The additive garuda.yaml: schema, layering and migration (#158, plan task C.1)."""

import os
import subprocess
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.config.garuda_yaml import CLI, PACKAGE, PROJECT, USER, GarudaConfigError

EXAMPLE_USER = """\
version: 1
defaults: {role: coder}
harnesses:
  claude-code: {allowed_models: [claude-sonnet-5-5], max_parallel: 2}
  codex: {allowed_models: [gpt-5.5-codex]}
roles:
  planner:  {harness: claude-code, model_id: claude-sonnet-5-5, permissions: smart, write_policy: no-edits}
  coder:    {harness: codex, model_id: gpt-5.5-codex, effort: high,
             fallback: [{harness: claude-code, model_id: claude-sonnet-5-5}], consult: [reviewer]}
  reviewer: {harness: claude-code, model_id: claude-sonnet-5-5, permissions: smart, write_policy: no-edits}
consults: {max_per_session: 5, timeout_sec: 600, max_answer_chars: 8000}
sessions: {isolation: auto, keep_days: 30}
"""

EXAMPLE_PROJECT = """\
version: 1
checks: [pytest -q]
flows:
  plan-build-review:
    steps:
      - {id: plan, role: planner, outputs: [plan]}
      - {id: build, role: coder, inputs: [plan], outputs: [patch], retries: 2,
         review: {by: reviewer, max_rounds: 2}}
      - {id: review, role: reviewer, inputs: [patch], outputs: [review]}
"""


def _err(text):
    with pytest.raises(GarudaConfigError) as caught:
        gy.load_text(text)
    return caught.value


# --- schema --------------------------------------------------------------------------


@pytest.mark.parametrize("text", [EXAMPLE_USER, EXAMPLE_PROJECT])
def test_maintained_examples_round_trip(text):
    doc = gy.load_text(text)
    assert gy.load_text(gy.dump(doc)) == doc
    assert gy.dump(gy.load_text(gy.dump(doc))) == gy.dump(doc)


@pytest.mark.parametrize("text, code, path", [
    ("version: 1\nroles: {coder: {harness: codex, efort: high}}", "config.invalid",
     "roles.coder.efort"),
    ("version: 1\nroles:\n  coder: {harness: codex}\n  coder: {harness: claude}\n",
     "config.invalid", ""),
    ("version: 1\nroles: !!python/object/apply:os.system [true]\n", "config.invalid", ""),
    ("version: 1\nroles: {coder: {harness: codex, authority: user-config}}", "config.invalid",
     "roles.coder.authority"),
    ("version: 1\nauthority: trusted-project\n", "config.invalid", "authority"),
    ("version: 2\n", "config.unsupported_version", "version"),
    ("version: 1\nrouting: {enabled: true}\n", "config.conflict", "routing"),
    ("version: 1\nroles: {r: {harness: codex}}\nflows: {f: {steps: [{flow: other}]}}",
     "config.invalid", "flows.f.steps[0].flow"),
    ("version: 1\nroles: {r: {harness: codex}}\nflows: {f: {steps: [{role: r, retries: 11}]}}",
     "config.invalid", "flows.f.steps[0].retries"),
    ("version: 1\nconsults: {max_per_session: 101}\n", "config.invalid",
     "consults.max_per_session"),
    ("version: 1\nroles: {r: {harness: codex}}\n"
     "flows: {f: {steps: [{parallel: [r], write_policy: no-edits}]}}", "config.invalid",
     "flows.f.steps[0].write_policy"),
    ("version: 1\nroles: {r: {harness: codex, permissions: no-edits}}", "config.invalid",
     "roles.r.permissions"),
    ("version: 1\nchecks: [{run: pytest, cwd: ../elsewhere}]", "config.invalid", "checks[0].cwd"),
])
def test_refusals_name_the_value(text, code, path):
    error = _err(text)
    assert (error.code, error.path) == (code, path)


def test_v1_bounds():
    steps = ", ".join("{role: r}" for _ in range(33))
    assert _err(f"version: 1\nroles: {{r: {{harness: codex}}}}\nflows: {{f: {{steps: [{steps}]}}}}"
                ).path == "flows.f.steps"
    group = ", ".join(f"r{i}" for i in range(17))
    roles = ", ".join(f"r{i}: {{harness: codex}}" for i in range(17))
    assert _err(f"version: 1\nroles: {{{roles}}}\nflows: {{f: {{steps: [{{parallel: [{group}]}}]}}}}"
                ).path == "flows.f.steps[0].parallel"


# --- layering ------------------------------------------------------------------------


def _user(**extra):
    return gy.parse({"version": 1, **extra})


ROLES = {
    "coder": {"harness": "codex", "model_id": "gpt-x", "effort": "high", "permissions": "smart",
              "fallback": [{"harness": "claude"}, {"harness": "opencode"}],
              "consult": ["reviewer"]},
    "reviewer": {"harness": "claude", "permissions": "readonly"},
}


def test_whole_definition_replacement_and_ceiling_intersection():
    user = _user(roles=ROLES)
    project = _user(roles={"coder": {"harness": "codex", "permissions": "yolo"}})

    resolved = gy.resolve(user, project)

    coder = resolved.config["roles"]["coder"]
    assert "effort" not in coder and "model_id" not in coder  # replaced, not merged
    assert coder["permissions"] == "smart"  # the stricter ceiling wins
    assert coder["fallback"] == ROLES["coder"]["fallback"]  # user-only grants carry over
    assert resolved.provenance["roles.coder"] == PROJECT
    assert resolved.provenance["roles.reviewer"] == USER


@pytest.mark.parametrize("project, path", [
    ({"roles": {"coder": {"harness": "codex", "fallback": [{"harness": "aider"}]}}},
     "roles.coder.fallback"),
    ({"roles": {"coder": {"harness": "codex",
                          "fallback": [{"harness": "opencode"}, {"harness": "claude"}]}}},
     "roles.coder.fallback"),  # reordering is not removal
    ({"roles": {"coder": {"harness": "codex", "consult": ["reviewer", "planner"]}}},
     "roles.coder.consult"),
    ({"roles": {"newbie": {"harness": "codex", "consult": ["reviewer"]}}}, "roles.newbie.consult"),
    ({"harnesses": {"aider": {}}}, "harnesses.aider"),
    ({"harnesses": {"codex": {"allowed_models": ["gpt-x", "gpt-y"]}}},
     "harnesses.codex.allowed_models"),
    ({"sessions": {"keep_days": 999}}, "sessions.keep_days"),
])
def test_a_project_cannot_widen_user_only_settings(project, path):
    user = _user(roles=ROLES, harnesses={"codex": {"allowed_models": ["gpt-x"], "max_parallel": 4},
                                          "claude": {}})
    with pytest.raises(GarudaConfigError) as caught:
        gy.resolve(user, _user(**project))
    assert (caught.value.code, caught.value.path) == ("config.project_widening", path)


def test_a_project_may_narrow():
    user = _user(roles=ROLES, harnesses={"codex": {"allowed_models": ["gpt-x", "gpt-y"],
                                                   "max_parallel": 4}, "claude": {}},
                 consults={"max_per_session": 5, "timeout_sec": 600})
    project = _user(roles={"coder": {"harness": "codex", "model_id": "gpt-x",
                                     "fallback": [{"harness": "opencode"}], "consult": []}},
                    harnesses={"codex": {"allowed_models": ["gpt-x"], "max_parallel": 9}},
                    consults={"max_per_session": 8, "timeout_sec": 60})

    config = gy.resolve(user, project).config

    assert config["roles"]["coder"]["fallback"] == [{"harness": "opencode"}]
    assert config["harnesses"]["codex"] == {"allowed_models": ["gpt-x"], "max_parallel": 4}
    assert config["consults"] == {"max_per_session": 5, "timeout_sec": 60}


def test_checks_accumulate_without_duplicates():
    user = _user(checks=["pytest -q", {"run": ["ruff", "check", "."]}])
    project = _user(checks=["pytest -q", {"run": "pytest -q", "cwd": "sub"}])

    resolved = gy.resolve(user, project, cli_checks=["pytest -q", "make lint"])

    runs = [(c["run"], c.get("cwd")) for c in resolved.config["checks"]]
    assert runs == [("pytest -q", None), (["ruff", "check", "."], None), ("pytest -q", "sub"),
                    ("make lint", None)]
    assert [resolved.provenance[f"checks[{i}]"] for i in range(4)] == [USER, USER, PROJECT, CLI]


@pytest.mark.parametrize("kwargs, role, source", [
    ({}, "coder", PROJECT),
    ({"cli_role": "reviewer"}, "reviewer", CLI),
    ({"cli_runtime": "native"}, None, None),  # explicit native bypasses the implicit default
    ({"cli_model": "some/model"}, None, None),
])
def test_role_selection_precedence(kwargs, role, source):
    package = _user(roles=ROLES, defaults={"role": "reviewer"})
    user = _user(defaults={"role": "reviewer"})
    project = _user(defaults={"role": "coder"})

    resolved = gy.resolve(user, project, package=package, **kwargs)

    assert resolved.role == role
    assert resolved.provenance.get("defaults.role") == source


@pytest.mark.parametrize("kwargs, path", [
    ({"cli_role": "coder", "cli_runtime": "claude"}, "--runtime"),
    ({"cli_role": "coder", "cli_model": "gpt-other"}, "--model"),
])
def test_an_explicit_mismatch_conflicts(kwargs, path):
    with pytest.raises(GarudaConfigError) as caught:
        gy.resolve(_user(roles=ROLES), None, **kwargs)
    assert (caught.value.code, caught.value.path) == ("config.conflict", path)


def test_a_legacy_capacity_that_disagrees_conflicts():
    user = _user(harnesses={"codex": {"max_parallel": 2}})
    assert gy.resolve(user, None, legacy_capacity={"codex": 2}).config["harnesses"]
    with pytest.raises(GarudaConfigError) as caught:
        gy.resolve(user, None, legacy_capacity={"codex": 3})
    assert caught.value.code == "config.conflict"


def test_references_and_allowed_models_are_checked():
    with pytest.raises(GarudaConfigError, match="names no role 'ghost'"):
        gy.resolve(_user(roles=ROLES, flows={"f": {"steps": [{"role": "ghost"}]}}), None)
    with pytest.raises(GarudaConfigError, match="allowed_models"):
        gy.resolve(_user(roles=ROLES, harnesses={"codex": {"allowed_models": ["other"]}}), None)
    assert gy.resolve(None, None).config == {"version": 1}
    assert gy.resolve(None, None, package=_user(roles=ROLES)).provenance["roles.coder"] == PACKAGE


# --- through the CLI ------------------------------------------------------------------


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def _main(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main as main

    return main(monkeypatch, capsys, *argv)


def test_a_conflict_refuses_before_anything_starts(tmp_path, monkeypatch, capsys):
    from garuda.core.sessions import SessionStore

    ws = _repo(tmp_path / "ws")
    (ws / "garuda.yaml").write_text("version: 1\nrouting: {enabled: true}\n")

    code, out = _main(monkeypatch, capsys, "run", "-t", "go", "--workspace", str(ws))

    assert code == 2 and "config.conflict" in out and "routing" in out
    assert SessionStore().list_sessions() == []


async def test_chat_refuses_an_invalid_file(tmp_path, capsys):
    import argparse

    from garuda.interfaces.cli import chat_loop

    ws = _repo(tmp_path / "ws")
    (ws / "garuda.yaml").write_text("version: 1\nroles: {coder: {harness: codex, x: 1}}\n")
    args = argparse.Namespace(workspace=str(ws), agents_dir=None, agent="build", json=False)
    assert await chat_loop(args) == 1
    assert "roles.coder.x" in capsys.readouterr().err


async def test_a_valid_file_leaves_the_native_run_plan_unchanged(tmp_path, monkeypatch):
    from garuda.agents.setup import prepare_agent_run
    from garuda.model.script_model import ScriptModel

    ws = _repo(tmp_path / "ws")

    async def plan():
        prepared = await prepare_agent_run("build", workspace=str(ws), model=ScriptModel([]))
        return (prepared.config.mode, prepared.config.permission_mode, prepared.profile.name,
                {k: v.provenance for k, v in prepared.provenance.items()},
                type(prepared.agent).__name__, sorted(t.name for t in prepared.tools))

    before = await plan()
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(EXAMPLE_USER)
    (ws / "garuda.yaml").write_text(EXAMPLE_PROJECT)
    gy.load_effective(ws)  # valid

    assert await plan() == before


def test_migrate_previews_writes_once_and_refuses_conflicts(tmp_path, monkeypatch, capsys):
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("runtimes:\n  - runtime_id: fakeacp\n    kind: acp\n    command: [x]\n"
                        "capacity:\n  native: 2\n")

    code, out = _main(monkeypatch, capsys, "config", "migrate")
    assert code == 0 and "+ harnesses.native.max_parallel: 2" in out
    assert "+ harnesses.fakeacp" in out and not gy.user_path().exists()

    code, out = _main(monkeypatch, capsys, "config", "migrate", "--write")
    assert code == 0 and gy.load_file(gy.user_path())["harnesses"]["native"] == {"max_parallel": 2}
    assert "capacity" in settings.read_text()  # never deletes

    code, out = _main(monkeypatch, capsys, "config", "migrate", "--write")
    assert code == 0 and "nothing to do" in out  # idempotent
    assert not list(gy.user_path().parent.glob("garuda.yaml.bak-*"))

    gy.user_path().write_text("version: 1\nharnesses: {native: {max_parallel: 5}}\n")
    code, out = _main(monkeypatch, capsys, "config", "migrate", "--write")
    assert code == 2 and "config.conflict" in out
    assert "max_parallel: 5" in gy.user_path().read_text()  # untouched
