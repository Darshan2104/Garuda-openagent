"""CLI stop and refusal messages say their code once and leak no paths (#796, #797).

#796: a step whose role is refused while its plan is built stops the flow at the
step id (not the role name), and ``flow run`` / ``flow resume`` / ``flow show``
print the stop code once.
#797: ``garuda approvals answer`` on an id with no parked request is a typed
``approval.not_found`` refusal (exit 2) that never prints the internal path.
"""

import json

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from tests.test_flow_stop_cause import repo  # noqa: F401  (fixture)

CONFIG = ("version: 1\nroles:\n"
          "  bad-coder: {harness: fakeacp, permissions: yolo, harness_mode: nosuchmode}\n"
          "flows:\n  bad:\n    steps:\n      - {id: build, role: bad-coder, outputs: [patch]}\n")
SID = "11111111-2222-3333-4444-555555555555"


def _stopped_line(out: str) -> str:
    return next(line for line in out.splitlines() if "flow stopped:" in line)


def test_a_role_plan_refusal_stops_at_the_step_and_says_its_code_once(
        repo, tmp_path, monkeypatch, capsys):  # noqa: F811
    from tests.test_runtime_cli import _install_shim, _main, _on_path, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(CONFIG)

    code, out = _main(monkeypatch, capsys, "flow", "run", "bad", "-t", "do nothing",
                      "--workspace", str(repo))
    assert code == 3, out
    stopped = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert stopped["code"] == "role.mode_unknown"
    line = _stopped_line(out)
    assert line.count("role.mode_unknown") == 1, line
    assert "flow stopped: role.mode_unknown: " in line

    flow_id = stopped["flow_session"]
    flow_stop = SessionStore().load_meta(flow_id)["flow_stop"]
    assert flow_stop["step"] == "build"  # the step id, not the role name
    assert flow_stop["message"].count("role.mode_unknown") == 1

    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id, "--workspace", str(repo))
    assert code == 3, out
    assert _stopped_line(out).count("role.mode_unknown") == 1, out

    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo))
    assert code == 0, out
    assert "stopped at step build: role.mode_unknown: " in out
    assert "bad-coder:" not in out and out.count("role.mode_unknown") == 1, out


def test_a_stop_built_from_a_refusal_says_its_code_once():
    from garuda.runtime.roles import RoleRefused

    refusal = RoleRefused("role.mode_unknown", "no such mode")
    stop = engine.FlowStopped(refusal.code, str(refusal), step="build")
    assert str(stop) == "role.mode_unknown: no such mode"
    assert str(engine.FlowStopped("flow.x", "plain")) == "flow.x: plain"


def _answer(tmp_path, capsys, *extra):
    from garuda.interfaces.main import build_parser, run_approvals

    store = SessionStore()
    store.begin(SID, task="t", model="m", agent="a", workspace=str(tmp_path))
    args = build_parser().parse_args(["approvals", "answer", SID, "abc", *extra,
                                      "--workspace", str(tmp_path)])
    code = run_approvals(args)
    return code, capsys.readouterr().err, store


@pytest.mark.parametrize("extra", [("--allow",), ("--deny", "--digest", "0" * 64)],
                         ids=["allow", "deny-digest"])
def test_answering_an_unknown_approval_is_a_typed_not_found(tmp_path, capsys, extra):
    code, err, store = _answer(tmp_path, capsys, *extra)
    assert code == 2
    assert err.startswith(f"Error: approval.not_found: no parked approval 'abc' for session {SID}")
    assert "Errno" not in err and "request.json" not in err
    assert str(store.session_dir(SID)) not in err and "/" not in err.split(SID, 1)[1]


def test_an_os_error_answering_an_approval_hides_its_path(tmp_path, capsys, monkeypatch):
    from garuda.acp import approval_channel

    def broken(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", str(tmp_path / "secret" / "abc.answer.json"))

    monkeypatch.setattr(approval_channel, "write_answer", broken)
    code, err, _ = _answer(tmp_path, capsys, "--allow")
    assert code == 2
    assert err.startswith("Error: approval.unavailable: cannot answer 'abc': Permission denied")
    assert str(tmp_path) not in err
