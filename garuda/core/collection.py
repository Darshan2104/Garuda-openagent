"""Collection-worker tool policy.

This module builds a deliberately narrow, non-mutating toolkit.  It is a local
guardrail, not a confinement boundary: strict write isolation still requires a
read-only mount or an isolated workspace snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from garuda.context.manager import FORK_BRIEF, FORK_NONE, ContextManager
from garuda.core.events import EventStore, EventType
from garuda.core.evidence import is_side_effect_free
from garuda.core.permissions import PermissionDecision, PermissionEngine
from garuda.core.subagent import persist_child_events
from garuda.core.termination import TerminalDecision
from garuda.model.config import CollectionPolicy as ModelCollectionPolicy
from garuda.model.factory import safe_model_identity
from garuda.tools.collection import SubmitCollectionTool
from garuda.tools.protocol import Tool, ToolContext, ToolEffect, tool_effect
from garuda.tools.registry import builtin_registry
from garuda.types import AgentConfig, Message, Role, ToolCall, ToolResult
from garuda.workspace.protocol import Environment

DEFAULT_COLLECTION_TOOL_NAMES = frozenset(
    {
        "read_file",
        "grep",
        "glob",
        "ls",
        "read_pdf",
        "read_spreadsheet",
        "image_read",
        "buffer_grep",
        "buffer_slice",
        "buffer_list",
        "buffer_query",
        "web_fetch",
        "web_search",
    }
)

# Names that remain forbidden even if a malicious profile supplies an object
# carrying a forged read-only declaration. Most are already excluded by effect;
# the name ceiling is defense in depth for control-plane and delegation tools.
COLLECTION_HARD_DENY = frozenset(
    {
        "write_file",
        "edit",
        "multi_edit",
        "notebook_edit",
        "bash_background",
        "task_output",
        "kill_task",
        "tmux_exec",
        "tmux_capture",
        "todo",
        "update_goal",
        "contract",
        "task_complete",
        "invoke_subagent",
        "delegate_collection",
    }
)


@dataclass(frozen=True)
class CollectionPolicy:
    """The independent selectors whose intersection forms a worker toolkit."""

    profile_tools: frozenset[str] = DEFAULT_COLLECTION_TOOL_NAMES
    requested_tools: frozenset[str] | None = None
    global_tools: frozenset[str] = DEFAULT_COLLECTION_TOOL_NAMES
    network_enabled: bool = False
    allow_readonly_shell: bool = False


class CollectionJobState(str, Enum):
    REQUESTED = "requested"
    VALIDATED = "validated"
    BUDGET_RESERVED = "budget_reserved"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_STALE = "completed_stale"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CollectionRequest:
    objective: str
    questions: tuple[str, ...]
    allowed_paths: tuple[str, ...] = ()
    allowed_sources: tuple[str, ...] = ()
    handoff: str = FORK_BRIEF
    max_turns: int | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    location: str
    detail: str


@dataclass(frozen=True)
class CollectionReport:
    summary: str
    findings: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    unknowns: tuple[str, ...]
    buffer_ids: tuple[str, ...]
    workspace_revision: str
    stale: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CollectionAttempt:
    attempt_id: str
    session_id: str
    model: str
    turns: int
    usage: dict
    elapsed_ms: int
    model_calls: int = 0
    cost_usd: float | None = None
    cost_known: bool = False
    model_binding_role: str = "collection"
    call_purpose: str = "collector"


@dataclass(frozen=True)
class CollectionReservation:
    """An atomic claim against the per-run collection budget."""

    job_id: str
    tokens: int
    cost_usd: float | None
    wall_time_sec: float | None


@dataclass
class CollectionBudgetLedger:
    """Lock-protected reservations and actual collection spend for one run.

    Token and known-cost reservations are released when an attempt terminates;
    actual usage remains charged.  This makes admission atomic without treating
    failed or cancelled provider calls as free.
    """

    budget: object
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    jobs_started: int = 0
    active_jobs: int = 0
    reserved_tokens: int = 0
    actual_tokens: int = 0
    reserved_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    cost_unknown_attempts: int = 0
    wall_time_ms: int = 0
    fallback_attempts: int = 0

    async def reserve(
        self,
        job_id: str,
        *,
        tokens: int,
        cost_usd: float | None,
        wall_time_sec: float | None,
    ) -> CollectionReservation:
        async with self._lock:
            if self.jobs_started >= self.budget.max_jobs_per_run:
                raise ValueError("collection job budget exhausted")
            total_ceiling = self.budget.max_total_tokens_per_run
            if (
                total_ceiling is not None
                and self.actual_tokens + self.reserved_tokens + tokens > total_ceiling
            ):
                raise ValueError("collection token budget exhausted")
            cost_ceiling = self.budget.max_cost_usd_per_run
            if cost_ceiling is not None:
                if cost_usd is None and total_ceiling is None:
                    raise ValueError(
                        "collection dollar budget cannot admit an unpriceable model; "
                        "configure a total-token ceiling or model pricing"
                    )
                if (
                    cost_usd is not None
                    and self.actual_cost_usd + self.reserved_cost_usd + cost_usd > cost_ceiling
                ):
                    raise ValueError("collection dollar budget exhausted")
            self.jobs_started += 1
            self.active_jobs += 1
            self.reserved_tokens += tokens
            self.reserved_cost_usd += cost_usd or 0.0
            return CollectionReservation(job_id, tokens, cost_usd, wall_time_sec)

    async def settle(
        self,
        reservation: CollectionReservation,
        *,
        usage: dict | None,
        cost_usd: float | None,
        elapsed_ms: int,
    ) -> None:
        tokens = _usage_tokens(usage)
        async with self._lock:
            self.active_jobs = max(0, self.active_jobs - 1)
            self.reserved_tokens = max(0, self.reserved_tokens - reservation.tokens)
            self.reserved_cost_usd = max(
                0.0, self.reserved_cost_usd - (reservation.cost_usd or 0.0)
            )
            self.actual_tokens += tokens
            self.wall_time_ms += max(0, elapsed_ms)
            if cost_usd is None and tokens:
                self.cost_unknown_attempts += 1
            elif cost_usd is not None:
                self.actual_cost_usd = round(self.actual_cost_usd + cost_usd, 8)

    async def note_fallback(self) -> None:
        async with self._lock:
            self.fallback_attempts += 1

    def snapshot(self) -> dict:
        return {
            "jobs_started": self.jobs_started,
            "active_jobs": self.active_jobs,
            "reserved_tokens": self.reserved_tokens,
            "actual_tokens": self.actual_tokens,
            "reserved_cost_usd": round(self.reserved_cost_usd, 8),
            "actual_cost_usd": round(self.actual_cost_usd, 8),
            "cost_unknown_attempts": self.cost_unknown_attempts,
            "wall_time_ms": self.wall_time_ms,
            "fallback_attempts": self.fallback_attempts,
        }


@dataclass(frozen=True)
class WorkspaceSnapshot:
    head: str
    dirty_fingerprint: str
    scoped_fingerprint: str
    background_writer: bool


def _usage_tokens(usage: dict | None) -> int:
    if not usage:
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return max(0, int(total))
    return max(0, int(usage.get("prompt_tokens", 0) or 0)) + max(
        0, int(usage.get("completion_tokens", 0) or 0)
    )


def estimate_cost(_model_name: str | None, usage: dict | None) -> float | None:
    """Return only provider-reported spend in the runtime layer.

    The agent must not import the evaluation package: benchmark pricing tables
    are an evaluation concern, while a provider invoice is safe runtime evidence.
    Absent an invoice, cost remains unknown rather than becoming a guessed zero.
    """
    if not isinstance(usage, dict):
        return None
    value = usage.get("cost_usd")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _merge_usage(target: dict[str, int], usage: dict | None) -> None:
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        value = (usage or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
            target[key] = target.get(key, 0) + int(value)


MAX_OBJECTIVE_CHARS = 4_000
MAX_QUESTIONS = 32
MAX_REPORT_CHARS = 24_000
MAX_REPORT_ITEMS = 128
_EVIDENCE_KINDS = frozenset({"file", "command", "document", "url", "buffer"})


def _strings(value: object, *, field: str, required: bool = False) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array of strings")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} entries must be non-empty strings")
        cleaned.append(item.strip())
    if required and not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > MAX_REPORT_ITEMS:
        raise ValueError(f"{field} has too many entries")
    return tuple(cleaned)


def parse_collection_request(
    arguments: object, policy: ModelCollectionPolicy
) -> CollectionRequest:
    """Parse and narrow an untrusted public-tool request."""
    if not isinstance(arguments, dict):
        raise ValueError("collection request must be an object")
    known = {
        "objective", "questions", "allowed_paths", "allowed_sources",
        "handoff", "max_turns", "max_tokens",
    }
    unknown = set(arguments) - known
    if unknown:
        raise ValueError(f"unknown collection request fields: {sorted(unknown)}")
    objective = arguments.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("objective must be a non-empty string")
    objective = objective.strip()
    if len(objective) > MAX_OBJECTIVE_CHARS:
        raise ValueError(f"objective exceeds {MAX_OBJECTIVE_CHARS} characters")
    questions = _strings(arguments.get("questions"), field="questions", required=True)
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(f"questions exceeds {MAX_QUESTIONS} entries")
    handoff = arguments.get("handoff", policy.handoff)
    if handoff not in (FORK_NONE, FORK_BRIEF):
        raise ValueError("handoff must be 'none' or 'brief'; full context is forbidden")

    def bounded_int(name: str, ceiling: int) -> int | None:
        value = arguments.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        if value > ceiling:
            raise ValueError(f"{name}={value} exceeds collection ceiling {ceiling}")
        return value

    sources = _strings(arguments.get("allowed_sources"), field="allowed_sources")
    for source in sources:
        parsed = urlparse(source)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"allowed source must be an http(s) URL: {source!r}")
    return CollectionRequest(
        objective=objective,
        questions=questions,
        allowed_paths=_strings(arguments.get("allowed_paths"), field="allowed_paths"),
        allowed_sources=sources,
        handoff=handoff,
        max_turns=bounded_int("max_turns", policy.budget.max_turns_per_job),
        max_tokens=bounded_int("max_tokens", policy.budget.max_tokens_per_job),
    )


def _relative_workspace_path(location: str, workspace_root: str) -> str:
    """Normalize a path into a traversal-free workspace-relative spelling."""
    root = Path(workspace_root)
    candidate = Path(location)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"path is outside the workspace: {location}") from exc
    pure = PurePosixPath(str(candidate).replace("\\", "/"))
    if any(part == ".." for part in pure.parts):
        raise ValueError(f"path traversal is outside the workspace: {location}")
    normalized = str(pure).removeprefix("./") or "."
    return normalized


def _in_path_scope(location: str, allowed: tuple[str, ...], workspace_root: str) -> bool:
    relative = _relative_workspace_path(location, workspace_root)
    if not allowed:
        return True
    for raw in allowed:
        scope = _relative_workspace_path(raw, workspace_root).rstrip("/") or "."
        if scope == "." or relative == scope or relative.startswith(scope + "/"):
            return True
    return False


def _url_in_scope(location: str, allowed: tuple[str, ...]) -> bool:
    target = urlparse(location)
    if target.scheme not in ("http", "https") or not target.netloc:
        return False
    for raw in allowed:
        scope = urlparse(raw)
        if target.scheme != scope.scheme or target.netloc != scope.netloc:
            continue
        base_path = scope.path.rstrip("/")
        if not base_path or target.path == base_path or target.path.startswith(base_path + "/"):
            return True
    return False


class ScopedCollectionTool:
    """Enforce request path/source/buffer scope before a read-only delegate."""

    def __init__(
        self,
        delegate: Tool,
        *,
        request: CollectionRequest,
        workspace_root: str,
        buffer_ids: set[str],
    ) -> None:
        self._delegate = delegate
        self._request = request
        self._workspace_root = workspace_root
        self._buffer_ids = buffer_ids
        self.name = delegate.name
        self.description = delegate.description
        self.parameters = delegate.parameters
        self.effect = tool_effect(delegate)

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        path_arg = {
            "read_file": "path", "read_pdf": "path", "read_spreadsheet": "path",
            "image_read": "path", "grep": "path", "glob": "path", "ls": "path",
        }.get(self.name)
        if path_arg is not None:
            location = arguments.get(path_arg) or "."
            try:
                allowed = _in_path_scope(
                    str(location), self._request.allowed_paths, self._workspace_root
                )
            except ValueError as exc:
                return ToolResult(tool_call_id="", content=str(exc), is_error=True)
            if not allowed:
                return ToolResult(
                    tool_call_id="",
                    content=f"Collection scope denied path: {location}",
                    is_error=True,
                )
        if self.name.startswith("buffer_"):
            buffer_id = arguments.get("buffer_id")
            if self.name == "buffer_list" or buffer_id not in self._buffer_ids:
                return ToolResult(
                    tool_call_id="",
                    content=f"Collection scope denied buffer: {buffer_id or '(list)'}",
                    is_error=True,
                )
        if self.name == "web_fetch":
            url = str(arguments.get("url") or "")
            if not _url_in_scope(url, self._request.allowed_sources):
                return ToolResult(
                    tool_call_id="", content=f"Collection scope denied source: {url}", is_error=True
                )
        if self.name == "web_search":
            # Search results cannot be constrained to a source prefix. A scoped
            # worker may fetch named sources but cannot perform an open web search.
            return ToolResult(
                tool_call_id="",
                content="Collection scope denies open web search; use an allowed source URL.",
                is_error=True,
            )
        return await self._delegate.execute(arguments, env, ctx)


class CollectionBufferView:
    """Expose only handed-off and child-created buffers to one worker."""

    def __init__(self, parent: object, allowed_ids: set[str]) -> None:
        self._parent = parent
        self._allowed_ids = allowed_ids
        self.threshold_bytes = parent.threshold_bytes

    def exceeds(self, content: str, threshold: int | None = None) -> bool:
        return self._parent.exceeds(content, threshold)

    def store(self, buffer_id: str, content: str, **kwargs):
        ref = self._parent.store(buffer_id, content, **kwargs)
        self._allowed_ids.add(buffer_id)
        return ref

    def _require(self, buffer_id: str) -> None:
        if buffer_id not in self._allowed_ids:
            raise KeyError(f"No collection-scoped buffer {buffer_id!r}")

    def read(self, buffer_id: str) -> str:
        self._require(buffer_id)
        return self._parent.read(buffer_id)

    def grep(self, buffer_id: str, pattern: str, max_results: int = 100):
        self._require(buffer_id)
        return self._parent.grep(buffer_id, pattern, max_results=max_results)

    def slice(self, buffer_id: str, start_line: int, end_line: int) -> str:
        self._require(buffer_id)
        return self._parent.slice(buffer_id, start_line, end_line)

    def list_buffers(self):
        return [
            ref for ref in self._parent.list_buffers() if ref.buffer_id in self._allowed_ids
        ]


@dataclass
class CollectionPermissionCeiling:
    """Require both the parent authority and the read-only child policy."""

    parent: PermissionEngine
    child: PermissionEngine
    mode: str = "readonly"

    @property
    def approval_handler(self):
        return None

    def check_tool(self, tool_name: str) -> PermissionDecision:
        decisions = (self.parent.check_tool(tool_name), self.child.check_tool(tool_name))
        if PermissionDecision.DENY in decisions:
            return PermissionDecision.DENY
        if PermissionDecision.ASK in decisions:
            return PermissionDecision.ASK
        return PermissionDecision.ALLOW

    @staticmethod
    def _strictest(
        first: PermissionDecision, second: PermissionDecision
    ) -> PermissionDecision:
        if PermissionDecision.DENY in (first, second):
            return PermissionDecision.DENY
        if PermissionDecision.ASK in (first, second):
            return PermissionDecision.ASK
        return PermissionDecision.ALLOW

    def check_path(self, path: str, operation: str) -> PermissionDecision:
        return self._strictest(
            self.parent.check_path(path, operation),
            self.child.check_path(path, operation),
        )

    def check_command(self, command: str) -> PermissionDecision:
        return self._strictest(
            self.parent.check_command(command),
            self.child.check_command(command),
        )

    async def evaluate_tool_call(
        self, tool_name: str, arguments: dict
    ) -> tuple[bool, str | None]:
        parent_allowed, parent_reason = await self.parent.evaluate_tool_call(
            tool_name, arguments
        )
        if not parent_allowed:
            return False, parent_reason or "Parent run permission ceiling denied the call"
        return await self.child.evaluate_tool_call(tool_name, arguments)


@dataclass
class CollectionCompletionGate:
    """Validate the worker's structured terminal submission."""

    context: ContextManager
    request: CollectionRequest
    env: Environment
    buffer: object | None
    workspace_root: str
    workspace_revision: str
    allowed_buffer_ids: set[str]
    permissions: CollectionPermissionCeiling | None = None
    tool_name: str = "submit_collection"
    supports_forced_submission: bool = False
    report: CollectionReport | None = None

    async def attempt(
        self, call: ToolCall, *, turn: int | None = None
    ) -> TerminalDecision:
        try:
            report = await self._validate(call.arguments)
        except ValueError as exc:
            feedback = f"Collection report rejected: {exc}"
            self.context.append(
                Message(
                    role=Role.TOOL,
                    content=feedback,
                    name=self.tool_name,
                    tool_call_id=call.id,
                )
            )
            return TerminalDecision(False, feedback)
        self.report = report
        return TerminalDecision(True, json.dumps(report.to_dict(), sort_keys=True))

    def flush_notes(self) -> None:
        return None

    async def _validate(self, data: object) -> CollectionReport:
        if not isinstance(data, dict):
            raise ValueError("submission must be an object")
        required = {"summary", "findings", "evidence", "unknowns", "buffer_ids"}
        missing = required - set(data)
        unknown = set(data) - required
        if missing:
            raise ValueError(f"missing fields: {sorted(missing)}")
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string")
        findings = _strings(data.get("findings"), field="findings")
        unknowns = _strings(data.get("unknowns"), field="unknowns")
        if not findings and not unknowns:
            raise ValueError("report must contain findings or explicit unknowns")
        buffer_ids = _strings(data.get("buffer_ids"), field="buffer_ids")
        for buffer_id in buffer_ids:
            if buffer_id not in self.allowed_buffer_ids:
                raise ValueError(f"buffer is not accessible from this handoff: {buffer_id}")
            try:
                self.buffer.read(buffer_id)  # type: ignore[union-attr]
            except (AttributeError, KeyError):
                raise ValueError(f"buffer does not exist: {buffer_id}") from None

        raw_evidence = data.get("evidence")
        if not isinstance(raw_evidence, list) or len(raw_evidence) > MAX_REPORT_ITEMS:
            raise ValueError("evidence must be a bounded array")
        evidence: list[EvidenceRef] = []
        for item in raw_evidence:
            if not isinstance(item, dict) or set(item) != {"kind", "location", "detail"}:
                raise ValueError("each evidence item needs kind, location, and detail")
            kind = item.get("kind")
            location = item.get("location")
            detail = item.get("detail")
            if kind not in _EVIDENCE_KINDS:
                raise ValueError(f"unsupported evidence kind: {kind!r}")
            if not isinstance(location, str) or not location.strip():
                raise ValueError("evidence location must be non-empty")
            if not isinstance(detail, str) or not detail.strip():
                raise ValueError("evidence detail must be non-empty")
            location = location.strip()
            if kind in ("file", "document"):
                if not _in_path_scope(
                    location, self.request.allowed_paths, self.workspace_root
                ):
                    raise ValueError(f"evidence path is outside allowed scope: {location}")
                if (
                    self.permissions is not None
                    and self.permissions.check_path(location, "read")
                    is not PermissionDecision.ALLOW
                ):
                    raise ValueError(f"evidence path is denied by permissions: {location}")
                try:
                    await self.env.read_file(location)
                except (FileNotFoundError, IsADirectoryError, OSError):
                    raise ValueError(f"evidence path is not readable: {location}") from None
            elif kind == "buffer":
                if location not in buffer_ids:
                    raise ValueError(f"buffer evidence is not declared in buffer_ids: {location}")
            elif kind == "url":
                if not _url_in_scope(location, self.request.allowed_sources):
                    raise ValueError(f"URL evidence is outside allowed sources: {location}")
                if (
                    self.permissions is not None
                    and self.permissions.check_tool("web_fetch")
                    is not PermissionDecision.ALLOW
                ):
                    raise ValueError("URL evidence is denied by permissions")
            elif kind == "command":
                if not is_side_effect_free(location):
                    raise ValueError(f"command evidence is not a safe read: {location}")
                if (
                    self.permissions is not None
                    and self.permissions.check_command(location)
                    is not PermissionDecision.ALLOW
                ):
                    raise ValueError(f"command evidence is denied by permissions: {location}")
                observed = {
                    str(call.arguments.get("command"))
                    for message in self.context.get_messages()
                    for call in (message.tool_calls or [])
                    if call.name == "bash" and call.arguments.get("command")
                }
                if location not in observed:
                    raise ValueError(f"command evidence was not executed: {location}")
            evidence.append(EvidenceRef(kind=kind, location=location, detail=detail.strip()))
        if findings and not evidence:
            raise ValueError("factual findings require evidence")
        report = CollectionReport(
            summary=summary.strip(),
            findings=findings,
            evidence=tuple(evidence),
            unknowns=unknowns,
            buffer_ids=buffer_ids,
            workspace_revision=self.workspace_revision,
            stale=False,
        )
        if len(json.dumps(report.to_dict(), default=str)) > MAX_REPORT_CHARS:
            raise ValueError(f"report exceeds {MAX_REPORT_CHARS} characters")
        return report


