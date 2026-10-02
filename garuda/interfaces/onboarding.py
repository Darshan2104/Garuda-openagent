"""`garuda init`, `garuda doctor` and `garuda config show` (plan task C.4, #158).

- ``doctor`` reports configuration (with provenance), the harnesses your
  roles use — executable, version, login and capabilities — and the leases,
  capacity slots and worktrees Garuda holds. It probes only harnesses a role
  or ``--runtime`` names: nothing is probed because another one is selected.
- ``init`` proposes ``scout``, ``planner``, ``coder`` and ``reviewer`` roles on
  harnesses whose executable is present (no probe runs), with models left
  unknown unless you name them, and the worst-case invocations of each
  packaged flow. It writes only after you confirm.
- ``init --project`` proposes checks from marker files (names only; nothing
  is opened or run) and, on confirmation in a terminal, writes the project
  file and its trust record in one step.
- ``config show`` prints the effective configuration and where each value
  came from.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from garuda.diagnostics import Diagnostic, diagnostic

#: marker file -> check proposed for it.
MARKER_CHECKS = (
    (("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini"), "pytest -q"),
    (("package.json",), "npm test"),
    (("cargo.toml",), "cargo test"),
    (("go.mod",), "go test ./..."),
    (("gemfile",), "bundle exec rake test"),
)

#: Worst-case invocations of each packaged flow (C.6b), per role.
FLOW_INVOCATIONS = {
    "plan-only": {"scout": 1, "planner": 1},
    "pair": {"coder": 3, "reviewer": 3},
    "plan-build-review": {"planner": 1, "coder": 3, "reviewer": 3},
}


# --- doctor ------------------------------------------------------------------------


def _config_diagnostics(workspace) -> tuple[list[Diagnostic], object]:
    from garuda.config import garuda_yaml as gy

    out = []
    try:
        resolved = gy.load_effective(workspace)
    except gy.GarudaConfigError as exc:
        level = "error"
        out.append(diagnostic(exc.code, str(exc), level=level, path=exc.path or "(file)",
                              file="garuda.yaml"))
        return out, None
    if resolved is None:
        out.append(diagnostic("config.missing", f"no garuda.yaml ({gy.user_path()})"))
        return out, None
    roles = ", ".join(sorted(resolved.config.get("roles", {}))) or "none"
    out.append(diagnostic("config.ok", f"garuda.yaml read; roles: {roles}"))
    if resolved.withheld:
        out.append(diagnostic("config.project_untrusted",
                              f"withheld until trusted: {', '.join(resolved.withheld)}",
                              level="warning"))
    return out, resolved


def _harness_diagnostics(catalog, targets: set[str], *, login_run=None,
                         probe_timeout: float = 10.0) -> list[Diagnostic]:
    from garuda.acp.login_probe import LoginState, probe_login

    out = []
    for manifest in catalog.registry.manifests:
        rid = manifest.runtime_id
        if manifest.kind.value == "native":
            continue
        if rid not in targets:
            out.append(diagnostic("harness.not_checked", f"{rid}: not used by your roles",
                                  runtime=rid))
            continue
        if rid in catalog.registry.disabled_ids:
            out.append(diagnostic("harness.disabled", f"{rid}: disabled", level="warning",
                                  runtime=rid))
            continue
        record = next(r for r in catalog.discover(only=frozenset({rid}), cache_ttl=60.0)
                      if r.runtime_id == rid)  # discovery also lists built-in stubs
        if not record.available:
            out.append(diagnostic("harness.cli_missing", f"{rid}: executable not found",
                                  level="error", setup=manifest.setup or "see its docs"))
            continue
        caps = ", ".join(record.capabilities) or "none declared"
        state = probe_login(manifest, timeout=probe_timeout, run=login_run)
        argv = " ".join(manifest.auth_probe.argv) if manifest.auth_probe else ""
        head = f"{rid} {record.version} at {record.executable} (capabilities: {caps})"
        if state is LoginState.AUTHENTICATED:
            out.append(diagnostic("harness.ok", f"{head}; logged in"))
        elif state is LoginState.LOGGED_OUT:
            out.append(diagnostic("harness.logged_out", f"{head}; logged out", level="error",
                                  login=manifest.login.instructions or "see its docs"))
        elif state is LoginState.TIMEOUT:
            out.append(diagnostic("harness.login_timeout", f"{head}; login check timed out",
                                  level="warning", argv=argv, timeout=int(probe_timeout)))
        elif state is LoginState.FAILED:
            out.append(diagnostic("harness.login_probe_failed", f"{head}; login check failed",
                                  level="warning", argv=argv))
        elif state is LoginState.UNRECOGNIZED:
            out.append(diagnostic("harness.login_unrecognized",
                                  f"{head}; login check answered unexpectedly",
                                  level="warning", argv=argv))
        else:
            out.append(diagnostic("harness.login_unknown",
                                  f"{head}; no documented login check"))
    return out


def _state_diagnostics() -> list[Diagnostic]:
    import time

    from garuda.workspace.lease import LeaseStore

    out = []
    try:
        holders = LeaseStore().possibly_live_holders()
    except Exception as exc:  # a corrupt lease file is itself worth reporting
        holders = []
        out.append(diagnostic("lease.unreadable", str(exc), level="warning"))
    now = time.time()
    for holder in holders:
        code = "lease.stale" if holder.is_stale(now) else "lease.held"
        out.append(diagnostic(code, f"{holder.workspace} held by {holder.session_id[:8]} "
                                    f"({holder.mode})", level="warning" if code == "lease.stale"
                              else "info", session=holder.session_id[:8]))
    from garuda.workspace.worktrees import worktrees_root

    root = worktrees_root()
    if root.is_dir():
        for repo_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for tree in sorted(p for p in repo_dir.iterdir() if p.is_dir()
                               and not p.name.startswith(".")):
                out.append(diagnostic("worktree.unpublished", f"worktree {tree}",
                                      session=tree.name[:8]))
    return out


def doctor_report(workspace, *, runtimes=(), login_run=None) -> list[Diagnostic]:
    """Every check `garuda doctor` runs, as diagnostics."""
    from garuda.agents.setup import prepare_runtime_catalog

    out, resolved = _config_diagnostics(workspace)
    targets = set(runtimes)
    if resolved is not None:
        targets |= {r["harness"] for r in resolved.config.get("roles", {}).values()}
        targets |= set(resolved.config.get("harnesses", {}))
    catalog = prepare_runtime_catalog(str(workspace))
    out += _harness_diagnostics(catalog, targets, login_run=login_run)
    out += _state_diagnostics()
    return out


# --- init ---------------------------------------------------------------------------


def available_harnesses(catalog) -> list[str]:
    """Harnesses whose executable is on PATH; a name lookup, never a probe."""
    found = []
    for manifest in catalog.registry.manifests:
        if manifest.kind.value == "native":
            continue
        if manifest.runtime_id in catalog.registry.disabled_ids or not manifest.command:
            continue
        binary = manifest.command[0]
        if (os.path.isabs(binary) and os.access(binary, os.X_OK)) or shutil.which(binary):
            found.append(manifest.runtime_id)
    return found


def init_proposal(catalog, *, models: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    """The proposed user garuda.yaml and the lines that explain it."""
    from garuda.config import garuda_yaml as gy

    harnesses = available_harnesses(catalog)
    builder = next((h for h in ("codex", "claude") if h in harnesses), None) or (
        harnesses[0] if harnesses else "native")
    thinker = next((h for h in ("claude", "codex") if h in harnesses), None) or builder
    models = models or {}
    roles = {
        "scout": {"harness": thinker, "permissions": "readonly", "write_policy": "no-edits"},
        "planner": {"harness": thinker, "permissions": "smart", "write_policy": "no-edits"},
        "coder": {"harness": builder},
        "reviewer": {"harness": thinker, "permissions": "smart", "write_policy": "no-edits"},
    }
    for role in roles.values():
        if models.get(role["harness"]):
            role["model_id"] = models[role["harness"]]
    doc = gy.parse({"version": 1, "defaults": {"role": "coder"}, "roles": roles})
    lines = [f"harnesses found: {', '.join(harnesses) or 'none (native only)'}"]
    for name, role in roles.items():
        lines.append(f"role {name}: {role['harness']}, model "
                     f"{role.get('model_id', 'unknown (the harness default)')}")
    for flow, counts in FLOW_INVOCATIONS.items():
        parts = ", ".join(f"{role} ×{n}" for role, n in counts.items())
        lines.append(f"flow {flow}: at most {sum(counts.values())} invocations ({parts}); "
                     "cost unknown")
    return doc, lines


def project_proposal(workspace) -> tuple[dict, list[str]]:
    """Checks proposed from marker files; nothing is opened or run."""
    from garuda.config import garuda_yaml as gy
    from garuda.runtime.selection.constraints import detect_repo_traits

    markers = tuple(name for names, _ in MARKER_CHECKS for name in names)
    traits = detect_repo_traits(workspace, markers=markers)
    found = set(traits.marker_files)
    checks, lines = [], []
    for names, check in MARKER_CHECKS:
        hit = next((n for n in names if n in found), None)
        if hit and check not in checks:
            checks.append(check)
            lines.append(f"check `{check}` (from {hit})")
    return gy.parse({"version": 1, **({"checks": checks} if checks else {})}), lines


# --- config show ----------------------------------------------------------------------


def config_show(workspace) -> list[str]:
    from garuda.config import garuda_yaml as gy

    resolved = gy.load_effective(workspace)
    if resolved is None:
        return ["No garuda.yaml (user or project). `garuda init` proposes one."]
    lines = [gy.dump(resolved.config).rstrip(), "", "# where each value came from"]
    lines += [f"# {key}: {source}" for key, source in sorted(resolved.provenance.items())]
    if resolved.withheld:
        lines.append(f"# withheld until trusted: {', '.join(resolved.withheld)}")
    return lines


def write_file(path: Path, text: str) -> Path | None:
    """Write ``text`` to ``path`` atomically; returns the backup made, if any."""
    import datetime as _dt

    backup = None
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return backup
