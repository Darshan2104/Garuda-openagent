"""The paired-report CLI is a read-only transport over the report service."""

import json
import sys

import pytest
from test_paired_report import _manifest, _refs

from garuda.core.sessions import SessionStore
from garuda.interfaces.main import build_parser, main


def _main(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["garuda", *argv])
    with pytest.raises(SystemExit) as exited:
        main()
    captured = capsys.readouterr()
    return exited.value.code, captured.out, captured.err


def _command(tmp_path, *, bad_candidate=False, require_passing_gates=False):
    store = SessionStore(tmp_path / "sessions")
    manifest, _assignments = _manifest(tmp_path)
    baselines, candidates = _refs(store, bad_candidate=bad_candidate)
    command = ["eval", "dual-model", "report", "--sessions-dir", str(store.root)]
    for reference in baselines:
        command.extend(["--baseline", f"{reference.task_id}={reference.session_id}"])
    for reference in candidates:
        command.extend(["--candidate", f"{reference.task_id}={reference.session_id}"])
    command.extend(
        [
            "--task-mix", str(manifest),
            "--model-version", "reasoning=provider/model@2026-09-29",
            "--price-source", "provider invoice",
            "--prompt-revision", "sha256:revision",
            "--output", str(tmp_path / "report.json"),
        ]
    )
    if require_passing_gates:
        command.append("--require-passing-gates")
    return command


def test_parser_exposes_report_only_inputs():
    args = build_parser().parse_args(
        [
            "eval", "dual-model", "report", "--task-mix", "mix.json",
            "--price-source", "invoice", "--prompt-revision", "rev", "--output", "out.json",
        ]
    )
    assert args.command == "eval"
    assert args.eval_command == "dual-model"
    assert args.dual_model_command == "report"
    assert args.baseline == args.candidate == []


def test_cli_writes_valid_report_without_provider_setup(tmp_path, monkeypatch, capsys):
    def provider_setup_must_not_run(*_args, **_kwargs):
        raise AssertionError("the report command must not prepare a provider-backed run")

    monkeypatch.setattr("garuda.agents.setup.prepare_agent_run", provider_setup_must_not_run)
    code, output, errors = _main(monkeypatch, capsys, *_command(tmp_path))

    assert code == 0
    assert "wrote paired report" in output
    assert not errors
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["release_gates_passed"] is True


def test_cli_can_require_gate_pass_after_writing_regression_report(tmp_path, monkeypatch, capsys):
    code, output, errors = _main(
        monkeypatch, capsys, *_command(tmp_path, bad_candidate=True, require_passing_gates=True)
    )

    assert code == 3
    assert "release gates=FAIL" in output
    assert not errors
    assert (tmp_path / "report.json").is_file()


def test_cli_rejects_missing_provenance_before_writing(tmp_path, monkeypatch, capsys):
    command = _command(tmp_path)
    price_source = command.index("--price-source")
    del command[price_source:price_source + 2]

    code, output, errors = _main(monkeypatch, capsys, *command)

    assert code == 2
    assert "required" in errors
    assert not output
    assert not (tmp_path / "report.json").exists()