class ReadOnlyCollectionShell:
    """A fail-closed shell facade for the optional collection shell capability."""

    name = "bash"
    effect = ToolEffect.READ_ONLY
    description = (
        "Run one conservatively classified read-only inspection command. "
        "Unrecognized programs, writes, backgrounding, and ambiguous composition are denied."
    )

    def __init__(self, delegate: Tool):
        self._delegate = delegate
        self.parameters = delegate.parameters

    async def execute(
        self, arguments: dict, env: Environment, ctx: ToolContext
    ) -> ToolResult:
        command = arguments.get("command") if isinstance(arguments, dict) else None
        if not isinstance(command, str) or not is_side_effect_free(command):
            return ToolResult(
                tool_call_id="",
                content=(
                    "Collection shell denied: command is not a recognized "
                    "side-effect-free inspection."
                ),
                is_error=True,
            )
        return await self._delegate.execute(arguments, env, ctx)


def trusted_collection_effect(tool: Tool) -> ToolEffect:
    """Return an effect only when its declaration has trusted provenance.

    Built-ins are trusted by object identity against the base registry, so a
    project tool cannot impersonate ``read_file`` merely by copying its name or
    declaring ``READ_ONLY``. MCP declarations are trusted only when the client
    marked the remote tool from the user-owned global configuration.
    """
    if isinstance(tool, ReadOnlyCollectionShell):
        return ToolEffect.READ_ONLY

    builtin = builtin_registry().get(tool.name)
    if builtin is tool:
        return tool_effect(tool)

    try:
        from garuda.mcp.client import McpRemoteTool
    except ImportError:
        McpRemoteTool = ()  # type: ignore[assignment,misc]
    if type(tool) is McpRemoteTool and tool.effect_trusted:
        return tool_effect(tool)
    return ToolEffect.UNKNOWN


