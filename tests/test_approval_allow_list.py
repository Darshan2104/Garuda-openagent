"""A flow role's approval allow-list (#861): ``roles.NAME.approvals.allow``.

Unit tests drive the matcher, the configuration and its layering; the flow
tests drive real ACP steps against ``garuda.acp.fake_agent`` asking to run a
command (``--approval-kind execute --approval-command CMD``).
"""

import asyncio
import json
import os
import subprocess

import pytest

from garuda.acp import allow_list
from garuda.acp.broker import ApprovalBroker
from garuda.config import garuda_yaml as gy
from garuda.config.garuda_yaml import GarudaConfigError
from garuda.core.approval_outcome import approval_summary
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore

PYTEST = "python -m pytest*"


def _call(command, kind="execute", **raw):
    call = {"toolCallId": "c1", "title": str(command), "kind": kind}
    if command is not None:
        call["rawInput"] = {"command": command, **raw}
    return call


# --- matching -------------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "python -m pytest tests/test_textkit.py -q",
    "  python   -m pytest\t-q  ",
    ["bash", "-lc", "python -m pytest -q"],
    ["/bin/sh", "-c", "python -m pytest"],
    ["python", "-m", "pytest", "-q"],
])
def test_an_allow_listed_execute_command_matches_its_normalized_text(tmp_path, command):
    found = allow_list.match(_call(command), [PYTEST], tmp_path)
    assert found is not None and found.pattern == PYTEST
    assert found.command.startswith("python -m pytest") and "  " not in found.command


@pytest.mark.parametrize("command", [
    "python -m pip install requests", "pytest -q", "python3 -m pytest", "echo python -m pytest",
])
def test_a_command_that_matches_no_pattern_is_not_allowed(tmp_path, command):
    assert allow_list.match(_call(command), [PYTEST, "npm test"], tmp_path) is None


def test_a_pattern_matches_the_whole_command(tmp_path):
    assert allow_list.match(_call("npm test"), ["npm test"], tmp_path).command == "npm test"
    assert allow_list.match(_call("npm test --watch"), ["npm test"], tmp_path) is None


@pytest.mark.parametrize("command", [
    "python -m pytest; rm -rf ~", "python -m pytest && rm -rf ~", "python -m pytest || rm x",
    "python -m pytest | tee log", "python -m pytest & rm x", "python -m pytest $(rm -rf ~)",
    "python -m pytest `rm -rf ~`", "python -m pytest ${HOME}", "python -m pytest > out.txt",
    "python -m pytest < in.txt", "python -m pytest\nrm -rf ~", "python -m pytest\r",
    "python -m pytest \\\nx", "python -m pytest \x00", ["bash", "-c", "python -m pytest; rm x"],
])
def test_shell_control_or_expansion_is_never_auto_approved(tmp_path, command):
    assert allow_list.match(_call(command), [PYTEST, "*"], tmp_path) is None


@pytest.mark.parametrize("kind", ["edit", "delete", "move", "fetch", "read", "search", "other",
                                  "switch_mode", None, "EXECUTE"])
def test_only_execute_asks_are_eligible(tmp_path, kind):
    assert allow_list.match(_call("python -m pytest", kind=kind), [PYTEST], tmp_path) is None


@pytest.mark.parametrize("call", [
    {"toolCallId": "c", "title": "python -m pytest", "kind": "execute"},
    {"toolCallId": "c", "title": "python -m pytest", "kind": "execute", "rawInput": "x"},
    {"toolCallId": "c", "title": "python -m pytest", "kind": "execute",
     "rawInput": {"command": 7}},
    {"toolCallId": "c", "title": "python -m pytest", "kind": "execute",
     "rawInput": {"command": ["python", "-c", "print('a b')"]}},
    {"toolCallId": "c", "title": "python -m pytest", "kind": "execute",
     "rawInput": {"command": []}},
])
def test_an_unreadable_command_is_never_auto_approved(tmp_path, call):
    assert allow_list.match(call, [PYTEST, "python -c*"], tmp_path) is None


def test_a_command_outside_the_workspace_is_not_auto_approved(tmp_path):
    (tmp_path / "sub").mkdir()
    assert allow_list.match(_call("python -m pytest", cwd=str(tmp_path / "sub")), [PYTEST], tmp_path)
    assert allow_list.match(_call("python -m pytest", cwd="sub"), [PYTEST], tmp_path)
    for cwd in ("/", str(tmp_path.parent), "..", "", 3, None):
        assert allow_list.match(_call("python -m pytest", cwd=cwd), [PYTEST], tmp_path) is None


