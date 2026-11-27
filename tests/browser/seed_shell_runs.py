"""Sessions for the app-shell and Runs-list checks: every state, plus more than one list window.

    GARUDA_GLOBAL_SETTINGS=<file> python seed_shell_runs.py <sessions-dir> <workspace> [manifest.json]

``seed_sessions.seed`` writes one session in each interesting state through the production
writers (queue, flow, approval, crash). On top of that, BULK sessions with long,
near-identical tasks — more than the old 500-session window the run list read — are cloned
on disk from one ``SessionStore.begin`` record (only the id, name, task and time differ), so
building them takes seconds rather than minutes. Every FIB_EVERY-th task mentions
"fibonacci", the oldest included, so a store-wide search has matches outside the newest 500.
Prints a JSON manifest the checks read.
"""

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from garuda.core.sessions import SessionStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_sessions import seed as seed_states  # noqa: E402

BULK = 1150
FIB_EVERY = 10
TASK = ("Refactor the {topic} module so the public API stays stable, add property tests for the "
        "edge cases we found last week, update the changelog and keep the CLI output identical "
        "(variant {i})")


def bulk_id(i: int) -> str:
    return f"20000000-0000-0000-0000-{i:012d}"


def seed(root: Path, workspace: str) -> dict:
    ids = seed_states(root, workspace)
    store = SessionStore(root)
    store.begin("bulk-template", task="template", model="openrouter/anthropic/claude-sonnet-4",
                agent="build", workspace=workspace, name="bulk-template")
    template = json.loads((store.session_dir("bulk-template") / "meta.json").read_text())
    shutil.rmtree(store.session_dir("bulk-template"))
    # Older than the state fixtures, so those stay on the first page.
    start = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(BULK):
        stamp = (start + timedelta(minutes=i)).isoformat()
        topic = "fibonacci" if i % FIB_EVERY == 0 else "parser"
        meta = {**template, "session_id": bulk_id(i), "name": f"bulk-{i}",
                "task": TASK.format(topic=topic, i=i), "created_at": stamp, "updated_at": stamp,
                "agent": "plan" if i % 3 == 0 else "build"}
        directory = store.session_dir(bulk_id(i))
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"ids": ids, "bulk": BULK, "fib": len(range(0, BULK, FIB_EVERY)),
            "oldest": bulk_id(0), "total": len(store.list_sessions(limit=None))}


if __name__ == "__main__":
    manifest = json.dumps(seed(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "/tmp"))
    if len(sys.argv) > 3:  # where the checks read it (GARUDA_CHECK_SHELL_MANIFEST)
        Path(sys.argv[3]).write_text(manifest, encoding="utf-8")
    print(manifest)
