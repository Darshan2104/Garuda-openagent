"""`flow run` and `run` refuse a bad workspace or an empty task before any
session, lease or agent exists (#774, #775)."""

from __future__ import annotations

import pytest

from garuda.config import garuda_yaml as gy
from garuda.config.garuda_yaml import GarudaConfigError
from garuda.flows import service as flow_service
from tests.test_runtime_cli import _main


@pytest.fixture
def no_launch(monkeypatch):
    """Fail the test if a flow service is ever built (no session, no agent)."""
    def refuse(self, *args, **kwargs):
        raise AssertionError("flow service must not start")

    monkeypatch.setattr(flow_service.FlowExecutionService, "__init__", refuse)


@pytest.fixture
def captured_run(monkeypatch):
    calls = []

    async def run(self, name, task, workspace, *, starter_record=None, checks=None):
        calls.append((name, task, workspace))
        raise ValueError("stub: stopped after input validation")

    monkeypatch.setattr(flow_service.FlowExecutionService, "run", run)
    return calls


def test_flow_run_refuses_a_missing_workspace(tmp_path, monkeypatch, capsys, no_launch):
    missing = tmp_path / "nope"
    code, out = _main(monkeypatch, capsys, "flow", "run", "one", "-t", "x",
                      "--workspace", str(missing))
    assert code == 2
    assert out.strip() == f"Error: workspace.missing: {missing} does not exist"
    assert "Traceback" not in out


def test_flow_run_refuses_a_file_workspace(tmp_path, monkeypatch, capsys, no_launch):
    afile = tmp_path / "hostname"
    afile.write_text("h\n")
    code, out = _main(monkeypatch, capsys, "flow", "run", "one", "-t", "x",
                      "--workspace", str(afile))
    assert code == 2
    assert out.strip() == f"Error: workspace.not_directory: {afile} is not a directory"
    assert "symlink" not in out


@pytest.mark.parametrize("task", ["", "   ", "\n\t"])
def test_flow_run_refuses_an_empty_task(tmp_path, monkeypatch, capsys, no_launch, task):
    code, out = _main(monkeypatch, capsys, "flow", "run", "one", "-t", task,
                      "--workspace", str(tmp_path))
    assert code == 1
    assert out.strip() == "Error: task.missing: provide -t/--task or -f/--file"


def test_flow_run_refuses_no_task_and_an_empty_task_file(tmp_path, monkeypatch, capsys,
                                                          no_launch):
    code, out = _main(monkeypatch, capsys, "flow", "run", "one", "--workspace", str(tmp_path))
    assert (code, out.strip()) == (1, "Error: task.missing: provide -t/--task or -f/--file")
    empty = tmp_path / "task.md"
    empty.write_text("  \n")
    code, out = _main(monkeypatch, capsys, "flow", "run", "one", "-f", str(empty),
                      "--workspace", str(tmp_path))
    assert (code, out.strip()) == (1, "Error: task.missing: provide -t/--task or -f/--file")


def test_flow_run_reports_an_unreadable_task_file(tmp_path, monkeypatch, capsys, no_launch):
    code, out = _main(monkeypatch, capsys, "flow", "run", "one", "-f", str(tmp_path / "none"),
                      "--workspace", str(tmp_path))
    assert code == 2
    assert out.startswith("Error: task.file_unreadable:")
    assert "Traceback" not in out


def test_flow_run_reads_the_task_from_a_file(tmp_path, monkeypatch, capsys, captured_run):
    task_file = tmp_path / "task.md"
    task_file.write_text("add retry handling\n")
    code, _ = _main(monkeypatch, capsys, "flow", "run", "one", "-f", str(task_file),
                    "--workspace", str(tmp_path))
    assert code == 2  # the stub stops right after validation
    assert captured_run == [("one", "add retry handling\n", str(tmp_path))]


@pytest.mark.parametrize("argv,code,message", [
    (["-t", "   "], 1, "Error: task.missing: provide -t/--task or -f/--file"),
    (["-t", "x", "--workspace", "{missing}"], 2,
     "Error: workspace.missing: {missing} does not exist"),
    (["-t", "x", "--workspace", "{file}"], 2,
     "Error: workspace.not_directory: {file} is not a directory"),
])
def test_run_refuses_the_same_inputs(tmp_path, monkeypatch, capsys, argv, code, message):
    afile = tmp_path / "f"
    afile.write_text("x")
    fill = {"missing": str(tmp_path / "nope"), "file": str(afile)}
    monkeypatch.chdir(tmp_path)
    got, out = _main(monkeypatch, capsys, "run", *[a.format(**fill) for a in argv])
    assert got == code
    assert out.strip() == message.format(**fill)


def test_a_file_workspace_is_not_reported_as_a_symlink(tmp_path):
    afile = tmp_path / "f"
    afile.write_text("x")
    with pytest.raises(GarudaConfigError) as refused:
        gy.load_effective(afile)
    assert "symlink" not in str(refused.value)
