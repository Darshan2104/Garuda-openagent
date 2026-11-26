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
  codex: {allowed_models: [gpt-5.5-codex], handshake_timeout_sec: 90}
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
      - {id: build, role: coder, inputs: [plan], outputs: [patch],
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
    # retries apply to plain steps only (#810)
    ("version: 1\nroles: {r: {harness: codex}}\nflows: {f: {steps: [{parallel: [r], retries: 1}]}}",
     "config.invalid", "flows.f.steps[0].retries"),
    ("version: 1\nroles: {r: {harness: codex}, v: {harness: codex}}\nflows: {f: {steps: ["
     "{role: r, outputs: [patch], retries: 1, review: {by: v}}, {role: v, outputs: [review]}]}}",
     "config.invalid", "flows.f.steps[0].retries"),
    ("version: 1\nroles: {r: {harness: codex}, v: {harness: codex}}\nflows: {f: {steps: ["
     "{role: r, outputs: [patch], review: {by: v}}, {role: v, outputs: [review], retries: 1}]}}",
     "config.invalid", "flows.f.steps[1].retries"),
    ("version: 1\nconsults: {max_per_session: 101}\n", "config.invalid",
     "consults.max_per_session"),
    ("version: 1\nroles: {r: {harness: codex}}\n"
     "flows: {f: {steps: [{parallel: [r], write_policy: no-edits}]}}", "config.invalid",
     "flows.f.steps[0].write_policy"),
    ("version: 1\nroles: {r: {harness: codex, permissions: no-edits}}", "config.invalid",
     "roles.r.permissions"),
    ("version: 1\nchecks: [{run: pytest, cwd: ../elsewhere}]", "config.invalid", "checks[0].cwd"),
    *[(f"version: 1\nharnesses: {{codex: {{handshake_timeout_sec: {bad}}}}}\n", "config.invalid",
       "harnesses.codex.handshake_timeout_sec") for bad in ("4", "601", "1.5", "true", "slow")],
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


def test_project_cannot_exceed_ceiling_when_user_role_sets_no_permissions():
    user = _user(roles={"coder": {"harness": "codex"}})
    project = _user(roles={"coder": {"harness": "codex", "permissions": "yolo"}})
    with pytest.raises(GarudaConfigError) as caught:
        gy.resolve(user, project)
    assert (caught.value.code, caught.value.path) == (
        "config.project_widening", "roles.coder.permissions")


@pytest.mark.parametrize("ceiling,permissions", [("smart", "auto"), ("readonly", "smart")])
def test_project_permissions_cannot_exceed_configured_ceiling(ceiling, permissions):
    user = _user(roles={"coder": {"harness": "codex"}})
    project = _user(roles={"coder": {"harness": "codex", "permissions": permissions}})
    with pytest.raises(GarudaConfigError) as caught:
        gy.resolve(user, project, project_ceiling=ceiling)
    assert (caught.value.code, caught.value.path) == (
        "config.project_widening", "roles.coder.permissions")


@pytest.mark.parametrize("ceiling, permissions", [("smart", "readonly"), ("yolo", "yolo")])
def test_project_permissions_within_ceiling_when_user_role_sets_none(ceiling, permissions):
    user = _user(roles={"coder": {"harness": "codex"}})
    project = _user(roles={"coder": {"harness": "codex", "permissions": permissions}})
    config = gy.resolve(user, project, project_ceiling=ceiling).config
    assert config["roles"]["coder"]["permissions"] == permissions


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
    ({"harnesses": {"codex": {"handshake_timeout_sec": 60}}}, "harnesses.codex.handshake_timeout_sec"),
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


def test_fallback_models_obey_harness_allowlists_during_config_resolution():
    config = gy.parse({"version": 1, "harnesses": {"native": {"allowed_models": ["approved"]}},
                       "roles": {"coder": {"harness": "codex", "fallback": [
                           {"harness": "native", "model_id": "forbidden"}]}}})
    with pytest.raises(GarudaConfigError) as error:
        gy.resolve(config, None, cli_role="coder")
    assert error.value.path == "roles.coder.fallback[0].model_id"


@pytest.mark.parametrize("steps", [
    [{"id": "coder-2", "role": "coder"}, {"role": "coder"}],
    [{"role": "coder"}, {"id": "coder-1", "role": "coder"}],
    [{"id": "group-2", "role": "coder"}, {"parallel": ["coder"]}],
])
def test_effective_flow_ids_reject_explicit_generated_collisions(steps):
    with pytest.raises(GarudaConfigError, match="step ids must be unique") as error:
        gy.parse({"version": 1, "flows": {"qa": {"steps": steps}}})
    assert error.value.path == "flows.qa.steps"


# --- flow inputs and reference errors -------------------------------------------------


@pytest.mark.parametrize("steps, path", [
    ([{"role": "coder", "inputs": ["plan"], "outputs": ["patch"]}], "flows.f.steps[0].inputs[0]"),
    ([{"role": "coder", "outputs": ["patch"]},
      {"role": "reviewer", "inputs": ["patch", "plan"], "outputs": ["review"]}],
     "flows.f.steps[1].inputs[1]"),
    # a step's own output is not an input to it
    ([{"role": "coder", "inputs": ["patch"], "outputs": ["patch"]}], "flows.f.steps[0].inputs[0]"),
])
def test_a_flow_input_no_earlier_step_outputs_is_refused_at_load(steps, path):
    with pytest.raises(GarudaConfigError) as caught:
        _user(roles=ROLES, flows={"f": {"steps": steps}})
    assert (caught.value.code, caught.value.path) == ("config.invalid", path)
    assert "no earlier step outputs a" in str(caught.value)


def test_packaged_flows_and_starters_have_satisfiable_inputs():
    from garuda.flows import packaged

    for flow in gy.load_text(packaged.TEXT)["flows"].values():
        gy.validate_flow_inputs(gy.effective_flow_steps(flow), path="flow.steps")
    flows = gy.load_text(EXAMPLE_USER).get("flows", {})
    for flow in flows.values():
        gy.validate_flow_inputs(gy.effective_flow_steps(flow), path="flow.steps")


def test_an_unknown_flow_role_names_the_file(tmp_path):
    ws = _repo(tmp_path / "ws")
    (ws / "garuda.yaml").write_text(
        "version: 1\nflows:\n  f:\n    steps:\n      - {role: ghost}\n")
    with pytest.raises(GarudaConfigError) as caught:
        gy.load_effective(ws)
    assert caught.value.code == "config.invalid" and caught.value.path == "flows.f.steps[0]"
    assert str(ws / "garuda.yaml") in str(caught.value) and "names no role 'ghost'" in str(caught.value)

    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text("version: 1\nroles: {coder: {harness: native, consult: [nobody]}}\n")
    (ws / "garuda.yaml").unlink()
    with pytest.raises(GarudaConfigError) as caught:
        gy.load_effective(ws)
    assert str(gy.user_path()) in str(caught.value) and caught.value.path == "roles.coder.consult"


# --- addressable inputs (#817) --------------------------------------------------------

_SCOUTS = [{"id": "scout-api", "role": "reviewer", "outputs": ["notes"]},
           {"id": "scout-web", "role": "reviewer", "outputs": ["notes", "plan"]},
           {"id": "panel", "parallel": ["coder", "reviewer"], "outputs": ["review"]}]


def _with_inputs(*inputs):
    return [*_SCOUTS, {"id": "use", "role": "coder", "inputs": list(inputs),
                       "outputs": ["patch"]}]


def test_mapping_inputs_name_their_producer_and_plain_ones_stay_strings():
    doc = _user(roles=ROLES, flows={"f": {"steps": _with_inputs(
        "plan", {"from": "scout-api", "type": "notes"}, {"type": "notes", "from": "scout-web"},
        {"type": "review", "from": "panel:reviewer"})}})
    assert doc["flows"]["f"]["steps"][3]["inputs"] == [
        "plan", {"type": "notes", "from": "scout-api"}, {"type": "notes", "from": "scout-web"},
        {"type": "review", "from": "panel:reviewer"}]


@pytest.mark.parametrize("entry, path, message", [
    ({"type": "notes", "from": "ghost"}, "inputs[0].from", "does not exist"),
    ({"type": "notes", "from": "use"}, "inputs[0].from", "is not earlier"),
    ({"type": "patch", "from": "scout-api"}, "inputs[0].from", "does not output a patch"),
    ({"type": "notes", "from": "scout-api:coder"}, "inputs[0].from", "is not a parallel group"),
    ({"type": "review", "from": "panel:other"}, "inputs[0].from", "is not a member"),
    ({"type": "notes", "from": "scout-api", "step": "x"}, "inputs[0].step", "unknown key"),
    ({"type": "notes"}, "inputs[0].from", "is required"),
    ({"from": "scout-api"}, "inputs[0].type", "is required"),
    ({"type": "nope", "from": "scout-api"}, "inputs[0].type", "must be one of"),
    ({"type": "notes", "from": "Scout API"}, "inputs[0].from", "is not a name"),
])
def test_a_bad_mapping_input_is_refused_with_its_path(entry, path, message):
    with pytest.raises(GarudaConfigError, match=message) as caught:
        _user(roles=ROLES, flows={"f": {"steps": _with_inputs(entry)}})
    assert (caught.value.code, caught.value.path) == ("config.invalid", f"flows.f.steps[3].{path}")


@pytest.mark.parametrize("inputs, message", [
    ([{"type": "notes", "from": "scout-api"}, {"type": "notes", "from": "scout-api"}],
     "duplicate input"),
    ([{"type": "review", "from": "panel"}, {"type": "review", "from": "panel:coder"}],
     "duplicate input"),
    ([{"type": "review", "from": "panel:coder"}, {"type": "review", "from": "panel"}],
     "duplicate input"),
    (["notes", {"type": "notes", "from": "scout-web"}], "also asked for plainly"),
    ([{"type": "notes", "from": "scout-api"}, "notes"], "also asked for by producer"),
])
def test_an_input_delivered_twice_is_refused(inputs, message):
    with pytest.raises(GarudaConfigError, match=message) as caught:
        _user(roles=ROLES, flows={"f": {"steps": _with_inputs(*inputs)}})
    assert caught.value.path == "flows.f.steps[3].inputs[1]"


def test_from_names_a_generated_step_id():
    doc = _user(roles=ROLES, flows={"f": {"steps": [
        {"role": "reviewer", "outputs": ["notes"]},
        {"role": "coder", "inputs": [{"type": "notes", "from": "reviewer-1"}]}]}})
    assert doc["flows"]["f"]["steps"][1]["inputs"] == [{"type": "notes", "from": "reviewer-1"}]


def test_flow_definition_digests_without_the_mapping_form_are_unchanged():
    """Checked-flow resume compares ``digest(flow)``: values recorded on main before #817."""
    from garuda.core.acceptance_records import digest
    from garuda.flows import packaged

    assert {name: digest(flow) for name, flow in packaged.FLOWS.items()} == {
        "plan-only": "648334a53094acafb55b6c686a1b47353a143c70723d2853acd75958a883c1a2",
        "pair": "c73516802af5b9f460ee47511f8ecd0d2aef2b882b84b41a5cddcdd2a5cd834c",
        "plan-build-review": "ca004f407f1eef25fb911efd6dde30c0383233fa380a2a56f233220b76c4a62f",
    }
    flow = gy.load_text("""\
version: 1
flows:
  scouts:
    steps:
      - {id: scout-api, role: scout, write_policy: no-edits, outputs: [notes]}
      - {id: scouts, parallel: [a, b], outputs: [notes]}
      - {id: plan, role: planner, inputs: [notes, notes], outputs: [plan]}
""")["flows"]["scouts"]
    assert digest(flow) == "ad9777ec0527c2ee3c3712b2cfdd1ce59de4c9c6851388db48e03af51f41d421"
