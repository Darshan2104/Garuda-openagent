"""Contract matrix tests for issue #42 (P1.10).

The matrix is generated from declared capabilities; every adapter ships a
CI-readable JSON report; nothing is marked supported without passing.
"""

import json

from garuda.eval.contract_matrix import (
    SCENARIOS,
    AdapterReport,
    CheckResult,
    build_matrix,
    run_entry,
    run_matrix,
)


def test_matrix_covers_vendors_and_declares_known_scenarios():
    entries = build_matrix()
    ids = [e.id for e in entries]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    for vendor in ("claude", "codex", "cursor", "opencode", "pi", "goose"):
        assert vendor in ids
    assert "native" in ids
    for entry in entries:
        assert entry.capabilities <= set(SCENARIOS), entry.id
        assert entry.make is not None


def test_supported_means_zero_failures():
    good = AdapterReport(
        adapter_id="x", kind="fake", capabilities=("lifecycle",),
        results=[CheckResult("lifecycle", "pass"), CheckResult("diff", "skip", "no cap")],
    )
    assert good.supported is True
    bad = AdapterReport(
        adapter_id="y", kind="fake", capabilities=("lifecycle",),
        results=[CheckResult("lifecycle", "fail", "boom")],
    )
    assert bad.supported is False
    assert AdapterReport(adapter_id="z", kind="fake", capabilities=()).supported is False


async def test_single_entry_report_is_ci_readable(tmp_path):
    entries = {e.id: e for e in build_matrix()}
    report = await run_entry(entries["fake-success"], tmp_path / "work")
    assert report.supported is True
    out = tmp_path / "reports"
    out.mkdir()
    (out / "fake-success.json").write_text(json.dumps(report.to_dict(), indent=2))
    loaded = json.loads((out / "fake-success.json").read_text())
    assert loaded["supported"] is True
    assert {r["scenario"] for r in loaded["results"]} >= {"lifecycle", "recovery"}


async def test_full_matrix_marks_every_adapter_supported(tmp_path):
    reports = await run_matrix(tmp_path / "reports", work_root=tmp_path / "work")
    assert reports, "matrix must not be empty"
    failures = [
        (r.adapter_id, c.scenario, c.detail)
        for r in reports
        for c in r.results
        if c.status == "fail"
    ]
    assert failures == [], failures
    assert all(r.supported for r in reports)
    summary = json.loads((tmp_path / "reports" / "summary.json").read_text())
    assert summary["unsupported"] == []
    assert len(summary["supported"]) == len(reports)
