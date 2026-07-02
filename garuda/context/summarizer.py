from garuda.model.protocol import Model
from garuda.types import Message, Role

MAX_HISTORY_MESSAGES = 200
MAX_MESSAGE_CHARS = 2000


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