def build_collection_toolkit(
    tools: list[Tool],
    policy: CollectionPolicy,
    *,
    permissions: PermissionEngine | None = None,
) -> list[Tool]:
    """Apply every collection selector and preserve the caller's tool order."""
    selected: list[Tool] = []
    seen: set[str] = set()
    for candidate in tools:
        name = candidate.name
        if name in seen or name in COLLECTION_HARD_DENY:
            continue
        if name not in policy.profile_tools:
            continue
        if policy.requested_tools is not None and name not in policy.requested_tools:
            continue

        tool: Tool = candidate
        is_readonly_shell = name == "bash" and policy.allow_readonly_shell
        if name == "bash":
            if not policy.allow_readonly_shell:
                continue
            # Never put a project-supplied object behind the trusted wrapper: its
            # execute method could ignore the screened command entirely.
            if builtin_registry().get("bash") is not candidate:
                continue
            tool = ReadOnlyCollectionShell(candidate)
            effect = ToolEffect.READ_ONLY
        else:
            effect = trusted_collection_effect(candidate)
            # Globally trusted MCP tools are exact dynamic additions to the
            # ceiling; ordinary/custom tools must also be named globally.
            is_trusted_mcp = effect != ToolEffect.UNKNOWN and getattr(
                candidate, "effect_trusted", False
            )
            if (
                name not in policy.global_tools
                and not is_trusted_mcp
                and not is_readonly_shell
            ):
                continue

        if effect == ToolEffect.EXTERNAL_READ and not policy.network_enabled:
            continue
        if effect not in (ToolEffect.READ_ONLY, ToolEffect.EXTERNAL_READ):
            continue
        if permissions is not None and (
            permissions.check_tool(name) is not PermissionDecision.ALLOW
        ):
            continue
        selected.append(tool)
        seen.add(name)
    return selected


