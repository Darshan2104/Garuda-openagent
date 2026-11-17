"""Public CLI discovery excludes commands used by Garuda's own processes."""

import pytest

from garuda.interfaces.main import build_parser


def test_internal_commands_are_hidden_from_help_and_usage(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(["--help"])
    assert exited.value.code == 0
    help_text = capsys.readouterr().out
    for text in (help_text, parser.format_usage()):
        assert "__worker" not in text
        assert "_consult-mcp" not in text
        assert "==SUPPRESS==" not in text
    assert "run" in help_text and "serve" in help_text
    assert parser.parse_args(["__worker", "test-session"]).command == "__worker"
    assert parser.parse_args(["_consult-mcp"]).command == "_consult-mcp"


def test_invalid_command_lists_only_public_choices(capsys):
    with pytest.raises(SystemExit) as exited:
        build_parser().parse_args(["bogus"])
    assert exited.value.code == 2
    message = capsys.readouterr().err
    assert "invalid choice: 'bogus'" in message
    assert "'run'" in message and "'serve'" in message
    assert "__worker" not in message
    assert "_consult-mcp" not in message
