"""Contract matrix tests for issue #42 (P1.10).

Identities are pinned (adapters and scenarios), expectations derive from
declared capabilities and behaviors, vendor rows are labeled simulated, and
support requires a PASS — all-SKIP never passes.
"""

import json

from garuda.eval.contract_matrix import (
    PROFILE_BEHAVIORS,
    SCENARIOS,
    AdapterReport,
    CheckResult,
    build_matrix,
    run_entry,
    run_matrix,
)


def test_identities_are_pinned():
    entries = build_matrix()
    ids = [e.id for e in entries]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert set(ids) == (
        {f"fake-{name}" for name in ("success", "streaming", "approval", "cancellation")}
        | {f"acp-{name}" for name in ("success", "streaming", "approval", "slow", "diff")}
        | {"claude", "codex", "cursor", "opencode", "pi", "goose", "native"}
    )
    assert tuple(SCENARIOS) == (
        "lifecycle", "cancellation", "permission", "diff", "resume", "handoff", "recovery",
    )
    # Behavior tables stay keyed to real, pinned contracts.
    from garuda.acp.fake_agent import PROFILES

    assert set(PROFILE_BEHAVIORS) <= set(PROFILES)
    assert set(PROFILE_BEHAVIORS) == {"success", "streaming", "approval", "slow", "diff"}
    from garuda.runtime.fake import FakeScenario

    assert {s.value for s in FakeScenario} >= {"success", "streaming", "approval", "cancellation"}


def test_expectations_derive_from_declarations():
    from garuda.eval.contract_matrix import SCENARIO_BEHAVIORS, SCENARIO_CAPS

    entries = {e.id: e for e in build_matrix()}
    for entry in entries.values():
        assert entry.capabilities <= {"prompt", "cancel"}, entry.id
        assert entry.make is not None
        for scenario in SCENARIOS:
            expected = (
                SCENARIO_CAPS[scenario] <= entry.capabilities
                and SCENARIO_BEHAVIORS[scenario] <= entry.behaviors
            )
            assert (scenario in entry.scenarios()) == expected, (entry.id, scenario)
    vendors = {e.id: e for e in entries.values() if e.kind == "vendor"}
    assert set(vendors) == {"claude", "codex", "cursor", "opencode", "pi", "goose"}
    for vendor in vendors.values():
        assert vendor.simulated is True
        # Manifest prompt/cancel caps: lifecycle + cancellation + wiring run.
        assert set(vendor.scenarios()) == {
            "lifecycle", "cancellation", "resume", "handoff", "recovery",
        }
    native = entries["native"]
    assert native.simulated is False
    assert set(native.scenarios()) == set(SCENARIOS)


def test_supported_means_zero_failures():
    good = AdapterReport(
        adapter_id="x", kind="fake", capabilities=("prompt", "cancel"),
        results=[CheckResult("lifecycle", "pass"), CheckResult("diff", "skip", "no cap")],
    )
    assert good.supported is True
    bad = AdapterReport(
        adapter_id="y", kind="fake", capabilities=("prompt", "cancel"),
        results=[CheckResult("lifecycle", "fail", "boom")],
    )
    assert bad.supported is False
    assert AdapterReport(adapter_id="z", kind="fake", capabilities=()).supported is False
    all_skip = AdapterReport(
        adapter_id="w", kind="fake", capabilities=(),
        results=[CheckResult("lifecycle", "skip", "no cap")],
    )
    assert all_skip.supported is False, "all-SKIP must never pass"


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
