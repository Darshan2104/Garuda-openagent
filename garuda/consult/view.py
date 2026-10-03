"""Read-only view of a task's consults (plan task G.4, #170).

Combines the durable admission state with the receipts for one root task and normalises
each request to a row for the CLI, the read models and the dashboard. It writes nothing and
starts nothing; anything it cannot read is shown as ``unknown`` rather than dropped.

``status`` is one of ``answered``, ``refused`` (the stable code is in ``code``), ``timeout``,
``withheld`` (the snapshot changed or could not be compared, so no answer was returned),
``failed``, ``quarantined`` (the child may still run), ``interrupted``, ``in_progress`` or
``unknown``. The rows hold identities, counts, durations and digests, never question or answer
text.
"""

from __future__ import annotations

import json
from pathlib import Path

from garuda.consult.state import ConsultState

UNKNOWN = "unknown"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _receipts(store, root: str) -> dict[str, dict]:
    directory = Path(store.root) / ".consult" / root / "receipts"
    out = {}
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return out
    for path in paths:
        document = _read_json(path)
        if isinstance(document, dict) and document.get("request_id"):
            out[document["request_id"]] = document
    return out


def _requests(store, root: str) -> dict[str, dict]:
    try:
        document = ConsultState(store, root).snapshot()
    except Exception:
        return {}
    requests = document.get("requests") if isinstance(document, dict) else None
    return requests if isinstance(requests, dict) else {}


def _status(state: str | None, receipt: dict | None, result: dict | None) -> tuple[str, str | None]:
    code = (receipt or {}).get("diagnostic") or (result or {}).get("code")
    if state == "quarantined":
        return "quarantined", code or "consult.quarantined"
    if state == "interrupted":
        return "interrupted", code or "consult.interrupted"
    if state in ("admitted", "dispatched"):
        return "in_progress", None
    outcome = (receipt or {}).get("outcome") or (result or {}).get("outcome")
    if outcome == "answered":
        return "answered", None
    if code == "consult.timeout":
        return "timeout", code
    if outcome == "withheld" or code == "consult.unexpected_changes":
        return "withheld", code
    if outcome == "failed":
        return "failed", code
    if outcome == "refused" or code:
        return "refused", code
    return UNKNOWN, code


def _changes(receipt: dict | None) -> dict:
    observed = (receipt or {}).get("observed_changes")
    if not isinstance(observed, dict) or "unchanged" not in observed:
        return {"unchanged": None, "changed": None, "evidence": UNKNOWN}
    return {"unchanged": bool(observed["unchanged"]), "changed": observed.get("changed"),
            "evidence": "receipt"}


def entries(store, root_session: str) -> list[dict]:
    """The consults requested under ``root_session``, oldest first."""
    requests = _requests(store, root_session)
    receipts = _receipts(store, root_session)
    rows = []
    for request_id in sorted(set(requests) | set(receipts),
                             key=lambda r: (requests.get(r) or {}).get("admitted_at") or 0):
        request = requests.get(request_id) or {}
        receipt = receipts.get(request_id)
        status, code = _status(request.get("state"), receipt, request.get("result"))
        identity = (receipt or {}).get("identity") or {}
        rows.append({
            "request_id": request_id,
            "root_session": root_session,
            "asker_session": (receipt or {}).get("asker_session") or request.get("asker"),
            "asker_role": (receipt or {}).get("asker_role"),
            "child_session": (receipt or {}).get("child_session") or request.get("child_id"),
            "target": (receipt or {}).get("role") or request.get("target"),
            "identity": {"runtime": identity.get("runtime"), "kind": identity.get("kind"),
                         "model_id": identity.get("model_id"),
                         "effort": identity.get("effort")} if identity else None,
            "admission": (receipt or {}).get("admission") or (
                "accepted" if request else UNKNOWN),
            "status": status,
            "code": code,
            "elapsed_ms": (receipt or {}).get("elapsed_ms"),
            "denied_operations": (receipt or {}).get("denied_operations"),
            "changes": _changes(receipt),
            "answer_chars": (receipt or {}).get("answer_chars"),
            "evidence": "receipt" if receipt else "state only",
            "admitted_at": request.get("admitted_at"),
        })
    return rows


def summary(rows: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    return {"count": len(rows), "by_status": dict(sorted(by_status.items()))}


def children(rows: list[dict]) -> list[str]:
    """The consulted child sessions, each once."""
    seen: list[str] = []
    for row in rows:
        child = row.get("child_session")
        if child and child not in seen:
            seen.append(child)
    return seen


def identities(rows: list[dict]) -> set[tuple[str | None, str | None]]:
    """The ``(runtime, model)`` identities actually consulted (those that ran)."""
    return {(row["identity"]["runtime"], row["identity"]["model_id"])
            for row in rows if row.get("identity")}


def line(rows: list[dict]) -> str:
    """One CLI line, or an empty string when there were no consults."""
    if not rows:
        return ""
    info = summary(rows)
    parts = ", ".join(f"{n} {status.replace('_', ' ')}" for status, n in info["by_status"].items())
    return f"consults: {info['count']} ({parts})"
