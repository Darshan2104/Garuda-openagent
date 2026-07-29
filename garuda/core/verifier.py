import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from garuda.core import evidence
from garuda.types import AgentConfig, Message, Role
from garuda.workspace.health import EnvironmentUnavailableError
from garuda.workspace.protocol import Environment

if TYPE_CHECKING:
    from garuda.core.permissions import PermissionEngine
    from garuda.model.protocol import Model

logger = logging.getLogger(__name__)

# Timeout (seconds) for each evidence-gathering git command.
EVIDENCE_COMMAND_TIMEOUT = 10.0

# Timeout (seconds) for each agent-supplied verification command. Bounded so a
# command that never returns (e.g. a test runner entering watch mode) can't hang
# the completion gate indefinitely.
VERIFICATION_COMMAND_TIMEOUT = 300.0

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


_APPROVE_WORDS = ("approve", "pass", "yes", "true", "ok", "accept")
_REJECT_WORDS = ("reject", "fail", "no", "false", "deny")


def _extract_json_object(text: str) -> dict | None:
    """Best-effort pull a single JSON object out of a model reply.

    Tries the whole string, then a ```json fenced block, then the widest
    ``{...}`` span. Returns ``None`` if nothing parses to a dict.
    """
    candidates: list[str] = [text]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_verdict(text: str) -> tuple[bool | None, str]:
    """Parse a verifier reply into ``(approved, reason)``.

    Prefers a structured JSON object ``{"verdict": "APPROVED"|"REJECTED",
    "reason": "..."}``; falls back to a leading ``APPROVED``/``REJECTED`` token so
    plain-text replies still work. Returns ``(None, "")`` when neither shape is
    present, so the caller can fail closed.
    """
    text = (text or "").strip()
    if not text:
        return None, ""

    obj = _extract_json_object(text)
    if obj is not None and "verdict" in obj:
        verdict = str(obj.get("verdict", "")).strip().lower()
        reason = str(obj.get("reason", "")).strip()
        if verdict.startswith(_APPROVE_WORDS):
            return True, reason
        if verdict.startswith(_REJECT_WORDS):
            return False, reason

    # Plain-text fallback: only the first non-empty (markdown-stripped) line counts.
    for line in text.splitlines():
        stripped = line.strip().strip("#*_`> ").strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith("APPROVED"):
            return True, stripped
        if upper.startswith("REJECTED"):
            return False, stripped
        break
    return None, ""


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
    # Observed output of each verification command, so the judge reasons about
    # what the workspace actually printed rather than the agent's account of it.
    evidence: list[dict] = field(default_factory=list)


