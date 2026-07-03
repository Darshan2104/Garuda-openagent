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


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


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


DEFAULT_SYSTEM_PROMPT = """You are Garuda, a capable software engineering agent.
You solve tasks by using tools: bash commands, searching (grep/glob/ls), reading files, \
writing files, and precise string-replacement edits.
Think step by step. Use grep/glob/read_file to inspect the environment before making changes.
Prefer the edit tool for modifying existing files; use write_file only to create new files \
or fully rewrite small ones. For multi-step work, track your plan with the todo tool.
When the task is fully complete, call the task_complete tool with a clear summary."""
