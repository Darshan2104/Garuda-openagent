from pathlib import Path

# Compatibility seam for the fail-closed startup gate tests.  The native
# binding path resolves profiles in shared setup rather than this entry point.
from garuda.agents.loader import load_profile  # noqa: F401
from garuda.core.events import EventStore
from garuda.core.modes import MODE_CHOICES
from garuda.interfaces.cli import chat_loop
from garuda.interfaces.runner import (
    cleanup_workspace,
    resolve_environment,
    run_agent_task,
)
from garuda.interfaces.web.live import DEFAULT_MAX_PERMISSION as WEB_DEFAULT_MAX_PERMISSION
from garuda.interfaces.web.security import DEFAULT_PORT as WEB_DEFAULT_PORT
from garuda.model.litellm_model import LitellmModel  # noqa: F401
from garuda.tools import build_toolkit  # noqa: F401
from garuda.workspace.factory import WORKSPACE_KINDS


def _add_model_flags(parser) -> None:
    """Role-model flags shared by every task-starting subcommand.

    Defaults are None (never the built-in): an omitted flag must stay
    distinguishable from an explicit one, or the parser default would mask
    profile, project, and global bindings below it. `--model` is the legacy
    explicit reasoning alias; `--reasoning-model` is its new spelling.
    """
    parser.add_argument(
        "--model",
        default=None,
        help="Reasoning model (alias for --reasoning-model; explicit beats env/bindings)",
    )
    parser.add_argument(
        "--reasoning-model",
        default=None,
        help="Reasoning model: owns the controller loop and all mutations",
    )
    parser.add_argument(
        "--collection-model",
        default=None,
        help="Optional collection model for bounded read-only investigation jobs",
    )
    parser.add_argument(
        "--no-collection",
        action="store_true",
        help="Disable the collection role even when a collection model resolves",
    )


def _add_tag_flags(parser) -> None:
    """`--with`, `--with-id` and the cross-project grant (B.7 session tags)."""
    parser.add_argument(
        "--with",
        dest="with_sessions",
        action="append",
        default=[],
        metavar="NAME",
        help="Attach the brief of a session in this project (repeatable)",
    )
    parser.add_argument(
        "--with-id",
        dest="with_ids",
        action="append",
        default=[],
        metavar="FULL_ID",
        help="Attach the brief of a session by full id; another project's needs a grant",
    )
    parser.add_argument(
        "--allow-cross-project-context",
        action="store_true",
        help="Grant --with-id sessions from other projects (headless; otherwise you are asked)",
    )


def _session_tags(args, task: str, *, exclude: str | None = None):
    """Resolve a run's session tags (B.7), echo them, or refuse before any prompt."""
    import os
    import sys

    from garuda.context.tags import attach_for_cli
    from garuda.interfaces.cli import confirm_cross_project

    return attach_for_cli(
        os.path.realpath(getattr(args, "workspace", ".")),
        task,
        with_refs=getattr(args, "with_sessions", None) or [],
        with_ids=getattr(args, "with_ids", None) or [],
        allow_cross_project=getattr(args, "allow_cross_project_context", False),
        confirm=confirm_cross_project,
        out=sys.stderr if getattr(args, "json", False) else sys.stdout,
        exclude=exclude,
    )