@dataclass
class CompletionGateState:
    """Verdict history for one run, so a rejection constrains the next attempt.

    Without this each ``task_complete`` is judged in isolation, and the cheapest
    way past a rejection is to resubmit with the failing check removed.
    """

    rejections: int = 0
    approvals: int = 0
    rejected_command_sets: list[frozenset[str]] = field(default_factory=list)
    last_feedback: str | None = None
    # Consecutive contract rejections naming the exact same outstanding criteria.
    # A gate that keeps returning an identical demand the agent cannot satisfy is
    # not steering it, and the run has no way out but the turn cap.
    contract_reject_streak: int = 0
    last_outstanding: frozenset[str] | None = None
    # Set once the gate has given up on the contract. Latched, so the run reports
    # the stall a single time instead of on every later attempt.
    contract_yielded: bool = False

    def record_rejection(self, commands: list[str], feedback: str | None) -> None:
        """Record a rejection that actually judged the evidence.

        Only for verdicts that examined ``commands`` and found them wanting —
        those are the ones a resubmission must improve on. A rejection made on
        other grounds must use :meth:`record_contract_rejection`.
        """
        self.rejections += 1
        self.rejected_command_sets.append(frozenset(commands))
        self.last_feedback = feedback

    def record_contract_rejection(self, feedback: str | None) -> None:
        """Record a rejection made on acceptance-criteria grounds only.

        Deliberately does **not** add the attempt's commands to
        ``rejected_command_sets``: the contract gate refuses before any evidence
        is run, so it has formed no opinion about those commands. Filing them as
        rejected evidence caused a lockout — the agent was told to resolve its
        criteria, did so, resubmitted the same (perfectly good) commands, and
        ``weaker_than_rejected`` then called that a resubmission. The only escape
        was to invent a verification command it never needed, so runs burned to
        the turn cap with the work already correct on disk.
        """
        self.rejections += 1
        self.last_feedback = feedback

    def note_contract_rejection(self, criterion_ids: list[str]) -> int:
        """Count consecutive rejections demanding the identical criteria.

        Returns the streak length. Any change in the outstanding set means the
        agent is making progress, so the count restarts.
        """
        current = frozenset(criterion_ids)
        if self.last_outstanding is not None and current == self.last_outstanding:
            self.contract_reject_streak += 1
        else:
            self.contract_reject_streak = 1
            self.last_outstanding = current
        return self.contract_reject_streak

    def weaker_than_rejected(self, commands: list[str]) -> bool:
        """True if this attempt offers nothing the rejected attempts did not.

        Equal or narrower evidence after a rejection means the agent resubmitted
        instead of responding to the feedback.
        """
        if not self.rejections:
            return False
        candidate = frozenset(commands)
        return any(candidate <= previous for previous in self.rejected_command_sets)


RESUBMIT_FEEDBACK = (
    "Completion rejected: this is attempt {attempt} and it presents no new evidence.\n"
    "The previous attempt was rejected for this reason:\n{prior}\n\n"
    "Resubmitting the same (or fewer) verification commands cannot change the outcome. "
    "Before calling task_complete again, actually fix the gap and supply at least one "
    "verification command that was not in the rejected attempt."
)

WEAK_EVIDENCE_FEEDBACK = (
    "Completion rejected: none of your verification commands can fail if the work is wrong.\n"
    "{breakdown}\n"
    "A command counts as evidence only when a wrong result would make it exit non-zero. "
    "Run the deliverable and assert on what it produces — for example execute the program "
    "and check its output (`... | grep -q EXPECTED`), diff against expected content "
    "(`diff -u expected.txt actual.txt`), or run the project's own test command. "
    "Then call task_complete again with that command included."
)

