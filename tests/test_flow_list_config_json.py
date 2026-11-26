"""`garuda flow list` and `garuda config show --json` (#782)."""

import json
import subprocess
from pathlib import Path

from garuda.config import garuda_yaml as gy
from garuda.flows import packaged

USER = """\
version: 1
roles:
  coder: {harness: native}
  reviewer: {harness: native}
flows:
  solo:
    description: Just the coder.
    steps:
      - {id: build, role: coder, outputs: [patch]}
  pair:
    steps:
      - {id: build, role: coder, outputs: [patch], review: {by: reviewer, max_rounds: 1}}
      - {id: review, role: reviewer, write_policy: no-edits, inputs: [patch], outputs: [review]}
"""

PROJECT = """\
version: 1
flows:
  team:
    steps:
      - {id: build, role: coder, outputs: [patch]}
      - {id: review, role: reviewer, write_policy: no-edits, inputs: [patch], outputs: [review]}
checks: [make test]
"""


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def _main(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main as main

    return main(monkeypatch, capsys, *argv)


def _configure(tmp_path) -> Path:
    ws = _repo(tmp_path / "ws")
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(USER)
    (ws / "garuda.yaml").write_text(PROJECT)
    return ws


def test_flow_list_json_names_packaged_user_and_project_flows(tmp_path, monkeypatch, capsys):
    ws = _configure(tmp_path)
    code, out = _main(monkeypatch, capsys, "flow", "list", "--workspace", str(ws), "--json")
    assert code == 0
    rows = {row["name"]: row for row in json.loads(out)}
    assert set(rows) == {"plan-only", "pair", "plan-build-review", "solo", "team"}
    assert rows["plan-only"]["source"] == "packaged"
    assert rows["plan-only"]["missing_roles"] == ["scout", "planner"]
    # a user flow of a packaged name replaces it
    assert rows["pair"]["source"] == "user" and rows["pair"]["steps"] == 2
    assert rows["solo"] == {"name": "solo", "source": "user", "description": "Just the coder.",
                            "steps": 1, "roles": ["coder"], "missing_roles": []}
    assert rows["team"]["source"] == "project"
    assert rows["team"]["roles"] == ["coder", "reviewer"]


def test_flow_list_text_without_any_config_lists_the_packaged_flows(tmp_path, monkeypatch,
                                                                    capsys):
    ws = _repo(tmp_path / "ws")
    code, out = _main(monkeypatch, capsys, "flow", "list", "--workspace", str(ws))
    assert code == 0
    lines = out.splitlines()
    assert [line.split()[0] for line in lines] == sorted(packaged.FLOWS)
    assert all("packaged" in line for line in lines)
    assert "3 step(s)  roles: planner, coder, reviewer; undefined roles:" in out


def test_flow_list_reports_an_invalid_config(tmp_path, monkeypatch, capsys):
    ws = _repo(tmp_path / "ws")
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text("version: 1\nflows: {x: {steps: [{id: a, role: ghost}]}}\n")
    code, out = _main(monkeypatch, capsys, "flow", "list", "--workspace", str(ws))
    assert code == 2 and "Error:" in out


def test_config_show_json_matches_the_text_view(tmp_path, monkeypatch, capsys):
    ws = _configure(tmp_path)
    code, out = _main(monkeypatch, capsys, "config", "show", "--workspace", str(ws), "--json")
    assert code == 0
    doc = json.loads(out)
    resolved = gy.load_effective(str(ws))
    assert doc["config"] == json.loads(json.dumps(resolved.config))
    assert doc["provenance"]["roles.coder"] == "user-config"
    assert doc["provenance"]["flows.team"] == "trusted-project"
    assert doc["withheld"] == ["checks[0]"]
    code, text = _main(monkeypatch, capsys, "config", "show", "--workspace", str(ws))
    assert code == 0
    for key, source in doc["provenance"].items():
        assert f"# {key}: {source}" in text


def test_config_show_json_without_any_config(tmp_path, monkeypatch, capsys):
    ws = _repo(tmp_path / "ws")
    code, out = _main(monkeypatch, capsys, "config", "show", "--workspace", str(ws), "--json")
    assert code == 0
    assert json.loads(out) == {"config": None, "provenance": {}, "withheld": []}