@dataclass
class CollectionCoordinator:
    """Run trusted, bounded collection children for one parent run."""

    model: object
    policy: ModelCollectionPolicy
    env: Environment
    events: EventStore
    parent_context: ContextManager
    parent_buffer: object | None
    base_tools: list[Tool]
    parent_permissions: PermissionEngine
    reasoning_model: object | None = None
    parent_task: str = ""
    agents_dir: object | None = None
    network_enabled: bool = False
    deadline_monotonic: float | None = None
    parent_mode: str = "interactive"
    background_writer: object | None = None
    isolated_snapshot: bool = False
    ledger: CollectionBudgetLedger = field(init=False)
    _parallel: asyncio.Semaphore = field(init=False, repr=False)
    _tasks: set[asyncio.Task] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self.ledger = CollectionBudgetLedger(self.policy.budget)
        self._parallel = asyncio.Semaphore(self.policy.budget.max_parallel_jobs)

    def _event(self, state: CollectionJobState, **payload: object) -> None:
        self.events.append(EventType.COLLECTION, {"state": state.value, **payload})

    def _buffer_ids(self, request: CollectionRequest) -> set[str]:
        if request.handoff != FORK_BRIEF or self.parent_buffer is None:
            return set()
        valid: list[str] = []
        for buffer_id in self.parent_context.referenced_buffer_ids():
            try:
                self.parent_buffer.read(buffer_id)
            except (AttributeError, KeyError):
                continue
            valid.append(buffer_id)
        return set(valid)

    async def _workspace_revision(self) -> str:
        result = await self.env.execute("git rev-parse HEAD", timeout=10)
        if result.exit_code == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
        return "unavailable"

    def _has_background_writer(self) -> bool:
        value = self.background_writer
        if callable(value):
            try:
                return bool(value())
            except Exception:
                return True
        return bool(value)

    async def _workspace_snapshot(self, request: CollectionRequest) -> WorkspaceSnapshot:
        head = await self._workspace_revision()
        dirty = await self.env.execute(
            "git status --porcelain=v1 -z --untracked-files=all", timeout=10
        )
        dirty_text = dirty.stdout if dirty.exit_code == 0 else "unavailable"
        scopes = request.allowed_paths or (".",)
        quoted = " ".join(shlex.quote(path) for path in scopes)
        scoped = await self.env.execute(
            "git status --porcelain=v1 -z --untracked-files=all -- " + quoted,
            timeout=10,
        )
        scoped_text = scoped.stdout if scoped.exit_code == 0 else "unavailable"
        # Local workspaces get content fingerprints as well as Git state. This
        # catches an untracked file changing in place (its porcelain line stays
        # identical) and keeps staleness useful outside a Git checkout.
        root = Path(self.env.workspace_root)
        if root.is_dir():
            digest = hashlib.sha256()
            for raw in scopes:
                relative = _relative_workspace_path(raw, self.env.workspace_root)
                target = (root / relative).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    digest.update(f"{relative}:outside-workspace".encode())
                    continue
                candidates = (
                    sorted(path for path in target.rglob("*") if path.is_file())
                    if target.is_dir()
                    else [target]
                )
                for path in candidates:
                    if ".git" in path.relative_to(root).parts or not path.is_file():
                        continue
                    try:
                        digest.update(str(path.relative_to(root)).encode())
                        digest.update(path.read_bytes())
                    except OSError:
                        digest.update(b"<unreadable>")
            scoped_text += ":" + digest.hexdigest()
        return WorkspaceSnapshot(
            head=head,
            dirty_fingerprint=hashlib.sha256(dirty_text.encode()).hexdigest(),
            scoped_fingerprint=hashlib.sha256(scoped_text.encode()).hexdigest(),
            background_writer=self._has_background_writer(),
        )

    def _context(
        self,
        request: CollectionRequest,
        config: AgentConfig,
        buffer_ids: set[str],
        buffer_view: CollectionBufferView | None,
        *,
        model: object | None = None,
    ) -> ContextManager:
        from garuda.core.run_state import reserved_output_tokens

        context = ContextManager(
            model=model or self.model,
            max_output_bytes=config.max_output_bytes,
            proactive_threshold=config.proactive_summarize_threshold,
            max_context_tokens=config.max_context_tokens,
            enable_three_step_summary=False,
            task=request.objective,
            buffer=buffer_view,
            reserved_output_tokens=reserved_output_tokens(config),
            safety_margin_tokens=config.context_safety_margin_tokens,
            adaptive_output=config.enable_adaptive_output,
            min_output_bytes=config.min_output_bytes,
        )
        messages: list[Message] = []
        if request.handoff == FORK_BRIEF:
            # ContextManager owns the only bounded state-card handoff renderer.
            # Copy just those rendered messages; never swap the child to the
            # parent's model or carry its raw transcript.
            messages = self.parent_context.fork(mode=FORK_BRIEF).get_messages()
        system = (
            "You are Garuda's bounded collection worker. Inspect only the permitted "
            "paths and sources using the supplied non-mutating tools. Do not edit or "
            "claim to edit the workspace. Finish only by calling submit_collection "
            "with a structured, evidence-backed report. Prose alone is not completion."
        )
        if messages and messages[0].role == Role.SYSTEM:
            messages[0] = Message(role=Role.SYSTEM, content=system)
        else:
            messages.insert(0, Message(role=Role.SYSTEM, content=system))
        request_payload = {
            "objective": request.objective,
            "questions": request.questions,
            "allowed_paths": request.allowed_paths or (".",),
            "allowed_sources": request.allowed_sources,
            "handoff": request.handoff,
            "accessible_buffer_ids": sorted(buffer_ids),
        }
        if request.handoff == FORK_BRIEF:
            request_payload["parent_task"] = self.parent_task[:MAX_OBJECTIVE_CHARS]
        messages.append(
            Message(
                role=Role.USER,
                content="Collection request:\n" + json.dumps(request_payload, indent=2),
            )
        )
        context.seed(messages)
        return context

    def _tools(
        self,
        request: CollectionRequest,
        profile_tools: list[str] | None,
        buffer_ids: set[str],
        *,
        profile_tool_rules: dict[str, str] | None = None,
        profile_path_rules: dict[str, list[str]] | None = None,
        profile_bash_rules: dict[str, list[str]] | None = None,
    ) -> tuple[list[Tool], CollectionPermissionCeiling]:
        child_permissions = PermissionEngine(
            mode="readonly",
            tool_rules=profile_tool_rules,
            path_rules=profile_path_rules,
            bash_rules=profile_bash_rules,
        )
        selected = build_collection_toolkit(
            self.base_tools,
            CollectionPolicy(
                profile_tools=frozenset(
                    DEFAULT_COLLECTION_TOOL_NAMES
                    if profile_tools is None
                    else profile_tools
                ),
                network_enabled=self.network_enabled and bool(request.allowed_sources),
            ),
            # The parent is the authority ceiling at selection time. Runtime
            # execution is screened again by the stricter child engine.
            permissions=self.parent_permissions,
        )
        scoped: list[Tool] = []
        for tool in selected:
            if tool.name == "buffer_list":
                continue
            scoped.append(
                ScopedCollectionTool(
                    tool,
                    request=request,
                    workspace_root=self.env.workspace_root,
                    buffer_ids=buffer_ids,
                )
            )
        scoped.append(SubmitCollectionTool())
        return scoped, CollectionPermissionCeiling(self.parent_permissions, child_permissions)

    def _config(self, request: CollectionRequest, profile: object) -> AgentConfig:
        config = profile.to_agent_config()
        config.mode = "readonly"
        config.permission_mode = "readonly"
        config.max_turns = min(
            request.max_turns or self.policy.budget.max_turns_per_job,
            self.policy.budget.max_turns_per_job,
        )
        config.max_tokens = min(
            request.max_tokens or self.policy.budget.max_tokens_per_job,
            self.policy.budget.max_tokens_per_job,
        )
        config.enable_acceptance_contract = False
        config.enable_three_step_summary = False
        config.enable_verifier = True
        config.force_final_submission = False
        config.bootstrap_environment = False
        config.allowed_tools = None
        return config

    async def _run_attempt(
        self,
        *,
        job_id: str,
        request: CollectionRequest,
        profile: object,
        model: object,
        role: str,
        revision: str,
        initial_buffer_ids: set[str],
    ) -> tuple[CollectionAttempt, CollectionReport | None, str | None]:
        from garuda.core.loop import DefaultAgent

        attempt_id = str(uuid.uuid4())
        config = self._config(request, profile)
        buffer_ids = set(initial_buffer_ids)
        buffer_view = (
            CollectionBufferView(self.parent_buffer, buffer_ids)
            if self.parent_buffer is not None
            else None
        )
        context = self._context(
            request, config, buffer_ids, buffer_view, model=model
        )
        tools, permissions = self._tools(
            request,
            profile.tools,
            buffer_ids,
            profile_tool_rules=profile.tool_rules,
            profile_path_rules=profile.path_rules,
            profile_bash_rules=profile.bash_rules,
        )
        model_name = safe_model_identity(model)
        child_events = EventStore(
            model_binding_role=role,
            call_purpose="collector",
            model_name=model_name,
        )
        persist_child_events(self.events, child_events)
        child_events.append(
            EventType.COLLECTION,
            {
                "state": CollectionJobState.RUNNING.value,
                "job_id": job_id,
                "attempt_id": attempt_id,
                "parent_session_id": self.events.session_id,
                "model_binding_role": role,
                "call_purpose": "collector",
            },
        )
        gate = CollectionCompletionGate(
            context=context,
            request=request,
            env=self.env,
            buffer=buffer_view,
            workspace_root=self.env.workspace_root,
            workspace_revision=revision,
            allowed_buffer_ids=buffer_ids,
            permissions=permissions,
        )
        self._event(
            CollectionJobState.RUNNING,
            job_id=job_id,
            attempt_id=attempt_id,
            child_session_id=child_events.session_id,
            model=model_name,
            model_binding_role=role,
            call_purpose="collector",
        )
        started = time.monotonic()
        result = await DefaultAgent(profile_name="collection").run(
            task=request.objective,
            model=model,
            env=self.env,
            tools=tools,
            config=config,
            events=child_events,
            permissions=permissions,
            context=context,
            buffer=buffer_view,
            terminal_strategy=gate,
            allowed_tool_effects=frozenset(
                {ToolEffect.READ_ONLY, ToolEffect.EXTERNAL_READ}
            ),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        usage = dict(result.metadata.get("usage") or {})
        cost = estimate_cost(model_name, usage)
        attempt = CollectionAttempt(
            attempt_id=attempt_id,
            session_id=child_events.session_id,
            model=model_name,
            turns=result.turns,
            usage=usage,
            elapsed_ms=elapsed_ms,
            model_calls=sum(
                1
                for event in child_events.get_all()
                if event.get("type") == EventType.MODEL_RESPONSE.value
                and not event.get("payload", {}).get("truncated")
            ),
            cost_usd=cost,
            cost_known=cost is not None or not usage,
            model_binding_role=role,
        )
        error = None
        if not result.success or gate.report is None:
            error = "worker did not submit an accepted structured report"
            if result.final_message:
                error += f": {result.final_message}"
        return attempt, gate.report, error

    def _fallback_model(self) -> object | None:
        if self.policy.fallback.for_mode(self.parent_mode) != "reasoning":
            return None
        return self.reasoning_model

    def _reservation_cost(self, model: object, tokens: int) -> float | None:
        # A conservative price probe. Provider-reported cost is unavailable before
        # the call, so admission uses configured/snapshotted rates when known.
        return estimate_cost(
            safe_model_identity(model),
            {"prompt_tokens": max(1, tokens // 2), "completion_tokens": max(1, tokens // 2)},
        )

    async def delegate(self, arguments: object) -> str:
        """Validate, atomically admit, run, and account for one collection job."""
        from garuda.agents.loader import load_profile

        job_id = str(uuid.uuid4())
        last_attempt_id: str | None = None
        child_session_id: str | None = None
        reservation: CollectionReservation | None = None
        attempts: list[CollectionAttempt] = []
        combined_usage: dict[str, int] = {}
        current = asyncio.current_task()
        if current is not None:
            self._tasks.add(current)
        started = time.monotonic()
        self._event(CollectionJobState.REQUESTED, job_id=job_id)
        try:
            request = parse_collection_request(arguments, self.policy)
            for path in request.allowed_paths:
                _relative_workspace_path(path, self.env.workspace_root)
            before = await self._workspace_snapshot(request)
            if (
                before.background_writer
                and self.parent_mode in {"eval", "rigorous"}
                and not self.isolated_snapshot
            ):
                raise ValueError(
                    "strict collection denied while a background workspace writer is active"
                )
            self._event(
                CollectionJobState.VALIDATED,
                job_id=job_id,
                workspace_revision=before.head,
                dirty_fingerprint=before.dirty_fingerprint,
                scoped_fingerprint=before.scoped_fingerprint,
                background_writer=before.background_writer,
            )
            profile = load_profile(self.policy.profile, extra_dir=self.agents_dir)
            per_attempt_tokens = min(
                request.max_tokens or self.policy.budget.max_tokens_per_job,
                self.policy.budget.max_tokens_per_job,
            )
            fallback_model = self._fallback_model()
            attempt_models = [self.model] + ([fallback_model] if fallback_model else [])
            reserved_tokens = per_attempt_tokens * len(attempt_models)
            known_costs = [
                self._reservation_cost(model, per_attempt_tokens)
                for model in attempt_models
            ]
            reservation_cost = (
                round(sum(cost for cost in known_costs if cost is not None), 8)
                if all(cost is not None for cost in known_costs)
                else None
            )
            wall_time_sec = None
            if self.deadline_monotonic is not None:
                remaining = max(0.0, self.deadline_monotonic - time.monotonic())
                wall_time_sec = remaining * self.policy.budget.deadline_fraction
                if wall_time_sec <= 0:
                    raise ValueError("collection deadline budget exhausted")
            reservation = await self.ledger.reserve(
                job_id,
                tokens=reserved_tokens,
                cost_usd=reservation_cost,
                wall_time_sec=wall_time_sec,
            )
            self._event(
                CollectionJobState.BUDGET_RESERVED,
                job_id=job_id,
                tokens=reserved_tokens,
                cost_usd=reservation_cost,
                wall_time_sec=wall_time_sec,
                ledger=self.ledger.snapshot(),
            )

            async def run_job() -> tuple[CollectionReport, CollectionAttempt]:
                initial_buffer_ids = self._buffer_ids(request)
                attempt, report, error = await self._run_attempt(
                    job_id=job_id,
                    request=request,
                    profile=profile,
                    model=self.model,
                    role="collection",
                    revision=before.head,
                    initial_buffer_ids=initial_buffer_ids,
                )
                attempts.append(attempt)
                _merge_usage(combined_usage, attempt.usage)
                if error is None and report is not None:
                    return report, attempt
                if fallback_model is None:
                    raise ValueError(error or "collection worker failed")
                await self.ledger.note_fallback()
                self.events.append(
                    EventType.MODEL_FALLBACK,
                    {
                        "job_id": job_id,
                        "from_model": attempt.model,
                        "to_model": safe_model_identity(fallback_model),
                        "reason": error,
                        "model_binding_role": "reasoning",
                        "call_purpose": "collector",
                    },
                )
                retry, report, retry_error = await self._run_attempt(
                    job_id=job_id,
                    request=request,
                    profile=profile,
                    model=fallback_model,
                    role="reasoning",
                    revision=before.head,
                    initial_buffer_ids=initial_buffer_ids,
                )
                attempts.append(retry)
                _merge_usage(combined_usage, retry.usage)
                if retry_error is not None or report is None:
                    raise ValueError(retry_error or "collection fallback failed")
                return report, retry

            async with self._parallel:
                if wall_time_sec is None:
                    report, final_attempt = await run_job()
                else:
                    async with asyncio.timeout(wall_time_sec):
                        report, final_attempt = await run_job()

            after = await self._workspace_snapshot(request)
            stale_reasons: list[str] = []
            if before.head != after.head:
                stale_reasons.append("git_head_changed")
            if before.dirty_fingerprint != after.dirty_fingerprint:
                stale_reasons.append("workspace_changed")
            if before.scoped_fingerprint != after.scoped_fingerprint:
                stale_reasons.append("scoped_paths_changed")
            if before.background_writer or after.background_writer:
                stale_reasons.append("background_writer_active")
            if stale_reasons:
                report = replace(report, stale=True)
            state = (
                CollectionJobState.COMPLETED_STALE
                if stale_reasons
                else CollectionJobState.COMPLETED
            )
            payload = {
                "job_id": job_id,
                "state": state.value,
                "report": report.to_dict(),
                # Compatibility for existing consumers, plus the lossless list.
                "attempt": asdict(final_attempt),
                "attempts": [asdict(attempt) for attempt in attempts],
                "stale_reasons": stale_reasons,
            }
            rendered = json.dumps(payload, sort_keys=True)
            if len(rendered) > MAX_REPORT_CHARS:
                raise ValueError("bounded collection result exceeded its output ceiling")
            self._event(
                state,
                job_id=job_id,
                attempt_id=final_attempt.attempt_id,
                child_session_id=final_attempt.session_id,
                attempts=len(attempts),
                usage=combined_usage,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                stale_reasons=stale_reasons,
                attempt_metrics=[asdict(attempt) for attempt in attempts],
            )
            return rendered
        except BaseException as exc:
            state = CollectionJobState.CANCELLED if isinstance(
                exc, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit)
            ) else CollectionJobState.FAILED
            if attempts:
                last_attempt_id = attempts[-1].attempt_id
                child_session_id = attempts[-1].session_id
            payload = {
                "job_id": job_id,
                "attempt_id": last_attempt_id,
                "error": f"{type(exc).__name__}: {exc}",
                "attempts": [asdict(attempt) for attempt in attempts],
            }
            if child_session_id is not None:
                payload["child_session_id"] = child_session_id
            self._event(state, **payload)
            raise
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if reservation is not None:
                attempt_costs = [a.cost_usd for a in attempts if a.usage]
                total_cost = (
                    round(sum(attempt_costs), 8)
                    if attempt_costs and all(cost is not None for cost in attempt_costs)
                    else None
                )
                await self.ledger.settle(
                    reservation,
                    usage=combined_usage,
                    cost_usd=total_cost,
                    elapsed_ms=elapsed_ms,
                )
            if current is not None:
                self._tasks.discard(current)

    async def aclose(self) -> None:
        """Cancel and await every live collection job owned by this run."""
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