def build_parser():
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="garuda", description="Garuda Open Agent harness")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a single agent task (headless)")
    run_parser.add_argument("-t", "--task", help="Task description")
    run_parser.add_argument("-f", "--file", help="Read task from file")
    _add_model_flags(run_parser)
    run_parser.add_argument("--workspace", default=".", help="Workspace root directory")
    run_parser.add_argument(
        "--workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
        help="Execution environment type",
    )
    run_parser.add_argument("--docker-image", default="ubuntu:22.04")
    run_parser.add_argument("--docker-host", help="Remote Docker daemon host (DOCKER_HOST)")
    run_parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow network egress inside the OS sandbox (sandbox kind denies it by default)",
    )
    run_parser.add_argument(
        "--no-network",
        action="store_true",
        help="Disable network egress for docker/remote containers (default: bridged)",
    )
    run_parser.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="Run --workspace-kind sandbox unconfined if no OS sandbox backend is available "
        "(default: fail loudly)",
    )
    run_parser.add_argument("--docker-memory", default="2g", help="Container memory limit (e.g. 2g)")
    run_parser.add_argument("--docker-cpus", default="2", help="Container CPU limit (e.g. 2)")
    run_parser.add_argument("--agent", default="build", help="Agent profile name")
    run_parser.add_argument(
        "--runtime",
        default=None,
        help="Trusted global runtime id or project alias. Naming one (including "
        "`native`) pins it; omitted, routing rules may choose and native is the default",
    )
    run_parser.add_argument("--agents-dir", help="Directory with custom agent YAML profiles")
    run_parser.add_argument(
        "--mcp-config",
        help="Path to MCP servers config (YAML or JSON); auto-discovered from "
        ".agent/mcp.json|yaml, .garuda/mcp.json|yaml or .cursor/mcp.json when omitted",
    )
    run_parser.add_argument(
        "--load-project-tools",
        action="store_true",
        default=None,
        help="Import custom tools from .agent/tools/*.py (runs repo code; "
        "overrides the load_project_tools setting)",
    )
    run_parser.add_argument("--permission-mode", choices=["auto", "smart", "readonly", "yolo"])
    run_parser.add_argument(
        "--mode",
        choices=list(MODE_CHOICES),
        default=None,
        help=(
            "Run posture, which picks a coherent set of completion gates. "
            "interactive (default): no gates that cost a model call. "
            "eval: full gate stack — LLM judge, acceptance contract, "
            "discriminating + stable evidence (~2 extra model calls per completion "
            "attempt); this is the benchmark configuration. "
            "rigorous: eval gates plus a plan/execute/critic agent. "
            "readonly: interactive gates with permissions forced read-only. "
            "standard is an alias for interactive. Omit to honor the profile's own."
        ),
    )
    run_parser.add_argument("--max-turns", type=int)
    run_parser.add_argument(
        "--deadline-sec",
        type=float,
        default=None,
        help=(
            "Wall-clock budget for the run. The agent paces itself against it and "
            "reserves turns to finish, which a turn count cannot express when one "
            "command may block for minutes."
        ),
    )
    run_parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high"],
        help="Enable extended thinking at this effort (cross-provider)",
    )
    run_parser.add_argument(
        "--thinking-budget",
        type=int,
        help="Anthropic extended-thinking budget in tokens (enables thinking)",
    )
    run_parser.add_argument(
        "--persistent-shell",
        action="store_true",
        help="Keep one shell alive across bash calls so cwd/env/venv persist (local env)",
    )
    run_parser.add_argument(
        "--no-post-edit-diagnostics",
        action="store_true",
        help="Disable the syntax check run after edit/write_file",
    )
    run_parser.add_argument(
        "--no-post-edit-lint",
        action="store_true",
        help="Disable the fast semantic lint (Python/ruff) run after edit/write_file",
    )
    run_parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Skip the session-start environment probe (cold start; the agent "
        "discovers the environment itself)",
    )
    run_parser.add_argument("--no-verifier", action="store_true")
    run_parser.add_argument("--no-three-step-summary", action="store_true")
    run_parser.add_argument("--json", action="store_true", help="Print JSONL events to stdout")
    run_parser.add_argument("--trajectory", help="Save event trajectory to JSONL file")
    run_parser.add_argument(
        "--resume",
        metavar="ID",
        help="Resume a saved session (full id, unique prefix, or 'latest' for this "
        "project's newest)",
    )
    run_parser.add_argument(
        "--all-projects",
        action="store_true",
        help="With --resume latest, resume the newest session from any project",
    )
    run_parser.add_argument(
        "--name",
        help="Name this session (unique in the project); resume or tag it by name later",
    )
    _add_tag_flags(run_parser)
    run_parser.add_argument(
        "--isolation",
        choices=["shared", "worktree", "auto"],
        default="shared",
        help="Where the session edits: the workspace itself (shared), its own Git "
        "worktree and garuda/<session> branch (worktree), or a worktree only when "
        "another session is editing (auto). A worktree is not confinement.",
    )

    chat_parser = subparsers.add_parser("chat", help="Interactive agent session with permission prompts")
    _add_model_flags(chat_parser)
    chat_parser.add_argument("--workspace", default=".")
    chat_parser.add_argument(
        "--workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
    )
    chat_parser.add_argument("--docker-image", default="ubuntu:22.04")
    chat_parser.add_argument("--docker-host")
    chat_parser.add_argument("--agent", default="build")
    _add_tag_flags(chat_parser)
    chat_parser.add_argument("--agents-dir")
    chat_parser.add_argument("--mcp-config")
    chat_parser.add_argument(
        "--mode",
        choices=list(MODE_CHOICES),
        default=None,
        help="Run posture (see `garuda run --help`); defaults to interactive.",
    )
    # No default, so an unset flag stays None and the profile/preset decides. Chat
    # is the interface built around permission prompts, so not being able to pick
    # the mode here was the one place the flag was most obviously missing.
    chat_parser.add_argument(
        "--permission-mode",
        choices=["auto", "smart", "readonly", "yolo"],
        default=None,
        help="Permission posture; overrides the profile and --mode.",
    )
    chat_parser.add_argument("--json", action="store_true")

    serve_parser = subparsers.add_parser("serve", help="Start JSON-RPC HTTP server for IDE integrations")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    _add_model_flags(serve_parser)
    serve_parser.add_argument("--agent", default="build")
    serve_parser.add_argument("--workspace", default=".")
    serve_parser.add_argument(
        "--workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
    )
    serve_parser.add_argument("--docker-image", default="ubuntu:22.04")
    serve_parser.add_argument("--docker-host")
    serve_parser.add_argument("--agents-dir")
    serve_parser.add_argument("--mcp-config")
    serve_parser.add_argument(
        "--token",
        default=os.environ.get("GARUDA_SERVE_TOKEN"),
        help="Bearer token required on requests (or set GARUDA_SERVE_TOKEN)",
    )
    serve_parser.add_argument(
        "--max-jobs",
        type=int,
        default=4,
        help="Max concurrent jobs for submit/status/result (default 4)",
    )
    serve_parser.add_argument(
        "--model-max-concurrency",
        type=int,
        default=0,
        help="Cap concurrent model calls per provider across jobs (0 = unlimited)",
    )

    # `web`, aliased `dashboard`: the name people reach for, while the module stays
    # `interfaces/web` so it does not collide with `garuda.eval.dashboard` (the
    # markdown cost table). Both spellings must be accepted in the dispatch below.
    web_parser = subparsers.add_parser(
        "web",
        aliases=["dashboard"],
        help="Serve the local web dashboard: read past runs and talk to an agent",
    )
    # No --host: the bind address is 127.0.0.1 and not configurable. See
    # interfaces/web/http.py::BIND_HOST for why.
    web_parser.add_argument("--port", type=int, default=WEB_DEFAULT_PORT)
    web_parser.add_argument(
        "--sessions-dir", help="Read runs from here instead of the configured sessions root"
    )
    web_parser.add_argument("--agents-dir", help="Directory with custom agent YAML profiles")
    web_parser.add_argument(
        "--read-only",
        action="store_true",
        help="Serve the run history only. Without this the dashboard can also talk to an "
        "agent, which runs tools as you — bounded by --max-permission, which defaults to "
        "asking before anything destructive.",
    )
    # Accepted and ignored: talking to an agent is the default now. Kept so an alias or a
    # shell history entry carrying it does not fail, rather than out of any real ambiguity.
    web_parser.add_argument("--allow-run", action="store_true", help=argparse.SUPPRESS)
    web_parser.add_argument(
        "--allow-workspace",
        action="append",
        default=[],
        metavar="DIR",
        help="A directory the agent may work in; repeatable. "
        "Defaults to the current directory. Requests name these by index, never by path.",
    )
    web_parser.add_argument(
        "--max-permission",
        choices=["readonly", "smart", "auto", "yolo"],
        default=WEB_DEFAULT_MAX_PERMISSION,
        help="The loosest permission posture a browser request may ask for "
        f"(default: {WEB_DEFAULT_MAX_PERMISSION}). A request may ask for this or stricter.",
    )
    web_parser.add_argument(
        "--web-model",
        default=None,
        help="Default model for dashboard conversations (reasoning role; unset honors bindings)",
    )
    web_parser.add_argument(
        "--web-agent", default="build", help="Default agent profile for dashboard conversations"
    )
    web_parser.add_argument(
        "--web-workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
        help="Workspace kind for dashboard conversations (default: local)",
    )
    web_parser.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser automatically"
    )

    doctor_parser = subparsers.add_parser("doctor", help="Diagnose and repair local Garuda state")
    doctor_parser.add_argument(
        "--recover-project-ids",
        action="store_true",
        help="Rebuild session project ids after the project key was lost",
    )

    sessions_parser = subparsers.add_parser("sessions", help="List recent saved sessions")
    sessions_parser.add_argument("--limit", type=int, default=20)
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_command")
    sessions_merge = sessions_sub.add_parser(
        "merge",
        help="Check a worktree session's work merged into a branch and publish it "
        "as refs/garuda/integration/<session>; never changes your checkout",
    )
    sessions_merge.add_argument("session", help="Session id, unique prefix, or name")
    sessions_merge.add_argument("--into", help="Destination branch (default: current branch)")
    sessions_merge.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="COMMAND",
        help="Check to run in Docker against the merged tree (repeatable; required)",
    )
    sessions_merge.add_argument(
        "--image", default="python:3.12-slim", help="Docker image for the checks"
    )
    sessions_merge.add_argument("--timeout", type=float, default=600)
    sessions_merge.add_argument("--workspace", default=".")
    sessions_remove = sessions_sub.add_parser(
        "remove-worktree", help="Remove a worktree session's worktree once its work is published"
    )
    sessions_remove.add_argument("session", help="Session id, unique prefix, or name")
    sessions_remove.add_argument(
        "--force", action="store_true", help="Remove even if the work was never published"
    )
    sessions_remove.add_argument("--workspace", default=".")

    mcp_parser = subparsers.add_parser("mcp", help="Inspect MCP server configuration")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")
    mcp_list = mcp_sub.add_parser(
        "list", help="Show resolved MCP config path(s) and the tools each server exposes"
    )
    mcp_list.add_argument("--workspace", default=".")
    mcp_list.add_argument(
        "--mcp-config", help="Explicit config path (skips auto-discovery/merge)"
    )
    mcp_list.add_argument(
        "--no-connect",
        action="store_true",
        help="Only show configured servers; do not connect to enumerate tools",
    )
    mcp_trust = mcp_sub.add_parser(
        "trust",
        help="Review and trust MCP servers that this project's own config defines",
    )
    mcp_trust.add_argument("names", nargs="*", help="Server names (default: every untrusted one)")
    mcp_trust.add_argument("--workspace", default=".")
    mcp_trust.add_argument(
        "--mcp-config",
        help="A project config file to review instead of the discovered ones "
        "(for example one a project profile names)",
    )
    mcp_trust.add_argument(
        "--yes",
        action="store_true",
        help="Trust the listed servers without asking (you have reviewed them)",
    )

    recipe_parser = subparsers.add_parser("recipe", help="Run YAML workflow recipes")
    recipe_sub = recipe_parser.add_subparsers(dest="recipe_command")
    recipe_run = recipe_sub.add_parser("run", help="Execute a recipe file")
    recipe_run.add_argument("recipe", help="Path to recipe YAML")
    _add_model_flags(recipe_run)
    recipe_run.add_argument("--workspace", default=".")
    recipe_run.add_argument(
        "--workspace-kind",
        choices=list(WORKSPACE_KINDS),
        default="local",
    )
    recipe_run.add_argument("--docker-image", default="ubuntu:22.04")
    recipe_run.add_argument("--docker-host")
    recipe_run.add_argument("--agents-dir")
    recipe_run.add_argument("--mcp-config")
    recipe_run.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Recipe parameter (repeatable)",
    )

    eval_parser = subparsers.add_parser("eval", help="Build and inspect evaluation evidence")
    eval_sub = eval_parser.add_subparsers(dest="eval_command")
    dual_model = eval_sub.add_parser(
        "dual-model", help="Build paired evidence from completed native sessions"
    )
    dual_sub = dual_model.add_subparsers(dest="dual_model_command")
    dual_report = dual_sub.add_parser(
        "report", help="Write a reproducible paired-trial report without running providers"
    )
    dual_report.add_argument(
        "--baseline", action="append", default=[], metavar="TASK=SESSION", help="Baseline trial"
    )
    dual_report.add_argument(
        "--candidate", action="append", default=[], metavar="TASK=SESSION", help="Candidate trial"
    )
    dual_report.add_argument(
        "--sessions-dir", help="Root containing persisted Garuda session directories"
    )
    dual_report.add_argument(
        "--task-mix", required=True, help="JSON manifest assigning every task to a mix category"
    )
    dual_report.add_argument(
        "--model-version",
        action="append",
        default=[],
        metavar="ROLE=VERSION",
        help="Pinned model version provenance (repeatable; reasoning required)",
    )
    dual_report.add_argument("--price-source", required=True, help="Provider invoice or pinned price source")
    dual_report.add_argument("--prompt-revision", required=True, help="Pinned prompt revision identifier")
    dual_report.add_argument("--evidence-scores", help="Optional JSON independent quality scores")
    dual_report.add_argument("--output", required=True, help="Destination JSON report")
    dual_report.add_argument("--overwrite", action="store_true", help="Replace an existing report")
    dual_report.add_argument(
        "--require-passing-gates",
        action="store_true",
        help="Exit nonzero after writing if release gates do not pass",
    )

    runtime_parser = subparsers.add_parser("runtime", help="Select and inspect runtimes")
    runtime_sub = runtime_parser.add_subparsers(dest="runtime_command")
    runtime_list = runtime_sub.add_parser("list", help="List configured runtimes")
    runtime_list.add_argument("--json", action="store_true", help="Print JSON health records")
    runtime_list.add_argument(
        "--workspace", default=".", help="Workspace whose project runtime refs apply"
    )
    runtime_inspect = runtime_sub.add_parser("inspect", help="Inspect one runtime")
    runtime_inspect.add_argument("runtime_id", help="Runtime id or alias")
    runtime_inspect.add_argument("--json", action="store_true", help="Print JSON health record")
    runtime_inspect.add_argument(
        "--workspace", default=".", help="Workspace whose project runtime refs apply"
    )
    runtime_handoff = runtime_sub.add_parser("handoff", help="Preview or execute a handoff")
    runtime_handoff.add_argument("--session", required=True, help="Source session id")
    runtime_handoff.add_argument("--to", required=True, help="Target runtime id")
    runtime_handoff.add_argument(
        "--workspace",
        default=None,
        help="Workspace root (default: the session's recorded workspace)",
    )
    runtime_handoff.add_argument(
        "--confirm",
        action="store_true",
        help="Execute the handoff transaction. Without it, only a preview prints.",
    )
    runtime_resume = runtime_sub.add_parser("resume", help="Resume a persisted session")
    runtime_resume.add_argument("--session", required=True, help="Session id to resume")
    runtime_resume.add_argument("-t", "--task", required=True, help="Continuation task")
    runtime_resume.add_argument("--workspace", default=".", help="Workspace root")
    runtime_resume.add_argument("--agent", default="build", help="Agent profile")
    _add_model_flags(runtime_resume)
    runtime_recover = runtime_sub.add_parser("recover", help="Classify and recover a session")
    runtime_recover.add_argument("--session", required=True, help="Session id")
    runtime_recover.add_argument("--json", action="store_true", help="Print JSON report")
    runtime_reclaim = runtime_sub.add_parser(
        "reclaim", help="Return a handed-off session whose target stopped to native"
    )
    runtime_reclaim.add_argument("--session", required=True, help="Session id")
    runtime_support = runtime_sub.add_parser("support", help="Print a redacted support bundle")
    runtime_support.add_argument("--session", required=True, help="Session id")

    return parser


