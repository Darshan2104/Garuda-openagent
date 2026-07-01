from garuda.model.protocol import Model
from garuda.types import Message, Role


async def summarize_three_step(model: Model, messages: list[Message], task: str) -> str:
    history_text = "\n".join(
        f"{message.role.value}: {message.content[:800]}" for message in messages[-60:]
    )

    summary_response = await model.complete(
        [
            Message(role=Role.SYSTEM, content="Summarize the agent conversation for context compaction."),
            Message(
                role=Role.USER,
                content=f"Task:\n{task}\n\nHistory:\n{history_text}\n\nWrite a concise summary.",
            ),
        ]
    )
    summary = summary_response.content or ""

    question_response = await model.complete(
        [
            Message(role=Role.SYSTEM, content="Identify missing details in a conversation summary."),
            Message(
                role=Role.USER,
                content=f"Task:\n{task}\n\nSummary:\n{summary}\n\nList key unanswered questions.",
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