NO_EVIDENCE_FEEDBACK = (
    "Completion rejected: task_complete was called with no verification_commands.\n"
    "State how the result was checked by supplying at least one command that executes the "
    "deliverable and exits non-zero if the result is wrong."
)


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
        answer_rationale: str | None = None,
        gate: CompletionGateState | None = None,
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

        # A resubmission that drops the check it just failed is the cheapest way
        # past the gate, so it is refused before anything is executed.
        if gate is not None and gate.weaker_than_rejected(verification_commands):
            checklist["new_evidence"] = False
            return VerificationResult(
                approved=False,
                checklist=checklist,
                feedback=RESUBMIT_FEEDBACK.format(
                    attempt=gate.rejections + 1,
                    prior=(gate.last_feedback or "(no feedback recorded)")[:800],
                ),
            )

        # Evidence screen. Commands that cannot fail are not evidence, so a
        # completion resting entirely on them is rejected before it is trusted.
        # Skipped when an authoritative domain grader is configured: that grader
        # *is* the oracle, and demanding the agent supply its own on top would
        # reject work an external check has already judged.
        has_external_grader = callable(getattr(config, "answer_check", None))
        if config.require_discriminating_evidence and not has_external_grader:
            if not verification_commands:
                checklist["evidence_present"] = False
                return VerificationResult(
                    approved=False, checklist=checklist, feedback=NO_EVIDENCE_FEEDBACK
                )
            screen = evidence.summarize(verification_commands)
            checklist["evidence_discriminating"] = bool(screen["n_discriminating"])
            if not screen["n_discriminating"]:
                breakdown = "\n".join(
                    f"  - `{command}` — {evidence.weakness_reason(command)}"
                    for command in verification_commands
                )
                return VerificationResult(
                    approved=False,
                    checklist=checklist,
                    feedback=WEAK_EVIDENCE_FEEDBACK.format(breakdown=breakdown),
                )

        observed: list[dict] = []
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
                        evidence=observed,
                        feedback=(
                            f"Verification command denied by permission policy: {command}"
                            + (f" ({denial_reason})" if denial_reason else "")
                        ),
                    )
            try:
                result = await env.execute(command, timeout=VERIFICATION_COMMAND_TIMEOUT)
            except EnvironmentUnavailableError:
                # The workspace is gone; the loop aborts the run. Never convert
                # this into a completion verdict of any kind.
                raise
            except Exception as exc:
                checklist[f"verify_cmd_{index}"] = False
                return VerificationResult(
                    approved=False,
                    checklist=checklist,
                    evidence=observed,
                    feedback=(
                        f"Verification command could not be run ({type(exc).__name__}: {exc}): "
                        f"{command}. Provide a command that completes within "
                        f"{int(VERIFICATION_COMMAND_TIMEOUT)}s."
                    ),
                )
            key = f"verify_cmd_{index}"
            checklist[key] = result.exit_code == 0
            observed.append(
                {
                    "command": command,
                    "class": evidence.classify_command(command),
                    "exit_code": result.exit_code,
                    "stdout": (result.stdout or "")[:EVIDENCE_CONTENT_CHARS],
                    "stderr": (result.stderr or "")[:EVIDENCE_CONTENT_CHARS],
                }
            )
            if result.exit_code != 0:
                return VerificationResult(
                    approved=False,
                    checklist=checklist,
                    evidence=observed,
                    feedback=(
                        f"Verification command failed (exit {result.exit_code}): {command}\n"
                        f"stdout: {result.stdout}\nstderr: {result.stderr}"
                    ),
                )

        # Stability: the same checks run twice in a row must agree. A result
        # that holds once and not twice is not a result — it depends on state
        # the first run consumed or created.
        if config.require_stable_verification:
            unstable = await self._recheck_stability(observed, env)
            checklist["verification_stable"] = unstable is None
            if unstable is not None:
                command, first, second = unstable
                return VerificationResult(
                    approved=False,
                    checklist=checklist,
                    evidence=observed,
                    feedback=(
                        f"Completion rejected: verification is not repeatable. `{command}` exited "
                        f"{first} on the first run and {second} when run again immediately after, "
                        "so the result depends on state that one run consumes or creates. Make the "
                        "work idempotent — running it twice from the same starting point must give "
                        "the same outcome — then verify again."
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
            verdict = await self._llm_verdict(
                task=task,
                summary=summary,
                env=env,
                model=model,
                messages=messages,
                checklist=checklist,
                answer_rationale=answer_rationale,
                observed=observed,
            )
            verdict.evidence = observed
            return verdict

        # No judge model wired. The deterministic gate above has already required
        # discriminating evidence and seen it pass, so approving here rests on
        # observed exit codes rather than on the agent's own account.
        return VerificationResult(approved=True, checklist=checklist, evidence=observed)

    async def _recheck_stability(
        self, observed: list[dict], env: Environment
    ) -> tuple[str, int, int] | None:
        """Re-run the discriminating checks once; report the first that disagrees.

        Only discriminating commands are repeated — re-running `cat` proves
        nothing and costs time — and only their exit codes are compared, since
        stdout legitimately varies (timings, ordering, temp paths).
        """
        for entry in observed:
            if entry.get("class") not in (evidence.EXECUTION, evidence.ASSERTION):
                continue
            command = entry["command"]
            try:
                repeat = await env.execute(command, timeout=VERIFICATION_COMMAND_TIMEOUT)
            except EnvironmentUnavailableError:
                raise
            except Exception:
                logger.warning("Stability re-check could not run: %s", command, exc_info=True)
                continue
            if repeat.exit_code != entry["exit_code"]:
                return command, int(entry["exit_code"]), int(repeat.exit_code)
        return None

    async def _llm_verdict(
        self,
        task: str,
        summary: str,
        env: Environment,
        model: "Model",
        messages: list[Message] | None,
        checklist: dict[str, bool],
        answer_rationale: str | None = None,
        observed: list[dict] | None = None,
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
        if answer_rationale:
            prompt_parts.append(f"## Agent's rationale for the chosen answer\n{answer_rationale}")
        if observed:
            # The strongest evidence available: what the workspace printed when
            # the agent's own checks were executed. Judged ahead of the summary,
            # which is the agent's account of the same events.
            rendered = []
            for entry in observed:
                rendered.append(
                    f"$ {entry['command']}\n"
                    f"[class: {entry['class']}, exit: {entry['exit_code']}]\n"
                    f"stdout: {(entry['stdout'] or '(empty)').strip()}\n"
                    f"stderr: {(entry['stderr'] or '(empty)').strip()}"
                )
            prompt_parts.append(
                "## Verification commands actually executed (observed output)\n"
                + "\n\n".join(rendered)
            )
        if git_evidence:
            prompt_parts.append(f"## Git evidence from the workspace\n{git_evidence}")
        if conversation:
            prompt_parts.append(f"## Recent conversation (most recent last)\n{conversation}")
        if has_numeric_contradiction(summary):
            note = (
                "## Caution\nThe summary contains numbers that differ by more than 10x. "
                "If these are competing candidate answers, the completion is ambiguous"
            )
            if answer_rationale:
                note += " — accept only if the rationale above clearly justifies the chosen one."
            else:
                note += (
                    " and no answer_rationale was provided — REJECT and ask the agent to "
                    "disambiguate. If the numbers are unrelated (e.g. counts vs durations), ignore this."
                )
            prompt_parts.append(note)
        prompt_parts.append(
            "## Checklist\n"
            "Evaluate the completion against this checklist:\n"
            "1. Does the observed command output actually demonstrate the task requirements "
            "are met — every requested file, name, format and value from the task statement?\n"
            "2. Was the work verified by evidence that could have failed, rather than by "
            "commands that exit 0 regardless (listing a file, printing content, compiling)?\n"
            "3. Are units, scale, and magnitude plausible and internally consistent?\n"
            "4. Any signs of premature completion (unfinished steps, unverified claims, "
            "requirements in the task that no command checked)?\n\n"
            "Weigh the observed output above the agent's summary: the summary is a claim, "
            "the output is what happened. If a requirement stated in the task has no "
            "corresponding evidence, REJECT and name the requirement.\n\n"
            'Reply with a single JSON object and nothing else:\n'
            '{"verdict": "APPROVED" or "REJECTED", "reason": "<one concise sentence>"}'
        )

        verifier_messages = [
            Message(
                role=Role.SYSTEM,
                content=(
                    "You are a strict task-completion verifier (for coding, research, and ops "
                    "tasks alike). Judge only on the evidence provided. Reply with a JSON object "
                    '{"verdict": "APPROVED" or "REJECTED", "reason": "..."} and nothing else.'
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

        # Prefer a structured JSON verdict; fall back to an APPROVED/REJECTED prefix.
        text = (response.content or "").strip()
        approved, reason = parse_verdict(text)
        if approved is True:
            checklist["llm_verdict"] = True
            return VerificationResult(approved=True, checklist=checklist)
        if approved is False:
            checklist["llm_verdict"] = False
            return VerificationResult(
                approved=False,
                checklist=checklist,
                feedback=f"Completion rejected by verifier: {reason or text}",
            )

        logger.warning(
            "LLM verifier reply was not a parseable verdict; rejecting to fail closed. Reply: %.200s",
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
