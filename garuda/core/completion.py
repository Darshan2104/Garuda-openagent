"""The completion gate: what happens when the agent calls ``task_complete``.

Split out of ``core/loop.py``. Calling ``task_complete`` does not end a run — this
does, and only if the run can show its work. In order:

1. **Acceptance contract** — derive checkable criteria from the task statement (once,
   lazily) and refuse while any is outstanding.
2. **Side-effect sweep** — kill processes this run started, so the gate observes the
   workspace an outside observer would.
3. **Verification** — evidence, stability, and the LLM verdict, via ``CompletionVerifier``.

Which of these actually run is the mode's decision (``core/modes.py``); in the
default ``interactive`` posture only the structural checks do.

Every gate here has a livelock story behind it, which is why the yield-breaker in
``_check_contract`` exists: a gate that can reject forever is not a gate, it is a
way to spend the whole turn budget re-submitting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from garuda.context.manager import ContextManager
from garuda.core.contract import derive_contract
from garuda.core.events import EventStore, EventType
from garuda.core.permissions import PermissionEngine
from garuda.core.side_effects import SideEffectLedger
from garuda.core.verifier import CompletionGateState, CompletionVerifier
from garuda.model.protocol import Model
from garuda.tools.protocol import Tool
from garuda.types import AgentConfig, Message, Role, ToolCall
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

# Consecutive contract-gate rejections naming the identical outstanding criteria
# before the gate yields and lets verification run. The gate's job is to make the
# agent address a gap; once the same demand has bounced this many times it is no
# longer steering, and repeating it just spends the turn budget.
CONTRACT_REJECT_LIMIT = 3


@dataclass
class CompletionGate:
    """Decides whether a ``task_complete`` call ends the run.

    Holds the per-run gate memory: a rejection has to mean something on the next
    attempt, so the verdict history and the derived contract travel with the run
    rather than being rebuilt per attempt.
    """

    task: str
    config: AgentConfig
    context: ContextManager
    env: Environment
    events: EventStore
    tool_map: dict[str, Tool]
    model: Model | None = None
    permissions: PermissionEngine | None = None
    ledger: SideEffectLedger | None = None
    verifier: CompletionVerifier = field(default_factory=CompletionVerifier)
    gate: CompletionGateState = field(default_factory=CompletionGateState)
    # Acceptance criteria are derived lazily, at the first completion attempt.
    # Deriving them up-front would put a model call in front of every run
    # including those that never finish, and the criteria matter most exactly
    # when the agent believes it is done — arriving with a rejection that
    # names what it has not shown.
    contract: object | None = None
    # Stops a run that skipped or failed derivation from paying for another
    # derive_contract model call on every subsequent completion attempt.
    contract_attempted: bool = False

    async def attempt(self, call: ToolCall) -> tuple[bool, str]:
        """Evaluate one ``task_complete`` call.

        Returns ``(approved, summary_or_feedback)``. On rejection the feedback has
        already been appended to the transcript as the tool result, so the loop only
        has to keep going.
        """
        summary = call.arguments.get("summary", "")
        verification_commands = call.arguments.get("verification_commands") or []
        answer_rationale = call.arguments.get("answer_rationale")

        await self._ensure_contract()
        if await self._check_contract(call, verification_commands):
            return False, ""

        # Clean up before verifying, not after: the gate must observe the same
        # workspace an outside observer would, not one held together by processes
        # this run happens to be keeping alive.
        if self.ledger is not None and self.config.enable_side_effect_sweep:
            await self.ledger.sweep(self.env)
            self.events.append(EventType.SIDE_EFFECTS, self.ledger.summary())
            if self.ledger.swept or self.ledger.listeners_after:
                self.context.append(Message(role=Role.USER, content=self.ledger.render()))

        result = await self.verifier.verify_with_commands(
            task=self.task,
            summary=summary,
            verification_commands=verification_commands,
            env=self.env,
            config=self.config,
            permissions=self.permissions,
            model=self.model if self.config.enable_llm_verifier else None,
            messages=self.context.get_messages(),
            answer_rationale=answer_rationale,
            gate=self.gate,
        )
        self.events.append(
            EventType.VERIFICATION,
            {
                "approved": result.approved,
                "checklist": result.checklist,
                "feedback": result.feedback,
                "evidence": result.evidence,
                "attempt": self.gate.rejections + 1,
            },
        )
        if result.approved:
            self.gate.approvals += 1
            return True, summary

        feedback = result.feedback or "Completion verification failed."
        self.gate.record_rejection(verification_commands, feedback)
        self._reject(call, feedback)
        return False, feedback

    async def _ensure_contract(self) -> None:
        """Derive the acceptance contract on the first completion attempt, once."""
        if self.contract is not None or self.contract_attempted:
            return
        if not self.config.enable_acceptance_contract:
            return
        contract = await derive_contract(task=self.task, model=self.model, events=self.events)
        if contract is not None and contract.criteria:
            # The gate below refuses a completion while any criterion is
            # outstanding, and `contract` is the only tool that can resolve
            # one. Enforcing the criteria without giving the agent that tool
            # makes every task_complete unsatisfiable, so the run burns its
            # whole turn budget re-submitting. Bind it, or drop the contract.
            binder = getattr(self.tool_map.get("contract"), "bind", None)
            if binder is None:
                logger.warning(
                    "enable_acceptance_contract is on but the `contract` tool is not "
                    "available to this profile; skipping contract enforcement. Add "
                    "`contract` to the profile's tools to enable it."
                )
                self.events.append(
                    EventType.CONTRACT,
                    {"action": "skipped", "reason": "contract tool unavailable"},
                )
                contract = None
            else:
                binder(self.events.session_id, contract)
                self.context.append(Message(role=Role.USER, content=contract.render(header=True)))
        self.contract = contract
        self.contract_attempted = True

    async def _check_contract(self, call: ToolCall, verification_commands: list) -> bool:
        """Refuse while acceptance criteria are outstanding. Returns True if rejected.

        Every stated requirement must be resolved before evidence is even run:
        an unaddressed criterion is the cheapest failure to catch and the one
        most often missed, because the agent is reasoning about what it built
        rather than about what it was asked for.
        """
        contract = self.contract
        if contract is None or not contract.outstanding or self.gate.contract_yielded:
            return False
        outstanding = contract.outstanding
        ids = [c.id for c in outstanding]
        # A demand the agent has failed to act on several times running is not
        # steering it any more. Stop repeating it and let the remaining checks
        # (evidence, stability, LLM verdict) decide, rather than spending the
        # rest of the turn budget on an exchange that has stopped moving.
        streak = self.gate.note_contract_rejection(ids)
        if streak > CONTRACT_REJECT_LIMIT:
            self.gate.contract_yielded = True
            logger.warning(
                "Contract gate rejected %d consecutive attempts with the same %d "
                "outstanding criteria; proceeding to verification.",
                streak - 1,
                len(ids),
            )
            self.events.append(
                EventType.CONTRACT,
                {"action": "gate_yield", "outstanding": ids, "attempts": streak - 1},
            )
            self.context.append(
                Message(
                    role=Role.USER,
                    content=(
                        f"{len(ids)} acceptance criterion(s) remain unresolved after "
                        f"{streak - 1} attempts. Proceeding to verification; the summary "
                        "should state plainly which requirements you could not confirm."
                    ),
                )
            )
            return False

        listing = "\n".join(f"  - [{c.id}] {c.text}" for c in outstanding[:12])
        feedback = (
            f"Completion rejected: {len(outstanding)} acceptance criterion(s) from the task "
            f"statement are still unresolved:\n{listing}\n\n"
            "For each one either verify it and mark it verified with the command you ran, "
            "or mark it unverifiable with a reason (use the `contract` tool). Do not mark a "
            "criterion verified without having run something that would have failed if it "
            "were wrong."
        )
        self.events.append(EventType.CONTRACT, {"action": "gate_reject", "outstanding": ids})
        # Not record_rejection: this refusal never looked at the commands, so
        # filing them as rejected evidence would lock out the very resubmission
        # the feedback asks for. See CompletionGateState.record_contract_rejection.
        self.gate.record_contract_rejection(feedback)
        self._reject(call, feedback)
        return True

    def _reject(self, call: ToolCall, feedback: str) -> None:
        self.context.append(
            Message(
                role=Role.TOOL,
                content=feedback,
                name="task_complete",
                tool_call_id=call.id,
            )
        )
