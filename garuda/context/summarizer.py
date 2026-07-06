from garuda.model.protocol import Model
from garuda.types import Message, Role

MAX_HISTORY_MESSAGES = 200
MAX_MESSAGE_CHARS = 2000

_STATE_SYSTEM = (
    "You maintain a compact STRUCTURED STATE of an agent's progress that survives context "
    "compaction. Update the existing state with new information from the transcript, PRESERVING "
    "still-relevant prior facts (do not drop them). Keep exactly these sections:\n"
    "## Objective\n## Files changed\n## Key findings\n## Failed approaches\n## Open TODOs\n"
    "## Current status\n"
    "Be concise and factual. Output ONLY the updated state, nothing else."
)


async def summarize_incremental(
    model: Model, prior_state: str, messages: list[Message], task: str
) -> str:
    """Fold new transcript into a running structured state (one model call).

    Unlike a full re-summarize, this preserves prior structured facts and merges,
    so quality doesn't drift over many compactions and the input stays bounded
    (after the first rebuild, only a small window is fed back in)."""
    transcript = _render_history(messages)
    prior = prior_state.strip() or "(no state yet — create it)"
    response = await model.complete(
        [
            Message(role=Role.SYSTEM, content=_STATE_SYSTEM),
            Message(
                role=Role.USER,
                content=(
                    f"Task:\n{task}\n\nCurrent state:\n{prior}\n\n"
                    f"New transcript to fold in:\n{transcript}\n\n"
                    "Return the full updated structured state."
                ),
            ),
        ]
    )
    return (response.content or "").strip() or prior_state


def _render_history(messages: list[Message]) -> str:
    lines: list[str] = []
    for message in messages[-MAX_HISTORY_MESSAGES:]:
        if message.role == Role.SYSTEM:
            continue
        content = (message.content or "")[:MAX_MESSAGE_CHARS]
        line = f"{message.role.value}: {content}"
        if message.tool_calls:
            calls = "; ".join(
                f"{call.name}({str(call.arguments)[:300]})" for call in message.tool_calls
            )
            line = f"{line}\n  -> called: {calls}"
        lines.append(line)
    return "\n".join(lines)


async def summarize_three_step(model: Model, messages: list[Message], task: str) -> str:
    history_text = _render_history(messages)

    summary_response = await model.complete(
        [
            Message(role=Role.SYSTEM, content="Summarize the agent conversation for context compaction."),
            Message(
                role=Role.USER,
                content=(
                    f"Task:\n{task}\n\nHistory:\n{history_text}\n\n"
                    "Write a concise summary covering: what has been tried, what worked, "
                    "what failed, current state of files/environment, and what remains to do."
                ),
            ),
        ]
    )
    summary = summary_response.content or ""

    question_response = await model.complete(
        [
            Message(
                role=Role.SYSTEM,
                content=(
                    "You check conversation summaries for completeness. Compare the summary "
                    "against the actual history and list important details the summary omits."
                ),
            ),
            Message(
                role=Role.USER,
                content=(
                    f"Task:\n{task}\n\nHistory:\n{history_text}\n\nSummary:\n{summary}\n\n"
                    "List the key facts from the history that are missing from the summary, "
                    "phrased as questions."
                ),
            ),
        ]
    )
    questions = question_response.content or ""

    answer_response = await model.complete(
        [
            Message(role=Role.SYSTEM, content="Answer questions using the conversation history."),
            Message(
                role=Role.USER,
                content=f"History:\n{history_text}\n\nQuestions:\n{questions}\n\nProvide answers.",
            ),
        ]
    )
    answers = answer_response.content or ""

    return f"Summary:\n{summary}\n\nQ&A:\n{questions}\n{answers}"
