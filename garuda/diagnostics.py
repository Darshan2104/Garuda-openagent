"""Stable diagnostic codes and their fixes (plan task C.4, #158).

Every refusal or warning a person may act on has one code from
:data:`REGISTRY`, a message that says what happened, and a fix that says what
to do. Codes are the contract — tests and tools match codes, never message
text — and every code has a fix template.
"""

from __future__ import annotations

from dataclasses import dataclass

#: code -> fix template. Templates use ``str.format`` fields from the call.
REGISTRY: dict[str, str] = {
    # configuration (C.1–C.3)
    "config.invalid": "Fix {path} in {file}; `garuda config show` prints what Garuda read.",
    "config.conflict": "Remove the conflicting value at {path}; keep each setting in one place.",
    "config.unsupported_version": "Set `version: 1` in {file}.",
    "config.project_widening": "Narrow {path} in the project file, or set it in your user file.",
    "config.project_untrusted": "Review the project file with `garuda config trust`.",
    "config.trust_requires_terminal": "Run `garuda config trust` in an interactive terminal.",
    "config.missing": "Create one with `garuda init`.",
    "config.ok": "Nothing to do.",
    "role.options_unproven": "Remove model_id/effort from the role, or use a proven adapter.",
    "role.model_unavailable": "Pick a model id the agent offers; `garuda doctor` lists them.",
    "role.model_not_allowed": "Add the model to harnesses.{harness}.allowed_models, or change it.",
    # agent definitions (H.1–H.2)
    "agent.ok": "Nothing to do.",
    "agent.legacy_warning": "Fix the key, or run `garuda agent migrate PATH`.",
    "agent.unknown_field": "Remove or rename {path}; `garuda agent show` lists the fields.",
    "agent.unknown_tool": "Use a registered tool name in {path}.",
    "agent.unknown_skill": "Add the skill, or remove it from {path}.",
    "agent.unknown_mcp_server": "Configure the server, or remove it from {path}.",
    "agent.unknown_tool_option": "Use an option the tool declares in {path}; "
                                 "`garuda agent show` lists them.",
    "agent.unknown_subagent": "Define the agent, or remove it from {path}.",
    "agent.mcp_tools_unknown": "Start a run to see the tools {servers} provide.",
    "agent.instruction_file_missing": "Create the file named in {path}, or remove it.",
    "agent.extends_cycle": "Break the cycle at {path}; extend garuda/<name> for a packaged agent.",
    "agent.extends_too_deep": "Flatten the chain at {path} to at most four levels.",
    "agent.unsupported_field": "Remove {path} until the release that supports it.",
    "agent.unsupported_version": "Set `version: 1`.",
    "agent.invalid_value": "Fix the value at {path}.",
    "agent.invalid_name": "Use a plain name, or garuda/, user/ or project/<name>.",
    "agent.invalid_budget": "Adjust {path} so the budgets fit.",
    "agent.invalid_yaml": "Simplify the YAML (fewer aliases, less nesting).",
    "agent.required_gate": "Set {path} in your user agents, or pass a flag for one run.",
    "agent.path_escapes": "Keep {path} inside the agent's own directory or repository.",
    "agent.ambiguous_instructions": "Use either the Markdown body or instructions.text.",
    "agent.too_large": "Keep the definition under 256 KiB.",
    "agent.project_widening": "Lower the permissions in the project file, or pass "
                              "--permission-mode for one run.",
    "skill.tool_not_granted": "Grant {tools} to the agent, or expect skill {skill} to work "
                              "without them (allowed-tools is advisory).",
    # harnesses
    "harness.ok": "Nothing to do.",
    "harness.cli_missing": "Install it: {setup}",
    "harness.disabled": "Remove {runtime} from disabled_runtimes in settings.yaml to use it.",
    "harness.logged_out": "Log in with the harness's own CLI: {login}",
    "harness.login_unknown": "Garuda cannot check this harness's login; run it once to see.",
    "harness.login_probe_failed": "Run `{argv}` yourself to see why it failed.",
    "harness.login_timeout": "Run `{argv}` yourself; it did not answer within {timeout}s.",
    "harness.login_unrecognized": "Run `{argv}` yourself; its answer was not one Garuda knows.",
    "harness.not_checked": "Check it with `garuda doctor --runtime {runtime}`.",
    # sessions and workspaces
    "lease.unreadable": "Inspect the leases directory; a corrupt lease blocks its workspace.",
    "lease.held": "Wait for session {session} to finish, or stop it.",
    "lease.stale": "Session {session} may have crashed; `garuda runtime recover --session {session}`.",
    "worktree.unpublished": "Merge it (`garuda sessions merge {session}`) or remove it with --force.",
    "session.project_key_missing": "Run `garuda doctor --recover-project-ids`.",
    "verification.no_trusted_check": "Add a check to garuda.yaml, or pass --check COMMAND.",
}


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    fix: str
    level: str = "info"  # info | warning | error

    def to_dict(self) -> dict:
        return {"code": self.code, "level": self.level, "message": self.message, "fix": self.fix}

    def render(self) -> str:
        mark = {"error": "✗", "warning": "!", "info": "·"}[self.level]
        return f"{mark} {self.code}: {self.message}\n    fix: {self.fix}"


def diagnostic(code: str, message: str, *, level: str = "info", **fields) -> Diagnostic:
    """A registered diagnostic; an unregistered code is a programming error."""
    template = REGISTRY[code]
    try:
        fix = template.format(**fields)
    except KeyError as exc:
        raise KeyError(f"diagnostic {code} needs field {exc}") from exc
    return Diagnostic(code=code, message=message, fix=fix, level=level)
