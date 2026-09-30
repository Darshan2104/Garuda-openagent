"""The maintained documentation must describe commands in Garuda's real parser."""

import importlib.util
import pathlib

from garuda.interfaces.main import build_parser

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_docs.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_docs_commands", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def test_maintained_docs_commands_match_real_cli_parser():
    errors = checker.find_invalid_documented_commands(REPO_ROOT, build_parser())
    assert errors == []
