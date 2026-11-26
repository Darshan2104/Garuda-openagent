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


def _global_settings_diagnostics() -> list[Diagnostic]:
    """Validate global settings with the same loaders/parsers as a run, without execution."""
    from garuda.acp.catalog import load_trusted_disabled, load_trusted_runtime_settings
    from garuda.agents.authority import project_ceiling
    from garuda.config.agent_home import global_settings_path
    from garuda.config.routing import parse_global_models, parse_model_bindings
    from garuda.lsp.config import parse_language_servers
    from garuda.model.config import parse_collection_policy
    from garuda.plugins.hooks import HookRegistry
    from garuda.plugins.reminders import load_reminders
    from garuda.runtime.capacity import CapacityError, configured_ceiling
    from garuda.runtime.registry import RegistryError, parse_global_manifests
    from garuda.runtime.selection.scoring import parse_global_selection

    path = global_settings_path()
    source = f"global settings ({path})"
    out = []

    def problem(key, message):
        out.append(diagnostic("settings.invalid", f"{source} {key}: {message}",
                              level="error", file=path, path=key))

    def check(key, validate):
        try:
            return validate()
        except (ValueError, TypeError, AttributeError, OSError, CapacityError, RegistryError) as exc:
            problem(key, str(exc))
            return None

    try:
        # The shared loader treats a missing file as defaults. Distinguish an
        # existing non-file (including a dangling symlink) from that valid case.
        try:
            path.lstat()
        except FileNotFoundError:
            return out
        if not path.is_file():
            raise ValueError("settings must be a regular file")
        settings = load_trusted_runtime_settings()
    except (ValueError, OSError) as exc:
        problem("(file)", str(exc))
        return out

    capacity = settings.get("capacity")
    if capacity is not None:
        for key in capacity if isinstance(capacity, dict) else ("native",):
            check(f"capacity.{key}" if isinstance(capacity, dict) else "capacity",
                  lambda key=key: configured_ceiling(key, settings))
    check("routing", lambda: parse_global_selection(settings, source=source))
    models = check("models", lambda: parse_global_models(settings.get("models"), source=source))
    check("model_bindings", lambda: parse_model_bindings(
        settings.get("model_bindings"), models or {}, source=source))
    check("collection", lambda: parse_collection_policy(settings.get("collection"), source=source))
    check("agents", lambda: project_ceiling(settings))
    check("runtimes", lambda: parse_global_manifests(settings.get("runtimes"), source=source))
    check("disabled_runtimes", lambda: load_trusted_disabled(settings))
    for message in parse_language_servers(settings.get("language_servers"))[1]:
        problem("language_servers", message)
    # These loaders only construct rules and hook callbacks; they never fire
    # commands. Omit the workspace so project hooks/reminders are not loaded.
    check("reminders", lambda: load_reminders(strict=True))
    check("hooks", lambda: HookRegistry.from_config(path))
    return out


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
                         probe_timeout: float = 10.0,
                         fallback_only: frozenset[str] = frozenset()) -> list[Diagnostic]:
    """Check each harness in ``targets``.

    A harness in ``fallback_only`` is used only in a role's ``fallback:``
    chain: it is checked the same way, but a missing or logged-out one is a
    warning (that fallback can never be taken), not an error, because the
    role itself can still start.
    """
    from garuda.acp.login_probe import LoginState, probe_login

    out = []
    for manifest in catalog.registry.manifests:
        rid = manifest.runtime_id
        if manifest.kind.value == "native":
            continue
        fallback = rid in fallback_only
        blocking = "warning" if fallback else "error"
        note = " (used only as a role fallback, which can never be taken)" if fallback else ""
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
            out.append(diagnostic("harness.cli_missing", f"{rid}: executable not found{note}",
                                  level=blocking, setup=manifest.setup or "see its docs"))
            continue
        caps = ", ".join(record.capabilities) or "none declared"
        state = probe_login(manifest, timeout=probe_timeout, run=login_run)
        argv = " ".join(manifest.auth_probe.argv) if manifest.auth_probe else ""
        head = f"{rid} {record.version} at {record.executable} (capabilities: {caps})"
        if state is LoginState.AUTHENTICATED:
            out.append(diagnostic("harness.ok", f"{head}; logged in"))
        elif state is LoginState.LOGGED_OUT:
            out.append(diagnostic("harness.logged_out", f"{head}; logged out{note}", level=blocking,
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
    from garuda.runtime.ownership import owner_liveness

    now = time.time()
    for holder in holders:
        session = holder.session_id[:8]
        message = f"{holder.workspace} held by {session} ({holder.mode})"
        # Only a dead owner's lease can be released, so the advice follows the
        # owner's state rather than always pointing at recovery (#676).
        state = owner_liveness({"pid": holder.pid, "identity": holder.identity,
                                "pgid": holder.pgid})
        if state is True and not holder.is_stale(now):
            out.append(diagnostic("lease.held", message, session=session))
            continue
        code = ("lease.quarantined" if holder.quarantined else "lease.overdue" if state is True
                else "lease.stale" if state is False else "lease.unknown")
        out.append(diagnostic(code, message, level="warning", session=session, pid=holder.pid))
    out += _capacity_diagnostics(owner_liveness)
    from garuda.workspace.worktrees import worktrees_root

    root = worktrees_root()
    if root.is_dir():
        for repo_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for tree in sorted(p for p in repo_dir.iterdir() if p.is_dir()
                               and not p.name.startswith(".")):
                out.append(diagnostic("worktree.unpublished", f"worktree {tree}",
                                      session=tree.name[:8]))
    return out


def _capacity_diagnostics(liveness) -> list[Diagnostic]:
    """Reserved runtime capacity slots, so a leaked slot is visible (#703)."""
    from garuda.runtime.capacity import CapacityStore

    out = []
    try:
        slots = CapacityStore().all_slots()
    except Exception as exc:
        return [diagnostic("capacity.unreadable", str(exc), level="warning")]
    for slot in slots:
        holder = slot["holder"]
        session = holder.split(":", 1)[0][:8]
        owner = slot.get("owner") or {}
        message = f"{slot['key']} capacity slot reserved by {holder}"
        state = liveness(owner)
        if slot.get("quarantined"):
            out.append(diagnostic("capacity.retained", message, level="warning",
                                  session=session, reason="quarantined by its owner"))
        elif state is True:
            out.append(diagnostic("capacity.held", message, pid=owner.get("pid")))
        elif "queue" in slot or "loan" in slot:
            out.append(diagnostic("capacity.retained", message, level="warning", session=session,
                                  reason="queue-bound" if "queue" in slot else "loaned"))
        elif state is False:
            out.append(diagnostic("capacity.stale", message, level="warning", session=session))
        else:
            out.append(diagnostic("capacity.retained", message, level="warning", session=session,
                                  reason="its owner's liveness cannot be determined"))
    return out


def _requested_runtimes(catalog, runtimes, targets: set[str]) -> tuple[list[Diagnostic], set[str]]:
    """Resolve each `--runtime` ref as `garuda run --runtime` does (#777).

    An alias checks the harness it names. An unknown ref is an error carrying
    the same refusal `run` prints, never a silent no-op.
    """
    from garuda.runtime.registry import RegistryError

    out: list[Diagnostic] = []
    resolved = set(targets) - set(runtimes)
    known = ", ".join(m.runtime_id for m in catalog.registry.manifests) or "none"
    for ref in dict.fromkeys(runtimes):
        try:
            resolved.add(catalog.registry.resolve_id(ref))
        except RegistryError as exc:
            out.append(diagnostic("harness.unknown", f"runtime selection refused: {exc}",
                                  level="error", known=known))
    return out, resolved


def doctor_report(workspace, *, runtimes=(), login_run=None) -> list[Diagnostic]:
    """Every check `garuda doctor` runs, as diagnostics."""
    from garuda.agents.setup import prepare_runtime_catalog

    out = _global_settings_diagnostics()
    if out:
        # Invalid trust settings must not turn into a default/empty catalog and
        # authorize probes. State diagnostics remain safe and useful.
        return out + _state_diagnostics()
    config, resolved = _config_diagnostics(workspace)
    out += config
    targets = set(runtimes)
    fallbacks: set[str] = set()
    if resolved is not None:
        roles = resolved.config.get("roles", {}).values()
        targets |= {r["harness"] for r in roles}
        targets |= set(resolved.config.get("harnesses", {}))
        # A role's fallback harnesses are used too (#680).
        fallbacks = {c["harness"] for r in roles for c in r.get("fallback", [])} - targets
        targets |= fallbacks
    catalog = prepare_runtime_catalog(str(workspace))
    unknown, targets = _requested_runtimes(catalog, runtimes, targets)
    out += unknown
    out += _harness_diagnostics(catalog, targets, login_run=login_run,
                                fallback_only=frozenset(fallbacks))
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
