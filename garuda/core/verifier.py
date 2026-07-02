from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from garuda.types import AgentConfig
from garuda.workspace.protocol import Environment

if TYPE_CHECKING:
    from garuda.core.permissions import PermissionEngine


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

        return VerificationResult(approved=True, checklist=checklist)
