"""Read retained decision observations from real session fixtures."""

import json


def decision_records(store, session_id):
    directory = store.session_dir(session_id) / "decision-audit"
    return [json.loads(line) for name in ("previous.jsonl", "current.jsonl")
            if (directory / name).exists() for line in (directory / name).read_text().splitlines()]
