from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Image data URIs to send alongside the text (for vision models). Serialized as
    # image_url content blocks; dropped for non-vision models.
    images: list[str] | None = None


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    # Image data URIs a tool wants the (vision-capable) model to actually see.
    images: list[str] = field(default_factory=list)


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    truncated: bool = False


@dataclass
class AgentConfig:
    max_turns: int = 200
    mode: str = "standard"
    permission_mode: str = "smart"
    max_output_bytes: int = 30_720
    proactive_summarize_threshold: int = 8000
    enable_verifier: bool = True
    # LLM-judge verification is opt-in: it costs a model call per completion and,
    # by design, fails CLOSED (rejects on error/unclear verdict), so enabling it
    # is a deliberate choice. Deterministic checks (summary, permission-screened
    # verification commands, answer_check) always run when enable_verifier is on.
    enable_llm_verifier: bool = False
    # Optional domain grader called before the LLM verdict: answer_check(env) ->
    # VerificationResult | None (None = no opinion). Set programmatically by
    # profiles/eval runners; not loadable from YAML.
    answer_check: Any = None
    enable_tmux: bool = True
    marker_polling: bool = True
    enable_three_step_summary: bool = True
    condenser: str = "microcompact"
    buffer_tool_output: bool = True
    buffer_threshold_bytes: int = 30_720
    workspace_kind: str = "local"
    docker_image: str = "ubuntu:22.04"
    docker_host: str | None = None
    sandbox_allow_network: bool = False
    sandbox_require: bool = True
    docker_network: str = "bridge"
    docker_memory: str | None = "2g"
    docker_cpus: str | None = "2"
    mcp_config_path: str | None = None
    system_prompt: str | None = None
    # Extended thinking: reasoning_effort is the cross-provider knob
    # (minimal|low|medium|high); thinking_budget_tokens sets an explicit Anthropic
    # thinking budget. Either enables reasoning; None keeps it off.
    reasoning_effort: str | None = None
    thinking_budget_tokens: int | None = None
    # Run a fast syntax check after edit/write_file and surface any error to the model.
    post_edit_diagnostics: bool = True
    # Probe the environment once at session start (OS, runtimes, package managers,
    # cwd, git) and fold it into the first-turn system prompt so the agent skips
    # redundant discovery turns. Cheap, benchmark-agnostic; off only when the caller
    # wants a fully cold start.
    bootstrap_environment: bool = True
    # Persist shell state (cwd/env/venv) across bash calls via a long-lived session
    # (local env only; opt-in). Off by default to keep bash fully isolated per call.
    persistent_shell: bool = False
    allowed_tools: list[str] | None = None
    max_context_tokens: int = 128_000
    skills: list[str] | None = None
    skills_dirs: list[str] | None = None


@dataclass
class AgentResult:
    success: bool
    final_message: str
    messages: list[Message]
    turns: int
    metadata: dict[str, Any] = field(default_factory=dict)


DEFAULT_SYSTEM_PROMPT = """You are Garuda, a highly capable autonomous agent. You solve tasks — \
coding, research, data, and ops — end to end using tools, and you keep going until the task is \
genuinely done and verified. Do not stop early or hand back partial work.

Operating principles:
1. Understand first — use grep/glob/ls and read_file to inspect the environment before acting; \
never guess a path, value, or fact you can check. When several reads are independent, request \
them together in one response — read-only tools run in parallel, saving turns.
2. Read before you edit — read the exact region you will change; prefer the edit tool for changes \
and write_file only for new files or a full small rewrite.
3. Plan multi-step work with the todo tool and keep it current.
4. Verify before finishing — actually run the checks and read their output; never assume success.
5. Be persistent and adaptive — if a tool errors or returns nothing unexpectedly, change approach \
(different arguments/tool, or bash with an explicit path) instead of repeating it or giving up.
6. Follow the task exactly — honor requested output files, names, and formats; for questions, give \
the final answer in exactly the requested form (exact match matters).
7. Ground every claim in tool evidence; never fabricate results.

When the task is complete AND verified, call task_complete with a clear summary of what you did \
and how you verified it, and state your final answer precisely."""
