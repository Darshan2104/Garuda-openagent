"""Load custom Garuda tools from project ``.agent/tools/*.py`` modules.

Opt-in only: importing these modules executes repo code, so callers must gate on
the ``load_project_tools`` setting/flag before invoking :func:`load_project_tools`.

A tool module may expose its tools three ways (all optional, combined):

- ``TOOLS`` — an iterable of Tool instances (or zero-arg Tool classes).
- ``def get_tools() -> list[Tool]`` — a callable returning instances.
- ``def register(registry) -> None`` — a hook that registers into the passed
  :class:`~garuda.tools.registry.ToolRegistry`.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from garuda.tools.protocol import Tool
from garuda.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _looks_like_tool(obj: object) -> bool:
    return isinstance(getattr(obj, "name", None), str) and callable(getattr(obj, "execute", None))


def _load_module(path: Path):
    # Unique module name per file so re-loads / same-stem files don't collide.
    mod_name = f"garuda_project_tool_{path.stem}_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # executes repo code — gated by opt-in upstream
    return module


def _tools_from_module(module) -> list[Tool]:
    collected: list[Tool] = []

    tools_attr = getattr(module, "TOOLS", None)
    if tools_attr:
        for item in tools_attr:
            obj = item() if isinstance(item, type) else item
            if _looks_like_tool(obj):
                collected.append(obj)

    getter = getattr(module, "get_tools", None)
    if callable(getter):
        for obj in getter() or []:
            if _looks_like_tool(obj):
                collected.append(obj)

    register = getattr(module, "register", None)
    if callable(register):
        collector = ToolRegistry()
        register(collector)
        collected.extend(collector.select(None))

    return collected


def load_project_tools(tools_dirs) -> list[Tool]:
    """Discover and instantiate custom tools from ``<dir>/*.py`` modules.

    Files whose names start with ``_`` (e.g. ``__init__.py``) are skipped. Import
    or collection errors are logged and skipped so one bad file can't break the
    run. First definition wins on duplicate tool names.
    """
    tools: list[Tool] = []
    seen: set[str] = set()
    for directory in tools_dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                module = _load_module(path)
                if module is None:
                    continue
                for tool in _tools_from_module(module):
                    if tool.name in seen:
                        continue
                    seen.add(tool.name)
                    tools.append(tool)
            except Exception:
                logger.warning("Failed to load project tools from %s", path, exc_info=True)
    return tools
