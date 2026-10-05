"""Parent death cannot prove a foreground runtime's descendants were reaped."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
from garuda.runtime.ownership import Owner, owner_liveness
from garuda.runtime.queue import QueueStore, scope_for

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def surviving_foreground(tmp_path):
    root = tmp_path / "capacity"
    output = tmp_path / "runtime-output"
    code = """
        import json, os, subprocess, sys
        from garuda.runtime.capacity import CapacityStore
        from garuda.runtime.recovery import _process_identity
        CapacityStore(sys.argv[1]).reserve('native', 'foreground', 1)
        child_code = "import sys,time; from pathlib import Path; p=Path(sys.argv[1]); p.write_bytes(b'alive'); end=time.monotonic()+60;\\nwhile time.monotonic()<end:\\n with p.open('ab') as f: f.write(b'.')\\n time.sleep(.02)"
        child = subprocess.Popen([sys.executable, '-c', child_code, sys.argv[2]],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        print(json.dumps({'pid': child.pid, 'identity': _process_identity(child.pid)}), flush=True)
        os._exit(0)
    """
    parent = subprocess.run([sys.executable, "-c", textwrap.dedent(code), str(root), str(output)],
                            env=dict(os.environ, PYTHONPATH=str(ROOT)), start_new_session=True,
                            capture_output=True, text=True, timeout=30)
    assert parent.returncode == 0, parent.stderr
    record = json.loads(parent.stdout)
    child = Owner(record["pid"], record["identity"], record["pid"], "test-child")
    try:
        deadline = time.monotonic() + 10
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert output.exists(), "the surviving runtime never started"
        assert owner_liveness(child.to_dict()) is True
        source, = root.glob("*.json")
        yield CapacityStore(root), source, output, child
    finally:
        if owner_liveness(child.to_dict()) is True:
            os.killpg(child.pid, signal.SIGKILL)


@pytest.mark.parametrize("caller", ["same-holder", "another-holder", "dead-owner-retry", "queue"])
def test_dead_foreground_owner_with_a_live_writer_keeps_capacity(surviving_foreground, tmp_path, caller):
    capacity, source, output, child = surviving_foreground
    before = source.read_bytes()
    written = output.stat().st_size
    if caller == "queue":
        queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
        scope = scope_for("native")
        queue.enqueue(scope, "queued", session_id="queued", config_digest="config")
        assert not queue.try_claim(scope, "queued"), "queue reclaimed an unproved foreground runtime"
    else:
        holder = "foreground" if caller == "same-holder" else "next"
        owner = Owner(**json.loads(before)["slots"]["foreground"]["owner"]) if caller == "dead-owner-retry" else None
        if caller == "dead-owner-retry":
            holder = "foreground"
        with pytest.raises(CapacityUnavailable):
            capacity.reserve("native", holder, 1, owner=owner)
    assert source.read_bytes() == before
    assert capacity.holders("native") == ["foreground"]
    deadline = time.monotonic() + 5
    while output.stat().st_size == written and time.monotonic() < deadline:
        time.sleep(.02)
    assert output.stat().st_size > written, "the runtime must still be writing at the refusal"
    assert owner_liveness(child.to_dict()) is True


def test_an_explicitly_released_foreground_reservation_allows_queue_admission(tmp_path):
    capacity = CapacityStore(tmp_path / "capacity")
    foreground = capacity.reserve("native", "foreground", 1)
    capacity.release(foreground)
    queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
    scope = scope_for("native")
    queue.enqueue(scope, "queued", session_id="queued", config_digest="config")
    assert queue.try_claim(scope, "queued")
    assert capacity.holders("native") == ["queued"]
    assert queue.release(scope, "queued")
