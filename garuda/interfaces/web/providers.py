"""Provider and limit cards for the dashboard (plan task F.2, #169).

One card per harness and per API provider. Everything is read from records Garuda already
holds — discovery, the cached login conclusions, the limit snapshots (E.2) and the usage
ledger (E.1) — and nothing is invented:

* a limit value carries its **source and observation time**; a harness whose source the
  usage-source spike did not prove for its exact version reads ``unknown``;
* an observation older than 60 seconds is shown as **stale** with its age, a version other
  than the one the source was proved for is flagged, a source that supplied no account id
  says so, and more than one account seen for a harness is called out;
* a limit **event** reads ``active`` while its reset is ahead, ``expired`` once past, and
  ``historical`` when no reset time was ever reported;
* observed use is labelled "through Garuda only": use outside Garuda draws on the same quota;
* **refresh** re-runs the documented status reads — the login check and, for a proved
  source, the read-only limit read. It cannot send a prompt.
"""

from __future__ import annotations

import time
from typing import Any

from garuda.observability.ledger import Ledger
from garuda.observability.limits import (
    FALLBACK_TTL,
    PROVED_SOURCES,
    LimitStore,
    event_state,
    exact_version,
)

WINDOWS = {"5h": 5 * 3600, "7d": 7 * 86400, "30d": 30 * 86400}
THROUGH_GARUDA = "through Garuda only"


def observed_use(records: list[dict], now: float) -> dict[str, Any]:
    out = {}
    for name, span in WINDOWS.items():
        recent = [r for r in records if now - span <= r.get("time", 0) <= now]
        native = [r for r in recent if r.get("kind") == "native_model_call"]
        deltas = [r for r in recent if r.get("kind") == "acp_usage_delta"]
        counted = native + deltas
        known = [r["cost_usd"] for r in counted if r.get("cost_usd") is not None]
        out[name] = {
            "sessions": len({r.get("session_id") for r in counted if r.get("session_id")}),
            "native_calls": sum(r.get("calls", 1) for r in native),
            "acp_turns": len(deltas),
            "total_tokens": sum((r.get("total_tokens") or ((r.get("input_tokens") or 0)
                                                           + (r.get("output_tokens") or 0)))
                                for r in counted),
            "cost_usd": round(sum(known), 8) if known else None,
            "cost_unknown_records": len(counted) - len(known),
        }
    return {"label": THROUGH_GARUDA, "windows": out}


def _limits_block(entry: dict, limits: LimitStore, ledger_events: list[dict], now: float) -> dict:
    harness = entry["runtime_id"]
    current_version = exact_version(entry.get("version"))
    proved = PROVED_SOURCES.get(harness)
    events = [{"state": event_state(e, now), "reset_at": e.get("reset_at"),
               "observed_at": e.get("time"), "limit_id": e.get("limit_id")} for e in ledger_events]
    block: dict[str, Any] = {"events": events, "windows": [], "status": "unknown",
                             "source": None, "observed_at": None, "age_seconds": None,
                             "notes": []}
    if proved is None:
        block["reason"] = "no documented non-interactive source for this harness"
        return block
    if current_version and current_version not in proved:
        block["notes"].append("version differs from the captured matrix "
                              f"({current_version}; proved for {', '.join(proved)})")
    observation = limits.latest(harness)
    if observation is None:
        block["reason"] = "no observation recorded yet; refresh to read it"
        return block
    age = max(0.0, now - observation.observed_at)
    block.update(
        source=observation.source, observed_at=observation.observed_at,
        age_seconds=round(age, 1), version=observation.harness_version,
        reached=observation.reached, reset_at=observation.reset_at,
        status="known" if age <= FALLBACK_TTL else "stale",
        windows=[{"name": w.name, "used_fraction": w.used_fraction, "resets_at": w.resets_at,
                  "window_minutes": w.window_minutes} for w in observation.windows])
    if observation.harness_version and current_version and \
            observation.harness_version != current_version:
        block["notes"].append(f"observed on {observation.harness_version}, now {current_version}")
    if not observation.account_digest:
        block["notes"].append("this source supplied no account id: it cannot bind a limit")
    accounts = sorted(p.name for p in limits.root.glob(f"profile-{harness}-*.json"))
    block["accounts_seen"] = len(accounts)
    if len(accounts) > 1:
        block["notes"].append("more than one account has been observed on this harness")
    return block


def _login_block(manifest, now: float) -> dict:
    from garuda.acp.login_probe import cached_login

    cached = cached_login(manifest)
    if cached is None:
        return {"state": "not_checked", "checked_at": None}
    state, at = cached
    return {"state": state.value, "checked_at": at or None,
            "age_seconds": round(max(0.0, now - at), 1) if at else None}


def cards(entries: list[dict], manifests: dict, *, ledger: Ledger | None = None,
          limits: LimitStore | None = None, now: float | None = None) -> dict[str, Any]:
    """The harness cards (from ``entries``, the dashboard's health records) and API provider cards."""
    now = time.time() if now is None else now
    ledger = ledger or Ledger()
    limits = limits or LimitStore(ledger=ledger)
    records = list(ledger.records(since=now - max(WINDOWS.values()), until=now + 1))
    harness_cards = []
    for entry in entries:
        rid = entry["runtime_id"]
        mine = [r for r in records if r.get("harness") == rid]
        events = [r for r in records if r.get("kind") == "limit_event" and r.get("harness") == rid]
        manifest = manifests.get(rid)
        harness_cards.append({
            "kind": "harness", "id": rid, "runtime_kind": entry.get("kind"),
            "available": entry.get("available"), "version": entry.get("version"),
            "login": _login_block(manifest, now) if manifest is not None
            else {"state": "not_checked", "checked_at": None},
            "limits": _limits_block(entry, limits, events, now),
            "observed_use": observed_use(mine, now),
        })
    providers: dict[str, list[dict]] = {}
    for record in records:
        if record.get("kind") == "native_model_call" and record.get("model"):
            providers.setdefault(str(record["model"]).split("/", 1)[0], []).append(record)
    provider_cards = [{
        "kind": "api_provider", "id": name, "models": sorted({r["model"] for r in rs}),
        "rate_limits": {"status": "unknown",
                        "reason": "provider rate-limit headers are declared by LiteLLM but not "
                                  "recorded; shown only once observed"},
        "observed_use": observed_use(rs, now),
    } for name, rs in sorted(providers.items())]
    return {"generated_at": now, "harnesses": harness_cards, "providers": provider_cards}


def refresh(workspace: str, *, login_run=None, limit_refresh=None) -> dict[str, Any]:
    """Re-run the documented status reads (no prompt). Returns what was refreshed."""
    from garuda.acp.limit_probe import refresh_codex
    from garuda.acp.login_probe import probe_login
    from garuda.interfaces.web.runtimes import registry_from_context

    registry = registry_from_context({}, workspace)
    store = LimitStore()
    done = {"login": [], "limits": []}
    for manifest in registry.manifests:
        probe_login(manifest, cache_ttl=0, run=login_run)
        done["login"].append(manifest.runtime_id)
        if manifest.runtime_id in PROVED_SOURCES and manifest.auth_probe is not None \
                and manifest.auth_probe.argv:
            reader = limit_refresh or (lambda exe: refresh_codex(exe, store))
            if reader(manifest.auth_probe.argv[0]) is not None:
                done["limits"].append(manifest.runtime_id)
    return done
