"""Operator controls for quota recovery; never invoked by a project config."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from garuda.flows.quota_recovery import FlowRecovery, RecoveryStore, default_root, project
from garuda.runtime import strict_store as ss


def run_recovery(args) -> int:
    from garuda.runtime.recovery_schedule import disable, install, schedule_status

    root = Path(args.root).expanduser().absolute() if args.root else default_root()
    records = RecoveryStore(root)
    try:
        command = args.recovery_command
        if command == "enable":
            result = install(root)
        elif command == "disable":
            result = disable(root)
        elif command == "tick":
            result = asyncio.run(FlowRecovery(root=root).tick())
        elif command == "cancel":
            row = records.cancel(args.flow_session)
            from garuda.core.sessions import SessionStore

            SessionStore(root=row["sessions_root"]).update_meta(
                args.flow_session, {"quota_recovery": project(row)})
            result = project(row)
        else:
            result = {"schedule": schedule_status(root),
                      "flows": [project(row) for row in records.records()]}
        print(json.dumps(result, indent=2))
        if command == "tick" and any(
                row.get("state") in {"blocked", "uncertain"} for row in result.get("flows", [])):
            print("Quota recovery requires attention; see the reported flow reason.", file=sys.stderr)
            return 3
        return 0
    except subprocess.CalledProcessError as exc:
        from garuda.context.redact import redact_text, summarize_command_output

        detail = exc.stderr or ""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        # Bound diagnostics, scrub known secret shapes, and remove terminal controls.
        detail, _ = redact_text(detail[:4096])
        detail = "".join(c for c in detail if c.isprintable() or c in "\n\t")
        detail = summarize_command_output(detail.strip(), limit=1024)
        print(f"Error: recovery command failed (exit {exc.returncode})"
              + (f": {detail}" if detail else "."), file=sys.stderr)
        if args.recovery_command in {"enable", "disable"}:
            print("Check user crontab permissions and run `garuda recovery status` "
                  "to inspect persisted enablement and installation.", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ss.StorageError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
