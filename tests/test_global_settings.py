"""Global settings defaults remain compatible across runtime configuration loaders."""

import os
from pathlib import Path

import pytest

from garuda.acp.catalog import load_trusted_runtime_settings
from garuda.agents.authority import project_ceiling
from garuda.agents.setup import prepare_runtime_catalog, select_initial_runtime
from garuda.config.routing import load_global_orchestration
from garuda.lsp.config import load_language_servers
from garuda.plugins.hooks import HookRegistry
from garuda.plugins.reminders import load_reminders
from garuda.runtime.capacity import configured_ceiling


@pytest.mark.parametrize("section", [
    "routing", "models", "model_bindings", "collection", "capacity", "runtimes",
    "language_servers", "reminders", "hooks", "agents", "disabled_runtimes", "all",
])
def test_null_global_sections_use_runtime_defaults(tmp_path, section):
    """Commenting out every entry in a section must not prevent a native run."""
    path = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    sections = (
        ("routing", "models", "model_bindings", "collection", "capacity", "runtimes",
         "language_servers", "reminders", "hooks", "agents", "disabled_runtimes")
        if section == "all" else (section,)
    )
    path.write_text("".join(f"{key}:\n  # No entries configured\n" for key in sections))

    settings = load_trusted_runtime_settings()
    catalog = prepare_runtime_catalog(tmp_path)
    assert catalog.registry.disabled_ids == frozenset()
    decision = select_initial_runtime(workspace=str(tmp_path), task="inspect", catalog=catalog)
    assert decision.selection.selected == "native"
    assert configured_ceiling("native", settings) is None
    assert project_ceiling(settings) == "smart"

    orchestration = load_global_orchestration()
    assert orchestration.models == {}
    assert orchestration.model_bindings == {}
    assert orchestration.default_binding == "default"
    assert orchestration.collection.enabled is False
    assert orchestration.collection.budget.max_jobs_per_run == 8

    language_servers = load_language_servers(tmp_path)
    assert language_servers.servers == ()
    assert language_servers.problems == ()
    reminders = load_reminders(strict=True)
    assert reminders.rules == []
    assert reminders.max_injections == 8
    hooks = HookRegistry.from_config(path)
    assert hooks.before_tool == hooks.after_tool == hooks.session_start == hooks.session_end == []
