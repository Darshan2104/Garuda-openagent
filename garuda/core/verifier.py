import inspect
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from garuda.types import AgentConfig, Message, Role
from garuda.workspace.protocol import Environment

if TYPE_CHECKING:
    from garuda.core.permissions import PermissionEngine
    from garuda.model.protocol import Model

logger = logging.getLogger(__name__)

# Timeout (seconds) for each evidence-gathering git command.
EVIDENCE_COMMAND_TIMEOUT = 10.0

# How many trailing conversation messages are rendered for the LLM verdict.
EVIDENCE_MESSAGE_WINDOW = 15

# Per-message content truncation when rendering conversation evidence.
EVIDENCE_CONTENT_CHARS = 1200

# Ratio above which two numbers in the summary are flagged as possibly contradictory.
CONTRADICTION_RATIO = 10.0

_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _significant_numbers(text: str) -> list[float]:
    values: list[float] = []
    for token in _NUMBER_RE.findall(text or ""):
        try:
            value = abs(float(token.replace(",", "")))
        except ValueError:
            continue
        if value != 0:
            values.append(value)
    return values


def has_numeric_contradiction(text: str, ratio: float = CONTRADICTION_RATIO) -> bool:
    """True if two numbers in ``text`` differ by more than ``ratio``×.

    Used only as a soft hint to the LLM verifier (not a hard gate): it is
    deliberately permissive, so the judge — which understands context like
    '1000 files in 5 seconds' — makes the final call.
    """
    numbers = _significant_numbers(text)
    if len(numbers) < 2:
        return False
    return max(numbers) / min(numbers) > ratio


async def gather_git_evidence(env: Environment) -> str:
    """Collect `git status --short` and `git diff HEAD --stat` from the workspace.

    Returns an empty string when the workspace is not a git repository or when
    git is unavailable. Individual command failures are tolerated silently.
    """
    try:
        probe = await env.execute(
            "git rev-parse --is-inside-work-tree", timeout=EVIDENCE_COMMAND_TIMEOUT
        )
    except Exception:
        return ""
    if probe.exit_code != 0:
        return ""

    sections: list[str] = []
    for command in ("git status --short", "git diff HEAD --stat"):
        try:
            result = await env.execute(command, timeout=EVIDENCE_COMMAND_TIMEOUT)
        except Exception:
            continue
        if result.exit_code == 0:
            output = result.stdout.strip() or "(no output)"
            sections.append(f"$ {command}\n{output}")
    return "\n\n".join(sections)


def render_messages_compact(
    messages: list[Message],
    limit: int = EVIDENCE_MESSAGE_WINDOW,
    max_chars: int = EVIDENCE_CONTENT_CHARS,
) -> str:
    """Render the last `limit` messages compactly: role, truncated content, tool call names."""
    lines: list[str] = []
    for message in messages[-limit:]:
        role = message.role.value if isinstance(message.role, Role) else str(message.role)
        content = " ".join((message.content or "").split())
        if len(content) > max_chars:
            content = content[:max_chars] + "..."
        line = f"[{role}] {content}"
        if message.tool_calls:
            names = ", ".join(call.name for call in message.tool_calls)
            line += f" (tool calls: {names})"
        lines.append(line)
    return "\n".join(lines)


@dataclass
class VerificationResult:
    approved: bool
    checklist: dict[str, bool] = field(default_factory=dict)
    feedback: str | None = None


