"""``remember``: propose a note for the user to review (plan task H.9, #171)."""

from garuda.context.notes import MAX_TEXT, NotesRefused
from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


async def _may_read(ctx: ToolContext, path: str, *, ask: bool) -> bool:
    """Screen a source exactly as a ``read_file`` call on it; fail closed.

    Without the run's permission engine, a model-named path is never observed.
    Proposing may ask for approval like ``read_file``; search never prompts, so
    an ``ask`` rule leaves that source unknown."""
    permissions = getattr(ctx, "permissions", None)
    if permissions is None:
        return False
    arguments = {"path": path}
    if ask:
        allowed, _reason = await permissions.evaluate_tool_call("read_file", arguments)
        return allowed
    from garuda.core.permissions import PermissionDecision

    return permissions.decide("read_file", arguments)[0] == PermissionDecision.ALLOW


class RememberTool:
    """Records a *proposal*. Nothing is remembered until the user accepts it with
    ``garuda memory review``; this tool changes no memory file."""

    effect = ToolEffect.EXTERNAL_SIDE_EFFECT
    name = "remember"
    description = (
        "Propose a short note worth remembering in future runs (a convention, a preference, a "
        f"non-obvious fact). At most {MAX_TEXT} characters, no secrets. This only records a "
        "proposal: the user decides later whether it is kept. Use action=search with query "
        "to retrieve accepted notes as labelled data (at most 4000 characters). Stale or "
        "conflicting notes defer to current source and user instructions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["propose", "search"], "default": "propose"},
            "query": {"type": "string", "maxLength": 200},
            "max_chars": {"type": "integer", "minimum": 256, "maximum": 4000,
                          "default": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            "text": {"type": "string", "description": f"The note, one or two sentences "
                                                      f"(at most {MAX_TEXT} characters)"},
            "scope": {"type": "string", "enum": ["user", "project"],
                      "description": "user: applies to everything this user does; "
                                     "project: only to this repository"},
            "evidence": {"type": "array", "maxItems": 4,
                         "items": {"type": "string", "maxLength": 160},
                         "description": "Evidence/session references, never raw transcripts or secrets"},
            "sources": {"type": "array", "maxItems": 8,
                        "items": {"type": "string", "maxLength": 240},
                        "description": "Workspace-relative source/config paths to fingerprint"},
            "supersedes": {"type": "array", "maxItems": 4,
                           "items": {"type": "string"},
                           "description": "Accepted note ids replaced only if this proposal is accepted"},
            "expires_at": {"type": "string", "description": "ISO timestamp with timezone"},
        },
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        ledger = getattr(ctx, "notes", None)
        if ledger is None:
            return ToolResult(tool_call_id="", is_error=True,
                              content="agent.notes_unavailable: proposing notes is not enabled")
        try:
            action = arguments.get("action", "propose")
            if action == "search":
                content = await ledger.proposals.search(
                    arguments.get("query", ""), env,
                    max_chars=arguments.get("max_chars", 2000), limit=arguments.get("limit", 5),
                    readable=lambda path: _may_read(ctx, path, ask=False))
                return ToolResult(tool_call_id="", content=content)
            if action != "propose":
                raise NotesRefused("memory.invalid_action", "action must be propose or search")
            from garuda.context.note_provenance import capture

            ledger.proposals.check_environment(env)
            provenance = await capture(
                env, sources=arguments.get("sources"), evidence=arguments.get("evidence"),
                supersedes=arguments.get("supersedes"), expires_at=arguments.get("expires_at"),
                readable=lambda path: _may_read(ctx, path, ask=True))
            proposal = ledger.propose(str(arguments.get("text", "")),
                                      str(arguments.get("scope", "")), ctx.session_id,
                                      provenance=provenance)
        except NotesRefused as exc:
            return ToolResult(tool_call_id="", is_error=True, content=str(exc))
        if proposal.state != "pending":
            return ToolResult(tool_call_id="", content=(
                f"Proposal {proposal.id[:8]} already exists (state: {proposal.state}). "
                "No new proposal was recorded."))
        return ToolResult(tool_call_id="", content=(
            f"Proposal {proposal.id[:8]} recorded for the user's review. It is not memory yet."))