def test_a_long_command_or_no_patterns_are_not_auto_approved(tmp_path):
    long = "python -m pytest " + "x" * allow_list.MAX_COMMAND_CHARS
    assert allow_list.match(_call(long), [PYTEST], tmp_path) is None
    assert allow_list.match(_call("python -m pytest"), [], tmp_path) is None


# --- configuration -----------------------------------------------------------------------


def _role(approvals, **extra):
    return {"version": 1, "roles": {"coder": {"harness": "native", **extra,
                                              "approvals": approvals}}}


def test_role_approvals_allow_parses():
    parsed = gy.parse(_role({"allow": [PYTEST, "npm test"]}))
    assert parsed["roles"]["coder"]["approvals"] == {"allow": [PYTEST, "npm test"]}
    assert gy.parse(_role({"allow": []}))["roles"]["coder"]["approvals"] == {"allow": []}


@pytest.mark.parametrize("approvals, path, message", [
    ([], "roles.coder.approvals", "must be a mapping"),
    ({}, "roles.coder.approvals.allow", "is required"),
    ({"allow": "npm test"}, "roles.coder.approvals.allow", "must be a list"),
    ({"allow": ["npm test"], "deny": []}, "roles.coder.approvals.deny", "unknown key"),
    ({"allow": [""]}, "roles.coder.approvals.allow[0]", "non-empty string"),
    ({"allow": ["npm test", 3]}, "roles.coder.approvals.allow[1]", "non-empty string"),
    ({"allow": ["npm test"] + [f"npm run t{i}" for i in range(32)]},
     "roles.coder.approvals.allow", "at most 32"),
    ({"allow": ["npm " + "x" * 200]}, "roles.coder.approvals.allow[0]", "longer than 200"),
    ({"allow": ["python -m pytest; rm *"]}, "roles.coder.approvals.allow[0]", "could never match"),
    ({"allow": ["npm test $X"]}, "roles.coder.approvals.allow[0]", "could never match"),
    ({"allow": ["*"]}, "roles.coder.approvals.allow[0]", "literal program name"),
    ({"allow": ["py* -m pytest"]}, "roles.coder.approvals.allow[0]", "literal program name"),
    ({"allow": ["npm test", "npm  test"]}, "roles.coder.approvals.allow[1]", "duplicate"),
])
def test_role_approvals_are_strict(approvals, path, message):
    with pytest.raises(GarudaConfigError) as refused:
        gy.parse(_role(approvals))
    assert refused.value.path == path and message in str(refused.value)


def _layers(user_allow, project_allow, *, project_has_role=True):
    user = gy.parse(_role({"allow": user_allow})) if user_allow is not None else gy.parse(
        {"version": 1, "roles": {"coder": {"harness": "native"}}})
    project = gy.parse(_role({"allow": project_allow}) if project_allow is not None
                       else {"version": 1, "roles": {"coder": {"harness": "native"}}})
    return gy.resolve(user if project_has_role else None, project).config["roles"]["coder"]


def test_a_project_may_remove_allow_list_patterns():
    assert _layers([PYTEST, "npm test"], ["npm test"])["approvals"] == {"allow": ["npm test"]}
    assert _layers([PYTEST], [])["approvals"] == {"allow": []}
    # A project role that leaves `approvals` out keeps the user's, like `consult`.
    assert _layers([PYTEST], None)["approvals"] == {"allow": [PYTEST]}


@pytest.mark.parametrize("user_allow, project_allow, has_role, path", [
    ([PYTEST], [PYTEST, "npm test"], True, "roles.coder.approvals.allow[1]"),
    (None, ["npm test"], True, "roles.coder.approvals.allow[0]"),
    (None, ["npm test"], False, "roles.coder.approvals.allow"),
])
def test_a_project_cannot_add_allow_list_patterns(user_allow, project_allow, has_role, path):
    with pytest.raises(GarudaConfigError) as refused:
        _layers(user_allow, project_allow, project_has_role=has_role)
    assert refused.value.code == "config.project_widening" and refused.value.path == path


#: Digests of this configuration before #861 (origin/main at 3f7b47d).
GOLDEN = {"config": "7b8652bff8aca5677aea43fd2de00f6efdb67d1de778649e673025452d4716f1",
          "flow": "f336288e317029f7cba19c69b4681e4900d9f7362dad252276d6ac1935ff2437",
          "role": "3996f1ee7cb3e8ced6a8125b71ea4734b1960a3037fbb495a846af2e81d5db17"}


