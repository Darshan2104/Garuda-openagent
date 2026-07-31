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
    """Per-run configuration.

    The gate fields below are individually switchable for ablation, but you
    normally should not set them one at a time: ``mode`` implies a coherent set
    (see ``garuda.core.modes``). The defaults here are the ``interactive``
    posture — cheapest thing that still verifies — so a bare ``AgentConfig()``
    costs no extra model calls. Pass ``mode="eval"`` for the full gate stack the
    benchmark numbers were produced under.
    """

    max_turns: int = 200
    mode: str = "interactive"
    permission_mode: str = "smart"
    max_output_bytes: int = 30_720
    proactive_summarize_threshold: int = 8000
    # The structural completion gate: task_complete must carry evidence. Cheap —
    # no model call — so it is on in every posture, and off only as an ablation.
    enable_verifier: bool = True
    # LLM-judge verification. An agent that picks its own success criteria grades
    # itself generously, and the judge is the only step that reads the task
    # statement back against observed output. Costs one model call per completion
    # attempt and fails CLOSED (rejects on error/unclear verdict). Off by default
    # because that cost belongs to a graded run, not to every interactive one;
    # `mode="eval"` turns it on.
    enable_llm_verifier: bool = False
    # Refuse completions whose verification commands cannot fail. `cat out.txt`,
    # `ls`, `echo` and `py_compile` exit 0 whether or not the task was solved, so
    # accepting them as proof makes the gate ceremonial.
    require_discriminating_evidence: bool = False
    # Re-run the discriminating checks once and require the same exit codes. Work
    # that verifies on the first run and not the second depends on state that run
    # consumed or created, which a grader starting fresh will not have. Doubles
    # the cost of every verification command.
    require_stable_verification: bool = False
    # Optional domain grader called before the LLM verdict: answer_check(env) ->
    # VerificationResult | None (None = no opinion). Set programmatically by
    # profiles/eval runners; not loadable from YAML.
    answer_check: Any = None
    # Derive checkable acceptance criteria from the task statement at run start,
    # pin them across compaction, and require each to be resolved before a
    # completion is accepted. One extra model call per run.
    enable_acceptance_contract: bool = False
    # Sweep agent-started background processes before verification, so the gate
    # observes the workspace an outside observer would see.
    enable_side_effect_sweep: bool = False
    # Wall-clock budget for the whole run. Turn count alone cannot express "most
    # of my time is gone", which is what matters when one command can block for
    # minutes. None leaves the run bounded only by max_turns.
    deadline_sec: float | None = None
    # Largest share of the *remaining* wall-clock budget any single command may
    # consume. Stops one hung command from spending the rest of the run.
    max_command_budget_fraction: float = 0.5
    # Ceiling on how many read-only tool calls execute concurrently within one
    # model response. Unbounded fan-out is not free: each read can be a
    # `docker exec`, so a response with twenty of them would open twenty
    # containers' worth of work at once and contend with itself.
    max_parallel_reads: int = 8
    # Run contiguous runs of side-effect-free verification commands concurrently
    # at the completion gate. Cannot change a verdict — the allowlist in
    # core/evidence.py admits only non-mutating readers, and evidence stays in the
    # command order the agent supplied — so this is a plain latency field rather
    # than one of the mode gates in core/modes.py.
    parallel_verification: bool = True
    enable_tmux: bool = True
    marker_polling: bool = True
    enable_three_step_summary: bool = True
    condenser: str = "microcompact"
    buffer_tool_output: bool = True
    buffer_threshold_bytes: int = 30_720
    # Context window held back for the model's own response. The window is shared
    # between prompt and completion, so budgeting the prompt against the whole of it
    # declares "10% free" at the exact moment a long answer will not fit. Raised
    # automatically for a reasoning run (see run_state.reserved_output_tokens). Set
    # to 0 to restore the pre-existing behaviour of budgeting against the raw window.
    reserved_output_tokens: int = 16_000
    # Slack on top of the reserve, absorbing what no local count can see: provider
    # framing, a tokenizer that disagrees with ours, an image larger than estimated.
    context_safety_margin_tokens: int = 2_000
    # Re-check the budget immediately before the model call, not only at the top of
    # the turn. Between the two, steering nudges and re-pinned state are appended —
    # so without this the measured prompt is never the prompt that gets sent.
    enable_request_preflight: bool = True
    # Scale the per-tool-result byte budget to how much window is left, instead of
    # spending a flat 30 KiB whether the run is 10% or 95% full. Under pressure this
    # mostly moves output into the buffer (lossless, still greppable) rather than
    # truncating it. False restores the flat max_output_bytes.
    enable_adaptive_output: bool = True
    # Floor for the adaptive budget. Below this a result stops being useful at all,
    # and re-running the tool costs more than the bytes saved.
    min_output_bytes: int = 2_048
    # Maintain the run's goal, todos, modified files, verification results and
    # acceptance criteria as structured state the harness owns, rather than facts
    # the model re-derives into prose on every compaction. Re-pinned as one message
    # instead of three, and handed to the summarizer as givens so the model's own
    # summary covers only findings and dead ends. False restores the prose-only
    # behaviour and the three separate pinned messages.
    enable_working_state_card: bool = True
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
    # Echo the model's own prior reasoning back to it on later turns
    # (thinking_blocks on Anthropic, reasoning_content elsewhere), so a reasoning
    # model does not re-derive its chain of thought from nothing each turn.
    # OFF by default on measurement, not on principle: over 4 terminal-bench-pro
    # tasks on minimax-m2.5 it left total reasoning flat (-1%), spread the same
    # thinking over 29% more turns, and cost 59% more. The mechanism is correct
    # and cheap to carry (the text rides in the cached prefix); what it did not do
    # is buy anything measurable. Turn it on to re-test, ideally with repeated
    # trials — the one reward gain sat on a task known to flip on identical code.
    preserve_reasoning: bool = False
    # Run a fast syntax check after edit/write_file and surface any error to the model.
    post_edit_diagnostics: bool = True
    # After the syntax check passes, run a fast single-file semantic lint (Python via
    # ruff: undefined names / use-before-assign) and surface findings. Best-effort —
    # silent when the linter is absent. Requires post_edit_diagnostics to be on.
    post_edit_lint: bool = True
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
them together in one response — every call in one response runs inside that same turn \
(read-only ones in parallel, the rest in order), so several independent greps and reads cost \
one round-trip, not five. Batching exists to make thorough investigation cheap; it is never a \
reason to investigate less. Split across turns only when a call depends on an earlier result.
2. Read before you edit — read the exact region you will change; prefer the edit tool for changes \
(multi_edit when one file needs several edits at once), and write_file only for new files or a \
full small rewrite.
3. Plan multi-step work — set a north-star objective with update_goal (it survives compaction) \
and track steps with the todo tool; keep both current.
4. Verify before finishing — actually run the checks and read their output; never assume success.
5. Be persistent and adaptive — if a tool errors or returns nothing unexpectedly, change approach \
(different arguments/tool, or bash with an explicit path) instead of repeating it or giving up.
6. Follow the task exactly — honor requested output files, names, and formats; for questions, give \
the final answer in exactly the requested form (exact match matters).
7. Ground every claim in tool evidence; never fabricate results.

When the task is complete AND verified, call task_complete with a clear summary of what you did \
and how you verified it, and state your final answer precisely."""
