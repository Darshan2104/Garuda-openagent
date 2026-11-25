"""How much of an external harness's run its persisted trail keeps (#691).

``observability.trace_detail`` in the user's ``settings.yaml``:

``metadata``  (default) ids, kinds, statuses and lifecycle only, as always.
``commands``  also each tool call's title, command and named paths, the parent
              call inside a harness subagent, and the message of a failed tool
              call or error event: redacted and bounded per field
              (:func:`garuda.acp.normalize.evidence_payload`).

Only the user's own settings decide it, never a project file. An unreadable or
unknown value keeps ``metadata``: persisting less is the safe side.
"""

from __future__ import annotations

import logging

from garuda.acp.normalize import TRACE_LEVELS

logger = logging.getLogger(__name__)
DEFAULT = "metadata"


def configured_level(settings: dict | None = None) -> str:
    try:
        if settings is None:
            from garuda.config.agent_home import _load_global_settings

            settings = _load_global_settings()
        section = settings.get("observability") if isinstance(settings, dict) else None
        value = section.get("trace_detail", DEFAULT) if isinstance(section, dict) else DEFAULT
    except Exception:
        logger.warning("observability.trace_detail unreadable; keeping %s", DEFAULT, exc_info=True)
        return DEFAULT
    if value not in TRACE_LEVELS:
        logger.warning("observability.trace_detail %r is not one of %s; keeping %s",
                       value, ", ".join(TRACE_LEVELS), DEFAULT)
        return DEFAULT
    return value
