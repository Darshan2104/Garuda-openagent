"""Record what an installed ACP adapter actually does, without sending a prompt (#152).

Opt-in and never run by CI. For one adapter command it:

1. runs ``initialize`` and records the protocol version, agent capabilities and
   auth methods;
2. runs ``session/new`` in an empty temporary directory and records the session's
   modes, models and config options (model, effort or reasoning ids);
3. exercises ``session/set_config_option`` (or the older ``session/set_model``)
   by re-selecting an advertised value, which changes nothing and costs nothing;
4. exercises ``session/close`` when the agent advertises it;
5. if ``loadSession`` is advertised, opens a second process and exercises
   ``session/load`` on the first session id.

No ``session/prompt`` is sent, so no model turn is spent and usage stays
``unknown``. Results are written as a redacted JSON fixture: home paths, the
temporary workspace, e-mail addresses and anything token-shaped are replaced.

Each capability is recorded as ``supported`` (exercised and it worked),
``declared`` (advertised but not exercised here) or ``unknown`` (not
advertised, or the exercise failed).

Usage::

    python scripts/capture_acp_capabilities.py --name claude \\
        --out tests/fixtures/acp/claude -- npx -y -p @agentclientprotocol/claude-agent-acp claude-agent-acp
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from garuda.acp.client import AcpProcess  # noqa: E402
from garuda.acp.protocol import ACP_VERSION  # noqa: E402

SUPPORTED, DECLARED, UNKNOWN = "supported", "declared", "unknown"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TOKENISH = re.compile(r"\b(?:sk|pk|ghp|gho|xox[bap])[-_][A-Za-z0-9_-]{8,}\b|\b[A-Za-z0-9_-]{40,}\b")


def redact(value: Any, replacements: dict[str, str]) -> Any:
    """Replace machine-specific and secret-shaped strings anywhere in ``value``."""
    if isinstance(value, str):
        out = value
        for needle, label in sorted(replacements.items(), key=lambda kv: -len(kv[0])):
            if needle:
                out = out.replace(needle, label)
        out = _EMAIL.sub("<email>", out)
        return _TOKENISH.sub("<redacted>", out)
    if isinstance(value, list):
        return [redact(v, replacements) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, replacements) for k, v in value.items()}
    return value


def _config_options(session: dict) -> list[dict]:
    options = session.get("configOptions")
    return [o for o in options if isinstance(o, dict)] if isinstance(options, list) else []


def _option_values(option: dict) -> list[str]:
    values = []
    for choice in option.get("options") or []:
        if isinstance(choice, dict) and isinstance(choice.get("value"), str):
            values.append(choice["value"])
        elif isinstance(choice, dict):
            for nested in choice.get("options") or []:
                if isinstance(nested, dict) and isinstance(nested.get("value"), str):
                    values.append(nested["value"])
    return values


def summarize_session(session: dict) -> dict:
    """The selection-relevant facts of a ``session/new`` result."""
    summary: dict[str, Any] = {"config_options": []}
    for option in _config_options(session):
        summary["config_options"].append(
            {
                "id": option.get("id"),
                "category": option.get("category"),
                "type": option.get("type"),
                "current": option.get("currentValue"),
                "values": _option_values(option),
            }
        )
    models = session.get("models")
    if isinstance(models, dict):
        summary["models"] = {
            "current": models.get("currentModelId"),
            "available": [
                m.get("modelId") for m in models.get("availableModels") or [] if isinstance(m, dict)
            ],
        }
    modes = session.get("modes")
    if isinstance(modes, dict):
        summary["modes"] = {
            "current": modes.get("currentModeId"),
            "available": [m.get("id") for m in modes.get("availableModes") or [] if isinstance(m, dict)],
        }
    return summary


async def _try(coro) -> tuple[bool, Any]:
    try:
        return True, await coro
    except Exception as exc:  # recorded, never raised: a failure is evidence
        return False, f"{type(exc).__name__}: {exc}"


async def capture(argv: list[str], *, timeout: float) -> dict:
    workspace = tempfile.mkdtemp(prefix="garuda-acp-capture-")
    result: dict[str, Any] = {
        "schema": 1,
        "client_protocol_version": ACP_VERSION,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "adapter_command": argv,
        "prompt_sent": False,
        "capabilities": {},
        "exercised": {},
    }
    # Adapter identity is the command plus the name/version the agent reports in
    # `initialize` (`agentInfo`); a launcher such as npx says nothing about it.
    caps = result["capabilities"]
    process = AcpProcess(argv, cwd=workspace, call_timeout=timeout)
    try:
        await process.launch()
        ok, init = await _try(process.initialize(timeout=timeout))
        if not ok:
            result["initialize_error"] = init
            result["stderr_tail"] = process.stderr_tail[-2000:]
            return result
        result["initialize"] = init
        agent_caps = init.get("agentCapabilities") or {}
        session_caps = agent_caps.get("sessionCapabilities") or {}
        caps["load_session"] = DECLARED if agent_caps.get("loadSession") else UNKNOWN
        caps["resume_session"] = DECLARED if "resume" in session_caps else UNKNOWN
        caps["close_session"] = DECLARED if "close" in session_caps else UNKNOWN
        # ACP v1 requires every agent to accept stdio MCP servers; it is still
        # only "declared" until session/new accepts an mcpServers list below.
        caps["mcp_stdio"] = DECLARED
        caps["mcp_http"] = DECLARED if (agent_caps.get("mcpCapabilities") or {}).get("http") else UNKNOWN

        ok, session = await _try(
            process._call("session/new", {"cwd": workspace, "mcpServers": []}, timeout=timeout)
        )
        if not ok or not isinstance(session, dict) or not session.get("sessionId"):
            result["session_new_error"] = session
            result["stderr_tail"] = process.stderr_tail[-2000:]
            return result
        session_id = session["sessionId"]
        result["session_new"] = session
        result["session_summary"] = summary = summarize_session(session)
        caps["mcp_stdio"] = SUPPORTED  # session/new accepted an mcpServers list

        model_option = next(
            (o for o in summary["config_options"] if o.get("category") == "model"), None
        )
        effort_option = next(
            (
                o
                for o in summary["config_options"]
                if o.get("category") in ("thought_level", "effort", "reasoning")
                or "effort" in str(o.get("id", "")).lower()
                or "reason" in str(o.get("id", "")).lower()
            ),
            None,
        )
        for name, option in (("model_selection", model_option), ("effort_selection", effort_option)):
            if option is None or option.get("current") is None:
                caps[name] = UNKNOWN
                continue
            ok, reply = await _try(
                process._call(
                    "session/set_config_option",
                    {"sessionId": session_id, "configId": option["id"], "value": option["current"]},
                    timeout=timeout,
                )
            )
            result["exercised"][name] = {"method": "session/set_config_option", "ok": ok, "reply": reply}
            caps[name] = SUPPORTED if ok else DECLARED
        if model_option is None and summary.get("models", {}).get("current"):
            ok, reply = await _try(
                process._call(
                    "session/set_model",
                    {"sessionId": session_id, "modelId": summary["models"]["current"]},
                    timeout=timeout,
                )
            )
            result["exercised"]["model_selection"] = {"method": "session/set_model", "ok": ok, "reply": reply}
            caps["model_selection"] = SUPPORTED if ok else DECLARED

        if caps["load_session"] == DECLARED:
            second = AcpProcess(argv, cwd=workspace, call_timeout=timeout)
            try:
                await second.launch()
                await second.initialize(timeout=timeout)
                ok, reply = await _try(
                    second._call(
                        "session/load",
                        {"sessionId": session_id, "cwd": workspace, "mcpServers": []},
                        timeout=timeout,
                    )
                )
                result["exercised"]["load_session"] = {"ok": ok, "reply": reply}
                caps["load_session"] = SUPPORTED if ok else DECLARED
            finally:
                await second.close()

        if caps["close_session"] == DECLARED:
            ok, reply = await _try(
                process._call("session/close", {"sessionId": session_id}, timeout=timeout)
            )
            result["exercised"]["close_session"] = {"ok": ok, "reply": reply}
            caps["close_session"] = SUPPORTED if ok else DECLARED

        caps["usage_update"] = UNKNOWN  # needs a prompt; deliberately not sent
        result["notifications"] = process.drain_notifications()[:20]
        return result
    finally:
        await process.close()
        shutil.rmtree(workspace, ignore_errors=True)
        result["_workspace"] = workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True, help="Adapter name, e.g. claude or codex")
    parser.add_argument("--out", required=True, help="Directory for the redacted fixture")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the adapter argv")
    args = parser.parse_args(argv)
    command = [c for c in args.command if c != "--"]
    if not command:
        parser.error("give the adapter command after --")
    raw = asyncio.run(capture(command, timeout=args.timeout))
    workspace = raw.pop("_workspace", "")
    replacements = {
        workspace: "<workspace>",
        os.path.realpath(workspace) if workspace else "": "<workspace>",
        str(Path.home()): "~",
        os.environ.get("USER", "") and f"/{os.environ.get('USER')}/": "/<user>/",
    }
    redacted = redact(raw, {k: v for k, v in replacements.items() if k})
    version = (
        ((redacted.get("initialize") or {}).get("agentInfo") or {}).get("version") or "unknown"
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{args.name}-{version}.json"
    target.write_text(json.dumps(redacted, indent=2, sort_keys=True) + "\n")
    print(f"wrote {target}")
    print(json.dumps(redacted.get("capabilities", {}), indent=2, sort_keys=True))
    return 0 if "initialize" in redacted else 1


if __name__ == "__main__":
    raise SystemExit(main())
