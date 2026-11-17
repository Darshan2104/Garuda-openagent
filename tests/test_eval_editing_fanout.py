"""The offline measurement must report real outcomes and settled shared spend."""

import importlib.util
import json
import socket
from pathlib import Path

import pytest

from garuda.core.editing_worker import LocalEditingWorkers


@pytest.fixture(scope="module")
def measurement():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "measure_editing_fanout", root / "scripts" / "measure_editing_fanout.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_measurement_reports_fresh_bases_stale_siblings_and_serial_integration(
    tmp_path, monkeypatch, measurement,
):
    # Primary owner: the measurement artifact. Worker locking itself is covered
    # by test_editing_workers; this catches dropped/reordered rows, concurrent
    # integration dispatch, invented usage and a baseline that reuses stale bases.
    def no_network(*args, **kwargs):
        raise AssertionError("offline measurement attempted a socket connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    integrate = LocalEditingWorkers.integrate
    active, peak = 0, 0
    observed = []

    async def observe(self, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            outcome = await integrate(self, *args, **kwargs)
            observed.append((outcome["state"], outcome.get("code")))
            return outcome
        finally:
            active -= 1

    monkeypatch.setattr(LocalEditingWorkers, "integrate", observe)
    destination = tmp_path / "reports" / "fanout.json"
    report = await measurement.run_measurement(destination, delay=0.001)
    saved = json.loads(destination.read_text())
    assert saved == report
    assert report["measurements"][1]["integrated"] == 4
    assert report["schema_version"] == 2
    assert report["provider_calls"] == 0 and report["provider_cost_usd"] == 0
    assert peak == 1 and active == 0
    assert [(row["scenario"], row["max_concurrent"]) for row in saved["measurements"]] == [
        ("disjoint", 1), ("disjoint", 4), ("overlapping", 1), ("overlapping", 4),
    ]
    reported = []
    for row in saved["measurements"]:
        assert row["fixture_passed"]
        assert row["outcomes"] == {"ready": 4}
        assert row["launches"] == 4 and row["scripted_calls"] == 8
        gate = row["spend_gate"]
        assert gate["calls"] == 8 and gate["settled_tokens"] == 960
        assert gate["reserved_tokens"] == gate["reserved_calls"] == 0
        assert gate["settled_cost_usd"] == gate["cost_unknown_calls"] == gate["estimated_token_calls"] == 0
        assert [candidate["task"] for candidate in row["candidates"]] == ["alpha", "beta", "gamma", "delta"]
        for candidate in row["candidates"]:
            assert candidate["success"] and candidate["output"] == {"edited": True}
            assert candidate["scripted_tokens"] == 240
            reported.append((candidate["integration"]["state"], candidate["integration"].get("code")))
        assert [(item["task"], item["phase"]) for item in row["integration_trace"]] == [
            (task, phase) for task in ("alpha", "beta", "gamma", "delta")
            for phase in ("integration_start", "integration_end")
        ]
        timestamps = [item["seconds"] for item in row["integration_trace"]]
        assert timestamps == sorted(timestamps)  # Ordering only; no duration/speed threshold.
        if row["max_concurrent"] == 1 or row["scenario"] == "disjoint":
            assert row["rebased_integrations"] == (3 if row["max_concurrent"] == 4 else 0)
            assert row["work_discarded"]["worker_seconds"] == 0
            assert all(candidate["integration"] == {"state": "integrated"} for candidate in row["candidates"])
            assert row["integrated"] == 4 and row["stale_base_refusals"] == 0
            assert row["work_discarded"]["candidates"] == row["work_discarded"]["scripted_tokens"] == 0
            expected = ({"shared.txt": "delta\n"} if row["scenario"] == "overlapping" else
                        {f"{task}.txt": f"{task}\n" for task in ("alpha", "beta", "gamma", "delta")})
        else:
            assert row["rebased_integrations"] == 0
            assert all(candidate["rebase"]["reason"] == "overlap" for candidate in row["candidates"][1:])
            assert row["integrated"] == 1 and row["stale_base_refusals"] == 3
            assert row["work_discarded"]["candidates"] == 3
            assert row["work_discarded"]["scripted_tokens"] == 720
            assert [candidate["integration"] for candidate in row["candidates"]] == [
                {"state": "integrated"}, *[{"state": "refused", "code": "integration.stale_base"}] * 3,
            ]
            expected = ({"shared.txt": "alpha\n"} if row["scenario"] == "overlapping" else
                        {"alpha.txt": "alpha\n", "beta.txt": "original\n", "gamma.txt": "original\n",
                         "delta.txt": "original\n"})
        assert row["final_files"] == expected
    assert reported == observed
