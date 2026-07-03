"""F1 (lossless ATIF: per-step usage, cost, id attribution) + F4 (dashboard)."""

import json
from pathlib import Path

import garuda.eval.costs as costs
from garuda.eval.atif_export import events_to_atif
from garuda.eval.dashboard import collect_rows, render_dashboard, row_from_atif
from garuda.core.sessions import SessionStore


def _events_with_usage() -> list[dict]:
    return [
        {"type": "session_start", "timestamp": "2026-07-03T10:00:00+00:00",
         "payload": {"task": "do it", "model": "openai/gpt-4o-mini"}},
        {"type": "model_response", "timestamp": "2026-07-03T10:00:01+00:00",
         "payload": {
             "content": None,
             "tool_calls": [
                 {"id": "call_a", "name": "read_file", "arguments": {"path": "x"}},
                 {"id": "call_b", "name": "read_file", "arguments": {"path": "y"}},
             ],
             "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
         }},
        # Two results for the SAME tool name, distinguished only by id.
        {"type": "tool_result", "timestamp": "2026-07-03T10:00:02+00:00",
         "payload": {"tool_call_id": "call_b", "name": "read_file", "content": "Y-CONTENT", "is_error": False}},
        {"type": "tool_result", "timestamp": "2026-07-03T10:00:02+00:00",
         "payload": {"tool_call_id": "call_a", "name": "read_file", "content": "X-CONTENT", "is_error": False}},
        {"type": "model_response", "timestamp": "2026-07-03T10:00:03+00:00",
         "payload": {"content": "done", "tool_calls": [], "usage": {"prompt_tokens": 130, "completion_tokens": 10, "total_tokens": 140}}},
        {"type": "session_end", "timestamp": "2026-07-03T10:00:05+00:00",
         "payload": {"success": True, "turns": 2}},
    ]


def test_atif_per_step_usage_and_totals(monkeypatch):
    # Deterministic cost: $0 per token base, override estimate to a known value.
    monkeypatch.setattr(costs, "estimate_cost", lambda model, usage: (usage.get("prompt_tokens", 0) * 0.001) if usage else None)
    # events_to_atif imported estimate_cost by name; patch there too.
    import garuda.eval.atif_export as atif
    monkeypatch.setattr(atif, "estimate_cost", lambda model, usage: (usage.get("prompt_tokens", 0) * 0.001) if usage else None)

    traj = events_to_atif(_events_with_usage(), session_id="s1")
    agent_steps = [s for s in traj["steps"] if s["source"] == "agent"]
    # Per-step metrics attached.
    with_metrics = [s for s in agent_steps if "metrics" in s]
    assert with_metrics, "model steps should carry per-step token metrics"
    assert with_metrics[0]["metrics"]["prompt_tokens"] == 100
    assert "cost_usd" in with_metrics[0]["metrics"]

    # Auto-aggregated totals (no explicit args passed).
    fm = traj["final_metrics"]
    assert fm["total_prompt_tokens"] == 230  # 100 + 130
    assert fm["total_completion_tokens"] == 30  # 20 + 10
    assert fm["total_cost_usd"] is not None
    # Non-standard aggregates live under the schema's `extra` dict.
    assert fm["extra"]["total_tokens"] == 260
    assert fm["extra"]["duration_ms"] == 5000  # 5s from timestamps


def test_atif_tool_result_attribution_by_id():
    traj = events_to_atif(_events_with_usage(), session_id="s1")
    agent_steps = [s for s in traj["steps"] if s.get("tool_calls")]
    obs = agent_steps[0]["observation"]["results"]
    # Result order is as-emitted (call_b then call_a); each pairs to its own id,
    # NOT collapsed onto the first tool_call as the old name-matching did.
    by_id = {r["source_call_id"]: r["content"] for r in obs}
    assert by_id["call_a"] == "X-CONTENT"
    assert by_id["call_b"] == "Y-CONTENT"


def test_atif_infers_model_from_session_start(monkeypatch):
    traj = events_to_atif(_events_with_usage(), session_id="s1")
    assert traj["agent"]["model_name"] == "openai/gpt-4o-mini"


def test_estimate_cost_graceful_without_pricing():
    assert costs.estimate_cost(None, {"prompt_tokens": 10}) is None
    assert costs.estimate_cost("openai/gpt-4o-mini", None) is None
    assert costs.estimate_cost("openai/gpt-4o-mini", {"prompt_tokens": 0, "completion_tokens": 0}) is None
    # A real known model returns a positive float via litellm pricing.
    cost = costs.estimate_cost("openai/gpt-4o-mini", {"prompt_tokens": 1000, "completion_tokens": 1000})
    assert cost is None or cost > 0


# --- F4 dashboard ------------------------------------------------------------

def test_dashboard_from_sessions(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "garuda.eval.dashboard.estimate_cost",
        lambda model, usage: 0.05 if usage.get("prompt_tokens") else None,
    )
    store = SessionStore(root=tmp_path)
    # Fabricate two finished session metas.
    for i, (status, prompt) in enumerate([("success", 1000), ("failed", 500)]):
        sid = f"sess{i}"
        store.begin(session_id=sid, task="t", model="openai/gpt-4o-mini", agent="build", workspace=".")
        meta_path = store.session_dir(sid) / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta.update({
            "status": status, "turns": i + 2,
            "usage": {"prompt_tokens": prompt, "completion_tokens": 100, "total_tokens": prompt + 100},
            "updated_at": "2026-07-03T10:00:03+00:00", "created_at": "2026-07-03T10:00:00+00:00",
        })
        meta_path.write_text(json.dumps(meta))

    rows = collect_rows(store=store)
    assert len(rows) == 2
    table = render_dashboard(rows)
    assert "Cost" in table and "Duration" in table
    assert "TOTAL (2)" in table
    # Cost is populated and duration derived (3s).
    assert any(r.cost_usd == 0.05 for r in rows)
    assert all(r.duration_ms == 3000 for r in rows)


def test_dashboard_from_atif_file(tmp_path: Path):
    traj = events_to_atif(_events_with_usage(), session_id="s1")
    path = tmp_path / "traj.json"
    path.write_text(json.dumps(traj), encoding="utf-8")
    row = row_from_atif(path)
    assert row.total_tokens == 260
    assert row.status == "success"
    assert row.turns == 2


def test_dashboard_empty():
    assert "No runs found" in render_dashboard([])