def _parse_params(pairs: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"Invalid --param (expected KEY=VALUE): {item}")
        key, value = item.split("=", 1)
        params[key.strip()] = value.strip()
    return params


def run_doctor(args) -> int:
    """`garuda doctor`. Today it only recovers project ids; checks come with C.4."""
    import sys

    if not args.recover_project_ids:
        print("Nothing to do. Use --recover-project-ids after the project key was lost.")
        return 0
    from garuda.core.project_recovery import RecoveryRefused, recover_project_ids
    from garuda.core.sessions import SessionStore

    try:
        report = recover_project_ids(SessionStore().root)
    except RecoveryRefused as exc:
        print(f"Error: recovery refused: {exc}", file=sys.stderr)
        return 2
    if report.noop:
        print("The project key is present; there is nothing to recover.")
        return 0
    print(f"Recovered {len(report.mapped)} project(s); updated {report.sessions_updated} session(s).")
    if report.unmapped:
        print(
            f"{len(report.unmapped)} project(s) could not be verified (moved, replaced or "
            "missing) and keep their old ids; their sessions still resolve by full id."
        )
    return 0


def run_sessions(args) -> int:
    from garuda.core.sessions import SessionStore

    if getattr(args, "sessions_command", None) in ("merge", "remove-worktree"):
        return run_sessions_merge(args)
    sessions = SessionStore().list_sessions(limit=args.limit)
    if not sessions:
        print("No saved sessions.")
        return 0
    from garuda.runtime.session_state import effective_state, summary_label

    print(f"{'ID':<10} {'NAME':<24} {'STATE':<10} {'TURNS':>5}  {'UPDATED':<32} TASK")
    for meta in sessions:
        task = " ".join((meta.get("task") or "").split())
        if len(task) > 60:
            task = task[:57] + "..."
        print(
            f"{meta.get('session_id', '')[:8]:<10} "
            f"{(meta.get('name') or '-')[:24]:<24} "
            f"{summary_label(effective_state(meta)):<10} "
            f"{meta.get('turns', 0):>5}  "
            f"{meta.get('updated_at', ''):<32} "
            f"{task}"
        )
    return 0