class CompletionVerifier:
    async def verify_with_commands(
        self,
        task: str,
        summary: str,
        verification_commands: list[str],
        env: Environment,
        config: AgentConfig,
        permissions: "PermissionEngine | None" = None,
        model: "Model | None" = None,
        messages: list[Message] | None = None,
    ) -> VerificationResult:
        if not config.enable_verifier:
            return VerificationResult(approved=True, checklist={"disabled": True})

        checklist = {
            "summary_present": bool(summary.strip()),
            "summary_length": len(summary.strip()) >= 10,
        }

        if not checklist["summary_present"]:
            return VerificationResult(
                approved=False,
                checklist=checklist,
                feedback="Completion rejected: provide a non-empty summary in task_complete.",
            )
        if not checklist["summary_length"]:
            return VerificationResult(
                approved=False,
                checklist=checklist,
                feedback="Completion rejected: summary is too short. Explain what was done and how it was verified.",
            )

        for index, command in enumerate(verification_commands):
            if permissions is not None:
                allowed, denial_reason = await permissions.evaluate_tool_call(
                    "bash", {"command": command}
                )
                if not allowed:
                    checklist[f"verify_cmd_{index}"] = False
                    return VerificationResult(
                        approved=False,
                        checklist=checklist,
                        feedback=(
                            f"Verification command denied by permission policy: {command}"
                            + (f" ({denial_reason})" if denial_reason else "")
                        ),
                    )
            result = await env.execute(command)
            key = f"verify_cmd_{index}"
            checklist[key] = result.exit_code == 0
            if result.exit_code != 0:
                return VerificationResult(
                    approved=False,
                    checklist=checklist,
                    feedback=(
                        f"Verification command failed (exit {result.exit_code}): {command}\n"
                        f"stdout: {result.stdout}\nstderr: {result.stderr}"
                    ),
                )

        # Optional domain grader (research/coding/eval profiles plug in their own
        # correctness check without core knowing the benchmark). A returned
        # VerificationResult is authoritative; None means "no opinion".
        answer_check = getattr(config, "answer_check", None)
        if callable(answer_check):
            try:
                verdict = answer_check(env)
                if inspect.isawaitable(verdict):
                    verdict = await verdict
            except Exception:
                logger.exception("answer_check hook raised; rejecting to fail closed")
                checklist["answer_check_error"] = True
                return VerificationResult(
                    approved=False,
                    checklist=checklist,
                    feedback="Completion rejected: answer_check hook failed.",
                )
            if verdict is not None:
                return verdict

        if model is not None:
            return await self._llm_verdict(
                task=task,
                summary=summary,
                env=env,
                model=model,
                messages=messages,
                checklist=checklist,
            )

        return VerificationResult(approved=True, checklist=checklist)

    async def _llm_verdict(
        self,
        task: str,
        summary: str,
        env: Environment,
        model: "Model",
        messages: list[Message] | None,
        checklist: dict[str, bool],
    ) -> VerificationResult:
        """One LLM call producing a structured APPROVED/REJECTED verdict.

        Fails **closed**: on model errors (after one retry) or an unparseable
        reply, the completion is rejected with feedback, because this is the
        completion gate — a broken verifier must not rubber-stamp wrong answers.
        """
        git_evidence = await gather_git_evidence(env)
        conversation = render_messages_compact(messages or [])

        prompt_parts = [
            f"## Task\n{task}",
            f"## Agent's completion summary\n{summary}",
        ]
        if git_evidence:
            prompt_parts.append(f"## Git evidence from the workspace\n{git_evidence}")
        if conversation:
            prompt_parts.append(f"## Recent conversation (most recent last)\n{conversation}")
        if has_numeric_contradiction(summary):
            prompt_parts.append(
                "## Caution\nThe summary contains numbers that differ by more than 10x. "
                "If these are competing candidate answers, the completion is ambiguous — "
                "REJECT and ask the agent to disambiguate. If they are unrelated (e.g. counts "
                "vs durations), ignore this note."
            )
        prompt_parts.append(
            "## Checklist\n"
            "Evaluate the completion against this checklist:\n"
            "1. Are the task requirements met (correct answer / artifact present)?\n"
            "2. Was the work actually verified (tests or commands run, results observed) — "
            "not just asserted?\n"
            "3. Are units, scale, and magnitude plausible and internally consistent?\n"
            "4. Any signs of premature completion (unfinished steps, unverified claims)?\n\n"
            "Your reply MUST start with exactly APPROVED or REJECTED: <reason>."
        )

        verifier_messages = [
            Message(
                role=Role.SYSTEM,
                content=(
                    "You are a strict task-completion verifier (for coding, research, and ops "
                    "tasks alike). Judge only on the evidence provided. Reply starting with "
                    "exactly APPROVED or REJECTED: <reason>."
                ),
            ),
            Message(role=Role.USER, content="\n\n".join(prompt_parts)),
        ]

        response = None
        for attempt in range(2):  # one retry, then fail closed
            try:
                response = await model.complete(verifier_messages)
                break
            except Exception:
                logger.warning("LLM verifier call failed (attempt %d/2)", attempt + 1, exc_info=True)
        if response is None:
            checklist["llm_verdict_error"] = True
            return VerificationResult(
                approved=False,
                checklist=checklist,
                feedback="Completion rejected: the verifier could not be reached to confirm the work.",
            )

        # Robust parse: strip markdown/whitespace, inspect the first non-empty line.
        text = (response.content or "").strip()
        first_line = ""
        for line in text.splitlines():
            stripped = line.strip().strip("#*_`> ").strip()
            if stripped:
                first_line = stripped
                break
        upper = first_line.upper()
        if upper.startswith("APPROVED"):
            checklist["llm_verdict"] = True
            return VerificationResult(approved=True, checklist=checklist)
        if upper.startswith("REJECTED"):
            checklist["llm_verdict"] = False
            return VerificationResult(
                approved=False,
                checklist=checklist,
                feedback=f"Completion rejected by verifier: {text}",
            )

        logger.warning(
            "LLM verifier reply did not start with APPROVED/REJECTED; rejecting to fail closed. Reply: %.200s",
            text,
        )
        checklist["llm_verdict_unparseable"] = True
        return VerificationResult(
            approved=False,
            checklist=checklist,
            feedback=(
                "Completion rejected: verifier verdict was unclear. Re-state the outcome and how "
                "it was verified, then call task_complete again."
            ),
        )
