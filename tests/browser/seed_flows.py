"""Seed the Flows page check: user flows of every shape and one project flow.

    python seed_flows.py ROOT

Writes ROOT/settings/garuda.yaml (the user file beside GARUDA_GLOBAL_SETTINGS, which the
caller points at ROOT/settings/settings.yaml), a Git workspace ROOT/workspace whose own
garuda.yaml defines one flow, and ROOT/manifest.json naming both. Native roles only: the
check previews and never starts here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from garuda.config import garuda_yaml as gy

ROLES = ("scout", "planner", "coder", "reviewer")


def main(root: Path) -> None:
    settings = root / "settings"
    settings.mkdir(parents=True)
    gy.user_path().write_text(gy.dump({
        "version": 1,
        "roles": {role: {"harness": "native", "model_id": f"{role}/model"} for role in ROLES},
        "flows": {
            "two-step": {"description": "Look around, then plan", "steps": [
                {"id": "look", "role": "scout", "write_policy": "no-edits", "outputs": ["notes"]},
                {"id": "make", "role": "coder", "inputs": [{"type": "notes", "from": "look"}],
                 "outputs": ["plan"], "limits": {"deadline_min": 20}}]},
            "review-loop": {"description": "Build with a review loop", "steps": [
                {"id": "build", "role": "coder", "outputs": ["patch"],
                 "review": {"by": "reviewer", "max_rounds": 2, "independent": False}},
                {"id": "check", "role": "reviewer", "write_policy": "no-edits",
                 "inputs": ["patch"], "outputs": ["review"]}]},
            "fan-out": {"description": "Two scouts at once", "steps": [
                {"id": "look", "parallel": ["scout", "reviewer"], "outputs": ["notes"]},
                {"id": "make", "role": "coder", "inputs": ["notes"], "outputs": ["plan"]}]},
        },
    }))
    workspace = root / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "garuda.yaml").write_text(gy.dump({"version": 1, "flows": {"team-flow": {
        "description": "Shipped by this repository",
        "steps": [{"id": "only", "role": "coder", "outputs": ["plan"]}]}}}))
    (root / "manifest.json").write_text(json.dumps({
        "workspace": str(workspace.resolve()), "settings": str(settings / "settings.yaml")}))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