def run_sessions_merge(args) -> int:
    """`garuda sessions merge` and `remove-worktree`."""
    import os
    import sys

    from garuda.core.project_identity import ProjectIdentityError
    from garuda.core.sessions import SessionStore
    from garuda.workspace.worktrees import WorktreeError, merge_session, remove_worktree

    store = SessionStore()
    try:
        session_id = store.resolve(args.session, workspace=os.path.realpath(args.workspace))
        if args.sessions_command == "remove-worktree":
            meta = store.load_meta(session_id)
            remove_worktree(meta, force=args.force)
            print(f"[garuda] removed {meta.get('worktree') or 'nothing (shared session)'}")
            return 0
        result = merge_session(
            store.load_meta(session_id),
            checks=args.check,
            image=args.image,
            destination=args.into,
            timeout=args.timeout,
        )
    except (WorktreeError, ProjectIdentityError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    for check in result.checks:
        print(f"[garuda] check passed in Docker: {check['command']}")
    print(f"[garuda] integration commit {result.commit} published as "
          f"refs/garuda/integration/{session_id}")
    print(f"[garuda] to apply it, with {result.destination} checked out:")
    print(f"  {result.apply_command}")
    return 0


def run_dual_model_report(args) -> int:
    """Build evidence from persisted sessions; this path never creates a model."""
    import sys

    from garuda.core.sessions import SessionStore
    from garuda.eval.dual_model import format_comparison
    from garuda.eval.paired_report import (
        PairedReportError,
        build_paired_report,
        load_evidence_scores,
        load_task_manifest,
        parse_model_versions,
        parse_trial_reference,
        write_paired_report,
    )

    try:
        baselines = [parse_trial_reference(value, trial="baseline") for value in args.baseline]
        candidates = [parse_trial_reference(value, trial="candidate") for value in args.candidate]
        report = build_paired_report(
            store=SessionStore(args.sessions_dir),
            baselines=baselines,
            candidates=candidates,
            task_manifest=load_task_manifest(args.task_mix),
            model_versions=parse_model_versions(args.model_version),
            price_source=args.price_source,
            prompt_revision=args.prompt_revision,
            evidence_scores=load_evidence_scores(args.evidence_scores),
        )
        target = write_paired_report(args.output, report, overwrite=args.overwrite)
    except PairedReportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote paired report: {target}")
    print(format_comparison(report.comparison))
    if args.require_passing_gates and not report.release_gates_passed:
        return 3
    return 0


async def run_mcp_list(args) -> int:
    """Show which MCP config file(s) resolve and the tools each server exposes."""
    from garuda.mcp.config import partition_mcp_servers, resolve_mcp_config_paths

    paths = resolve_mcp_config_paths(args.workspace, args.mcp_config)
    if not paths:
        print(
            "No MCP config found (looked for .agent/mcp.json|yaml, .garuda/mcp.json|yaml, "
            ".cursor/mcp.json, and the global ~/.agent/mcp.json)."
        )
        return 0

    print("Resolved MCP config path(s):")
    for path in paths:
        print(f"  {path}")

    user_paths = [args.mcp_config] if args.mcp_config else []
    servers, untrusted = partition_mcp_servers(
        paths, workspace=args.workspace, user_paths=user_paths
    )
    if not servers and not untrusted:
        print("\nNo servers defined in the resolved config.")
        return 0

    print(f"\n{len(servers) + len(untrusted)} server(s) configured:")
    for server in servers:
        target = server.url or f"{server.command} {' '.join(server.args)}".strip()
        print(f"  - {server.name} [{server.transport}] {target}")
    for server in untrusted:
        target = server.url or f"{server.command} {' '.join(server.args)}".strip()
        print(
            f"  - {server.name} [{server.transport}] {target}  "
            f"(project server, not trusted: run `garuda mcp trust {server.name}`)"
        )

    if args.no_connect:
        return 0

    print("\nConnecting to enumerate tools...")
    tools, manager = await build_toolkit(
        [], paths, workspace=args.workspace, mcp_user_paths=user_paths
    )
    try:
        mcp_tools = [t for t in tools if t.name.startswith("mcp__")]
        if not mcp_tools:
            print("  (no tools registered — servers may have failed to start; check logs)")
        for tool in mcp_tools:
            print(f"  {tool.name}")
    finally:
        if manager is not None:
            await manager.close()
    return 0


def run_mcp_trust(args, *, ask=input) -> int:
    """Show each untrusted project MCP server and record the user's trust in it.

    A grant binds this exact entry (and any repository script it runs) to this
    repository; editing either later means asking again.
    """
    import sys

    from garuda.mcp.config import partition_mcp_servers, resolve_mcp_config_paths
    from garuda.mcp.trust import describe, grant, repository_identity

    explicit = getattr(args, "mcp_config", None)
    paths = [explicit] if explicit else resolve_mcp_config_paths(args.workspace)
    _, untrusted = partition_mcp_servers(paths, workspace=args.workspace)
    wanted = set(args.names or [])
    candidates = [s for s in untrusted if not wanted or s.name in wanted]
    unknown = sorted(wanted - {s.name for s in untrusted})
    if unknown:
        print(
            "Not an untrusted project server here: " + ", ".join(unknown),
            file=sys.stderr,
        )
    if not candidates:
        print("No untrusted project MCP servers.")
        return 1 if unknown else 0
    if not args.yes and not sys.stdin.isatty():
        print(
            "Refusing to trust project MCP servers without a terminal; review them and "
            "pass --yes.",
            file=sys.stderr,
        )
        return 2
    repository = repository_identity(args.workspace)
    for server in candidates:
        print(f"\n{server.name}  (from {server.source_path})")
        print(f"  This would {describe(server)}")
        if server.env:
            print(f"  with environment variables: {', '.join(sorted(server.env))}")
        if not args.yes:
            answer = ask(f"Trust {server.name!r} for {repository}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("  skipped")
                continue
        grant(server, args.workspace)
        print("  trusted")
    return 0


def _configured_catalog(workspace: str = "."):
    """The single trusted runtime catalog for CLI runtime commands."""
    from garuda.interfaces.runtime_cli import configured_catalog

    return configured_catalog(workspace)


def _print_runtime_refusal(exc: Exception) -> int:
    import sys

    print(f"Error: runtime selection refused: {exc}", file=sys.stderr)
    print(
        "Check `runtimes`/`disabled_runtimes` in your global settings and the "
        "project's `runtime_refs`, or pass `--runtime native` to pin the native runtime.",
        file=sys.stderr,
    )
    return 2


async def run_runtime_command(args) -> int:
    """Dispatch `garuda runtime ...`. Preview paths never mutate."""
    from garuda.acp.catalog import RuntimeSettingsError
    from garuda.runtime.registry import RegistryError

    try:
        return await _run_runtime_command(args)
    except (RegistryError, RuntimeSettingsError) as exc:
        return _print_runtime_refusal(exc)


async def _run_runtime_command(args) -> int:
    import sys

    from garuda.context.pack import ContextPackManager
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runtime_cli import (
        cmd_handoff_confirm,
        cmd_handoff_preview,
        cmd_inspect_registry,
        cmd_list_registry,
        cmd_reclaim,
        cmd_recover,
    )
    from garuda.runtime.registry import RegistryError

    command = args.runtime_command
    if command == "list":
        catalog = _configured_catalog(args.workspace)
        print(
            cmd_list_registry(
                catalog.registry, as_json=args.json, project_disabled=catalog.project_disabled
            ),
            end="",
        )
        return 0
    if command == "inspect":
        catalog = _configured_catalog(args.workspace)
        try:
            print(
                cmd_inspect_registry(
                    catalog.registry,
                    args.runtime_id,
                    as_json=args.json,
                    project_disabled=catalog.project_disabled,
                ),
                end="",
            )
        except KeyError as exc:
            print(f"Error: {exc}")
            return 2
        return 0
    store = SessionStore()
    if command in {"handoff", "resume", "reclaim", "recover", "support"}:
        try:
            session_id = store.resolve(args.session)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    if command == "handoff":
        if not args.confirm:
            print(cmd_handoff_preview(store, session_id, args.to), end="")
            return 0
        from garuda.interfaces.run_guard import interactive_approval

        manager = ContextPackManager(store.session_dir(session_id))
        try:
            print(
                await cmd_handoff_confirm(
                    store,
                    session_id,
                    args.to,
                    workspace=args.workspace,
                    pack_manager=manager,
                    approval=interactive_approval(),
                ),
                end="",
            )
        except RegistryError:
            raise
        except Exception as exc:
            print(f"Error: {exc}")
            return 1
        return 0
    if command == "resume":
        from garuda.agents.setup import prepare_agent_run
        from garuda.interfaces.runtime_cli import cmd_resume
        from garuda.model.config import ConfigError

        try:
            prepared = await prepare_agent_run(
                args.agent,
                workspace=args.workspace,
                model=args.model,
                reasoning_model=args.reasoning_model,
                collection_model=args.collection_model,
                no_collection=args.no_collection,
            )
        except ConfigError as exc:
            print(f"Error: {exc}")
            return 2
        profile, config, permissions, tools, agent, mcp_manager = prepared
        model = prepared.reasoning
        try:
            print(
                await cmd_resume(
                    store=store,
                    session_id=session_id,
                    task=args.task,
                    model=model,
                    agent=agent,
                    tools=tools,
                    config=config,
                    permissions=permissions,
                    workspace=args.workspace,
                    mcp_manager=mcp_manager,
                    collection_model=prepared.collection,
                    collection_policy=prepared.collection_policy,
                ),
                end="",
            )
        except Exception as exc:
            print(f"Error: {exc}")
            return 1
        finally:
            if mcp_manager is not None:
                try:
                    await mcp_manager.close()
                except Exception:
                    pass
        return 0
    if command == "reclaim":
        from garuda.runtime.recovery import RecoveryError
        from garuda.workspace.lease import LeaseStore

        try:
            print(cmd_reclaim(store, session_id, leases=LeaseStore()), end="")
        except RecoveryError as exc:
            print(f"Error: reclaim refused: {exc}")
            return 1
        return 0
    if command == "recover":
        from garuda.runtime.recovery import RecoveryError

        try:
            print(cmd_recover(store, session_id, as_json=args.json), end="")
        except RecoveryError as exc:
            print(f"Error: recovery refused: {exc}")
            return 1
        return 0
    if command == "support":
        from garuda.interfaces.runtime_cli import cmd_support_bundle

        try:
            print(cmd_support_bundle(store, session_id), end="")
        except ValueError as exc:
            print(f"Error: {exc}")
            return 2
        return 0
    build_parser().parse_args(["runtime", "--help"])
    return 1


async def run_acp_command(args, task: str, catalog) -> int:
    """Run one task on a named ACP runtime via loud, explicit selection."""
    from garuda.interfaces.run_guard import interactive_approval
    from garuda.interfaces.runtime_cli import NativeStartupFallback, run_acp_task

    try:
        summary = await run_acp_task(
            task,
            runtime_id=args.runtime,
            workspace=args.workspace,
            catalog=catalog,
            approval=interactive_approval(),
            initial_selection=getattr(args, "_initial_selection", None),
            initial_plan=getattr(args, "_initial_plan", None),
            attached=getattr(args, "_attached", None),
            name=getattr(args, "name", None),
        )
    except NativeStartupFallback:
        raise
    except Exception as exc:
        print(f"Error: {exc}")
        return 1
    print(
        f"session {summary['session_id']}: {summary['status']} "
        f"(turn {summary['turn']}, {summary['events']} normalized events; "
        "the ACP result is not verified by Garuda)"
    )
    return 0 if summary["status"] == "completed" else 1


async def run_task(args) -> int:
    import sys

    task = args.task
    if args.file:
        task = Path(args.file).read_text(encoding="utf-8")
    if not task:
        print("Error: provide -t/--task or -f/--file", file=sys.stderr)
        return 1
    # Session tags are resolved — or refused — before any runtime, model,
    # workspace or prompt exists (B.7).
    attached = _session_tags(args, task)
    args._attached = attached

    # Resolve policy before constructing a model, toolkit, workspace, or
    # provider adapter. The selected id is then handed to the matching
    # executor below; the native default is not an implicit bypass.
    from garuda.agents.setup import (
        prepare_runtime_catalog,
        select_initial_runtime_async,
        select_runtime,
    )
    from garuda.eval.costs import estimate_cost
    from garuda.interfaces.runtime_cli import NativeStartupFallback

    # P2's explicit opt-in router may choose a policy target first. P1 then
    # resolves and explains the initial owner from the same trusted registry;
    # its selected id is the executor below, never merely an audit record.
    runtime_catalog = prepare_runtime_catalog(args.workspace)
    # `None` means the flag was omitted. Naming a runtime, including `native`,
    # is an explicit choice that routing rules and the classifier never override.
    runtime_named = args.runtime is not None
    requested_runtime = args.runtime if runtime_named else "native"
    if requested_runtime != "native":
        # Resolve aliases and the global disabled gate before selection turns
        # an explicit request into a candidate id. This preserves the public
        # fail-closed error for a disabled alias and avoids treating aliases as
        # unconfigured runtime ids in the generic selector.
        requested_runtime = runtime_catalog.registry.get(
            requested_runtime
        ).runtime_id
    if runtime_named and requested_runtime == "native":
        routed_runtime = "native"
    else:
        routed_runtime = select_runtime(args.workspace, requested_runtime)
    explicit_native = runtime_named and routed_runtime == "native"
    # This catalog is passed through selection and launch. That prevents a
    # selection probe from observing different trusted registry facts than the
    # executor it chooses. When no explicit, profile, or rule source selects,
    # an enabled trusted classifier may recommend one revalidated candidate
    # (#80); it runs before any executor exists and cannot start a runtime.
    initial_plan = await select_initial_runtime_async(
        workspace=args.workspace,
        task=task,
        catalog=runtime_catalog,
        agent=getattr(args, "agent", "build"),
        mode=getattr(args, "mode", None) or "",
        explicit_runtime=(
            routed_runtime if routed_runtime != "native" or explicit_native else None
        ),
        workspace_kind=getattr(args, "workspace_kind", "local"),
        permission_ceiling=getattr(args, "permission_mode", None) or "smart",
        available_runtime_ids=(
            # An explicit, registry-authorized runtime must select its ACP
            # executor even when discovery has already reported a missing
            # binary. The ACP launch path then emits its established loud
            # installation error; it must never silently become native.
            frozenset({routed_runtime}) if routed_runtime != "native" else None
        ),
        cost_estimator=estimate_cost,
    )
    args.runtime = initial_plan.selection.selected
    args._initial_selection = initial_plan.selection
    args._initial_plan = initial_plan
    fallback_store = None
    fallback_events = None
    if getattr(args, "runtime", "native") != "native":
        # Resolved before constructing any model, tools, or workspace state,
        # preserving the fail-closed disabled/alias policy on `garuda run`.
        # An ACP selection launches through the ACP path with the same
        # catalog; an alias of the native loop continues below. A refused
        # selection raises `RegistryError`, which `main()` reports as a
        # message with exit status 2 (`_run_with_runtime_gate`).
        from garuda.runtime.protocol import RuntimeKind

        selected = runtime_catalog.registry.get(args.runtime)
        if selected.kind is RuntimeKind.ACP:
            try:
                return await run_acp_command(args, task, runtime_catalog)
            except NativeStartupFallback as fallback:
                # A failed ACP start may transfer exactly once to native only
                # after `run_acp_task` verified the workspace is unchanged.
                # Reuse its session trail so the explanation and both runtime
                # tenures remain one recoverable session.
                args.runtime = fallback.selection.selected
                args._initial_selection = fallback.selection
                fallback_store = fallback.store
                fallback_events = fallback.events

    # Native selection still goes through the common trusted boundary before
    # any toolkit or workspace startup. ACP selection is resolved by
    # ``run_acp_command`` through the same trusted registry service.
    from garuda.agents.setup import prepare_agent_run

    runtime_catalog.select_for_native_facade(args.runtime)
    from garuda.config.agent_home import resolve_agents_dirs
    from garuda.model.config import ConfigError
    from garuda.model.factory import safe_model_identity

    agents_dir = resolve_agents_dirs(args.workspace, args.agents_dir)
    try:
        prepared = await prepare_agent_run(
            args.agent,
            workspace=args.workspace,
            agents_dir=args.agents_dir,
            mcp_config_path=args.mcp_config,
            mode=args.mode,
            permission_mode=args.permission_mode,
            model=getattr(args, "model", None),
            reasoning_model=getattr(args, "reasoning_model", None),
            collection_model=getattr(args, "collection_model", None),
            no_collection=getattr(args, "no_collection", False),
            reasoning_effort=getattr(args, "reasoning_effort", None),
            thinking_budget_tokens=getattr(args, "thinking_budget", None),
            load_project_tools=getattr(args, "load_project_tools", None),
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    config = prepared.config
    # Preset first, explicit flags after: every `if args.x` below is a narrower
    # statement of intent than the posture and must win over it.
    if args.max_turns is not None:
        config.max_turns = args.max_turns
    if getattr(args, "deadline_sec", None) is not None:
        config.deadline_sec = args.deadline_sec
    if args.no_verifier:
        config.enable_verifier = False
    if args.no_three_step_summary:
        config.enable_three_step_summary = False
    if getattr(args, "persistent_shell", False):
        config.persistent_shell = True
    if getattr(args, "no_post_edit_diagnostics", False):
        config.post_edit_diagnostics = False
    if getattr(args, "no_post_edit_lint", False):
        config.post_edit_lint = False
    if getattr(args, "no_bootstrap", False):
        config.bootstrap_environment = False
    config.workspace_kind = args.workspace_kind
    config.docker_image = args.docker_image
    config.docker_host = args.docker_host
    config.sandbox_allow_network = getattr(args, "allow_network", False)
    config.sandbox_require = not getattr(args, "allow_unsandboxed", False)
    config.docker_network = "none" if getattr(args, "no_network", False) else "bridge"
    config.docker_memory = getattr(args, "docker_memory", "2g")
    config.docker_cpus = getattr(args, "docker_cpus", "2")

    model = prepared.reasoning
    permissions = prepared.permissions
    agent = prepared.agent
    tools = prepared.tools
    mcp_manager = prepared.mcp_manager
    if not args.json:
        print(
            f"[garuda] reasoning={safe_model_identity(model)} "
            f"({prepared.provenance['reasoning'].provenance.value})",
        )
        if prepared.collection is not None:
            print(
                f"[garuda] collection={safe_model_identity(prepared.collection)} "
                f"({prepared.provenance['collection'].provenance.value})",
            )
    events = fallback_events or EventStore()

    result = await run_agent_task(
        task=task,
        model=model,
        agent=agent,
        tools=tools,
        config=config,
        permissions=permissions,
        workspace=args.workspace,
        events=events,
        emit_json=args.json,
        workspace_kind=args.workspace_kind,
        docker_image=args.docker_image,
        docker_host=args.docker_host,
        mcp_manager=mcp_manager,
        agents_dir=agents_dir,
        resume=args.resume,
        resume_all_projects=getattr(args, "all_projects", False),
        session_name=getattr(args, "name", None),
        isolation=getattr(args, "isolation", "shared"),
        context_attached=attached,
        runtime_catalog=runtime_catalog,
        runtime_ref=args.runtime,
        initial_selection=args._initial_selection,
        store=fallback_store,
        collection_model=prepared.collection,
        collection_policy=prepared.collection_policy,
    )

    if args.trajectory:
        events.save(args.trajectory)
    if not args.json:
        print(result.final_message)
        if getattr(args, "isolation", "shared") != "shared" or args.resume:
            _print_worktree_note(fallback_store, events.session_id)
    return 0 if result.success else 1


def _print_worktree_note(store, session_id: str) -> None:
    from garuda.core.sessions import SessionStore

    try:
        meta = (store or SessionStore()).load_meta(session_id)
    except Exception:
        return
    if meta.get("isolation") != "worktree":
        return
    print(f"[garuda] worked in worktree {meta['worktree']} on branch {meta['branch']} "
          "(a separate checkout, not a sandbox)")
    if meta.get("dirty_source"):
        print("[garuda] uncommitted changes in the source checkout were not carried over")
    print(f"[garuda] to integrate: garuda sessions merge {session_id[:8]} --check 'COMMAND'")


async def run_recipe_command(args) -> int:
    import sys

    from garuda.config.agent_home import resolve_agents_dirs
    from garuda.config.recipes import load_recipe, run_recipe

    try:
        params = _parse_params(args.param)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    recipe = load_recipe(args.recipe)
    env, handle = await resolve_environment(
        args.workspace_kind,
        args.workspace,
        args.docker_image,
        docker_host=args.docker_host,
    )
    events = EventStore()
    try:
        results = await run_recipe(
            recipe,
            params,
            model=getattr(args, "model", None),
            reasoning_model=getattr(args, "reasoning_model", None),
            collection_model=getattr(args, "collection_model", None),
            no_collection=getattr(args, "no_collection", False),
            env=env,
            workspace=args.workspace,
            events=events,
            agents_dir=resolve_agents_dirs(args.workspace, args.agents_dir),
            mcp_config_path=args.mcp_config,
        )
    finally:
        await cleanup_workspace(handle)

    for index, result in enumerate(results, start=1):
        print(f"--- Step {index} ({'ok' if result.success else 'failed'}) ---")
        print(result.final_message)
    # `run_recipe` stops at the first failed step. Say so, and name what did not run:
    # otherwise the output ends on a failed step and a 3-step recipe that stopped at
    # step 1 looks identical to a 1-step recipe that failed.
    skipped = len(recipe.steps) - len(results)
    if skipped > 0:
        print(
            f"--- Recipe stopped at step {len(results)}; {skipped} later step(s) "
            f"were not run ---",
            file=sys.stderr,
        )
    return 0 if results and results[-1].success else 1


async def run_serve(args) -> int:
    from garuda.interfaces.server import ServerConfig, serve

    config = ServerConfig(
        host=args.host,
        port=args.port,
        model=getattr(args, "model", None),
        reasoning_model=getattr(args, "reasoning_model", None),
        collection_model=getattr(args, "collection_model", None),
        no_collection=getattr(args, "no_collection", False),
        agent=args.agent,
        workspace=args.workspace,
        workspace_kind=args.workspace_kind,
        docker_image=args.docker_image,
        docker_host=args.docker_host,
        agents_dir=args.agents_dir,
        mcp_config=args.mcp_config,
        token=args.token,
        max_jobs=getattr(args, "max_jobs", 4),
        model_max_concurrency=getattr(args, "model_max_concurrency", 0),
    )
    await serve(config)
    return 0


async def run_web(args) -> int:
    """Serve the dashboard until interrupted."""
    from garuda.interfaces.web import DashboardConfig, serve_dashboard

    config = DashboardConfig(
        port=args.port,
        sessions_dir=Path(args.sessions_dir) if args.sessions_dir else None,
        agents_dir=Path(args.agents_dir) if args.agents_dir else None,
        allow_run=not args.read_only,
        workspaces=tuple(Path(p) for p in (args.allow_workspace or [])),
        max_permission=args.max_permission,
        model=args.web_model or "",
        agent=args.web_agent,
        workspace_kind=args.web_workspace_kind,
        open_browser=not args.no_browser,
    )
    try:
        await serve_dashboard(config)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    return 0


def _run_with_runtime_gate(args) -> int:
    """`garuda run`, with a refused runtime selection as a message, not a traceback."""
    import asyncio

    from garuda.acp.catalog import RuntimeSettingsError
    from garuda.context.brief import BriefBudgetExceeded
    from garuda.context.tags import TagError
    from garuda.runtime.capacity import CapacityError
    from garuda.runtime.registry import RegistryError
    from garuda.workspace.lease import LeaseError
    from garuda.workspace.worktrees import WorktreeError

    try:
        return asyncio.run(run_task(args))
    except (RegistryError, RuntimeSettingsError) as exc:
        return _print_runtime_refusal(exc)
    except (LeaseError, CapacityError, WorktreeError, TagError, BriefBudgetExceeded) as exc:
        # The workspace is held by another run, the runtime is at its
        # capacity, or no worktree could be made: a refusal, not a crash.
        import sys

        print(f"Error: {exc}", file=sys.stderr)
        return 2


def main() -> None:
    import asyncio

    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        raise SystemExit(_run_with_runtime_gate(args))
    if args.command == "chat":
        raise SystemExit(asyncio.run(chat_loop(args)))
    if args.command == "serve":
        raise SystemExit(asyncio.run(run_serve(args)))
    if args.command in ("web", "dashboard"):
        raise SystemExit(asyncio.run(run_web(args)))
    if args.command == "runtime":
        raise SystemExit(asyncio.run(run_runtime_command(args)))
    if args.command == "doctor":
        raise SystemExit(run_doctor(args))
    if args.command == "sessions":
        raise SystemExit(run_sessions(args))
    if args.command == "eval":
        if args.eval_command == "dual-model" and args.dual_model_command == "report":
            raise SystemExit(run_dual_model_report(args))
        parser.parse_args(["eval", "--help"])
        raise SystemExit(1)
    if args.command == "mcp":
        if args.mcp_command == "list":
            raise SystemExit(asyncio.run(run_mcp_list(args)))
        if args.mcp_command == "trust":
            raise SystemExit(run_mcp_trust(args))
        parser.parse_args(["mcp", "--help"])
        raise SystemExit(1)
    if args.command == "recipe" and args.recipe_command == "run":
        raise SystemExit(asyncio.run(run_recipe_command(args)))
    parser.print_help()
    raise SystemExit(1)


if __name__ == "__main__":
    main()