def test_configurations_without_the_key_keep_their_digests(tmp_path):
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.core.acceptance_records import digest
    from garuda.runtime.roles import plan_role

    config = {"version": 1,
              "roles": {"coder": {"harness": "native", "model_id": "m/1", "permissions": "smart"},
                        "reviewer": {"harness": "native", "write_policy": "no-edits"}},
              "flows": {"f": {"steps": [
                  {"id": "build", "role": "coder", "outputs": ["patch"]},
                  {"id": "review", "role": "reviewer", "inputs": ["patch"],
                   "outputs": ["review"]}]}}}
    resolved = gy.resolve(gy.parse(config), None, cli_role="coder")
    plan = plan_role(resolved, prepare_runtime_catalog(str(tmp_path)))
    assert {"config": digest(resolved.config), "flow": digest(resolved.config["flows"]["f"]),
            "role": plan.digest} == GOLDEN
    assert plan.approvals_allow == () and "approvals_allow" not in plan.record()
    # With the key, the role's identity changes and its record names the patterns.
    config["roles"]["coder"]["approvals"] = {"allow": [PYTEST]}
    resolved = gy.resolve(gy.parse(config), None, cli_role="coder")
    allowed = plan_role(resolved, prepare_runtime_catalog(str(tmp_path)))
    assert allowed.digest != GOLDEN["role"] and allowed.record()["approvals_allow"] == [PYTEST]
    assert digest(resolved.config["flows"]["f"]) == GOLDEN["flow"]


# --- who may use it ------------------------------------------------------------------------


def _plan(**fields):
    from garuda.runtime.roles import RolePlan

    return RolePlan(role="coder", runtime_id="fakeacp", kind="acp",
                    **{"approvals_allow": (PYTEST,), **fields})


def test_no_edits_steps_and_read_only_roles_never_use_the_allow_list():
    from garuda.runtime.roles import step_allow_list

    assert step_allow_list(_plan()) == (PYTEST,)
    assert step_allow_list(_plan(), no_edits=True) == ()
    assert step_allow_list(_plan(write_policy="no-edits")) == ()
    assert step_allow_list(_plan(permissions="readonly")) == ()
    assert step_allow_list(None) == ()
    assert step_allow_list(_plan(approvals_allow=())) == ()


@pytest.mark.parametrize("no_edits, fields, passed", [
    (False, {}, True), (True, {}, False), (False, {"permissions": "readonly"}, False),
    (False, {"approvals_allow": ()}, False),
])
async def test_a_flow_step_passes_the_allow_list_only_when_it_may_edit(
        monkeypatch, tmp_path, no_edits, fields, passed):
    from garuda.flows.engine import StepLaunch
    from garuda.flows.launch import launch_step
    from garuda.interfaces import runtime_cli

    seen = {}

    async def fake_run(task, **kwargs):
        seen.update(kwargs)
        return {"session_id": kwargs["session_id"], "status": "completed", "reply": "ok"}

    monkeypatch.setattr(runtime_cli, "run_acp_task", fake_run)
    await launch_step(StepLaunch(flow_session="f", step_id="build", role="coder",
                                 role_plan=_plan(**fields), prompt="p", workspace=str(tmp_path),
                                 capability=None, attempt=1, no_edits=no_edits,
                                 store=SessionStore()))
    assert ("approvals_allow" in seen) is passed
    if passed:
        assert seen["approvals_allow"] == (PYTEST,)


def test_an_allow_listed_ask_that_cannot_be_recorded_is_not_allowed(tmp_path):
    class Store:
        def update_meta(self, *_a, **_k):
            raise OSError("disk full")

    decide = ApprovalBroker(PermissionEngine(), store=Store()).allow_list(
        [PYTEST], tmp_path, session_id="s", runtime_id="fakeacp")
    assert decide("python -m pytest [acp]", _call("python -m pytest")) is None
    unbound = ApprovalBroker(PermissionEngine(), store=None).allow_list(
        [PYTEST], tmp_path, session_id="s", runtime_id="fakeacp")
    assert unbound("python -m pytest [acp]", _call("python -m pytest")) is None


# --- flows against the fake agent -------------------------------------------------------------


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", ".")
    _git(path, "-c", "maintenance.auto=false", "-c", "gc.auto=0", "commit", "-qm", "base")
    return path


