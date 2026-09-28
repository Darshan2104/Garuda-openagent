import json

import pytest
from test_collection_budget import _coordinator, _submission

from garuda.model.config import CollectionBudget
from garuda.model.script_model import ScriptModel


class MutatingModel(ScriptModel):
    def __init__(self, path):
        super().__init__([])
        self.path = path

    async def complete(self, *args, **kwargs):
        self.path.write_text("after", encoding="utf-8")
        return _submission()


@pytest.mark.asyncio
async def test_workspace_mutation_marks_interactive_report_stale(tmp_path):
    target = tmp_path / "source.txt"
    target.write_text("before", encoding="utf-8")
    coordinator = _coordinator(
        tmp_path, MutatingModel(target), CollectionBudget()
    )
    payload = json.loads(
        await coordinator.delegate(
            {
                "objective": "inspect",
                "questions": ["what?"],
                "allowed_paths": ["source.txt"],
            }
        )
    )
    assert payload["state"] == "completed_stale"
    assert payload["report"]["stale"] is True
    assert "scoped_paths_changed" in payload["stale_reasons"]


@pytest.mark.asyncio
async def test_strict_mode_blocks_known_background_writer(tmp_path):
    model = ScriptModel([_submission()])
    coordinator = _coordinator(
        tmp_path,
        model,
        CollectionBudget(),
        parent_mode="rigorous",
        background_writer=lambda: True,
    )
    with pytest.raises(ValueError, match="background workspace writer"):
        await coordinator.delegate({"objective": "inspect", "questions": ["what?"]})
    assert len(model._responses) == 1