def _install(tmp_path, monkeypatch, command, kind="execute", profile="artifacts"):
    from tests.test_runtime_cli import _install_shim, _on_path, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    extra = ("--approval-command", command) if command is not None else ()
    _install_shim(tmp_path / "bin", profile=profile, approval_kind=kind, extra=extra)
    _trusted_settings(tmp_path, monkeypatch)


def _runner(repo, allow, *, long_run=None):
    from garuda.flows.engine import FlowRunner
    from garuda.flows.launch import launch_step

    role = {"harness": "fakeacp", **({"approvals": {"allow": allow}} if allow is not None else {})}
    config = {"version": 1, "roles": {"coder": role},
              "flows": {"one": {"steps": [{"id": "build", "role": "coder",
                                           "outputs": ["summary"]}]}}}
    resolved = gy.resolve(gy.parse(config), None)
    flow = json.loads(json.dumps(resolved.config["flows"]["one"]))
    if long_run is not None:
        flow["steps"][0]["long_run"] = long_run
    return FlowRunner(SessionStore(), repo, "one", flow, resolved, task="build it",
                      launcher=launch_step)


def _approvals():
    (meta,) = [m for m in SessionStore().list_sessions()
               if any(k.startswith("approval:") for k in m)]
    return meta, [v for k, v in meta.items() if k.startswith("approval:")]


async def test_an_allow_listed_command_runs_without_a_person_and_is_audited(
        repo, tmp_path, monkeypatch):
    from garuda.flows import engine

    _install(tmp_path, monkeypatch, "python  -m pytest tests -q")
    result = await asyncio.wait_for(_runner(repo, [PYTEST], long_run={}).run(), 90)
    assert result.completed, result.stopped
    meta, (record,) = _approvals()
    assert record["outcome"] == "allow" and record["decided_by"] == "allow_list"
    assert record["via"] == "allow_list" and record["pattern"] == PYTEST
    assert record["command"] == "python -m pytest tests -q" and record["tool_kind"] == "execute"
    assert record["action"].startswith("python  -m pytest tests -q [acp ")
    assert approval_summary(meta) == {"requested": 1, "allowed": 1, "denied": 0,
                                      "all_denied": False, "by_outcome": {"allow": 1},
                                      "allow_listed": 1}
    assert meta["role"]["approvals_allow"] == [PYTEST]
    # The turn loop's progress does not call an allowed command denied.
    path = engine.flow_dir(SessionStore(), result.flow_session) / "progress" / "build-1.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[-1]["denied"] == [] and rows[-1]["approval_denied"] is False


@pytest.mark.parametrize("command, kind, allow", [
    ("python -m pip install requests", "execute", [PYTEST]),   # no pattern matches
    ("python -m pytest -q; rm -rf ~", "execute", [PYTEST]),    # shell control
    ("python -m pytest $(rm -rf ~)", "execute", [PYTEST]),     # expansion
    ("python -m pytest -q", "fetch", [PYTEST]),                # not a shell command
    ("python -m pytest -q", "edit", [PYTEST]),
    (None, "execute", ["pwd"]),                               # no rawInput.command
    ("python -m pytest -q", "execute", None),                  # no allow-list: as before
])
async def test_anything_else_is_denied_as_before(repo, tmp_path, monkeypatch, command, kind, allow):
    _install(tmp_path, monkeypatch, command, kind=kind)
    result = await asyncio.wait_for(_runner(repo, allow).run(), 90)
    assert not result.completed
    meta, (record,) = _approvals()
    assert record["outcome"] == "deny" and "decided_by" not in record
    assert approval_summary(meta)["denied"] == 1 and "allow_listed" not in approval_summary(meta)


def test_garuda_run_with_the_role_still_asks(tmp_path, monkeypatch, capsys):
    """`garuda run --role` is attended: the allow-list is for unattended flow steps only."""
    from tests.test_runtime_cli import _git_workspace, _main

    ws = _git_workspace(tmp_path)
    _install(tmp_path, monkeypatch, "python -m pytest -q", profile="approval")
    gy.user_path().write_text(
        "version: 1\nroles:\n  coder:\n    harness: fakeacp\n"
        "    approvals: {allow: ['python -m pytest*']}\n")
    code, out = _main(monkeypatch, capsys, "run", "-t", "test it", "--workspace", str(ws),
                      "--role", "coder")
    assert code == 0, out
    meta, (record,) = _approvals()
    assert record["outcome"] == "deny" and "decided_by" not in record
    assert meta["role"]["approvals_allow"] == [PYTEST]
