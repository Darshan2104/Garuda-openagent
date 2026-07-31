from garuda.model.protocol import Model
from garuda.types import Message, Role

MAX_HISTORY_MESSAGES = 200
MAX_MESSAGE_CHARS = 2000
# Ceiling on the whole rendered transcript. The per-message caps alone allow
# 200 x 2000 = 400 KB (~100k tokens), which can exceed the context window at the
# exact moment summarization is needed to get back under it — the request meant to
# recover from overflow would itself overflow. Newest messages are kept.
MAX_TRANSCRIPT_CHARS = 60_000

_STATE_SYSTEM = (
    "You maintain a compact STRUCTURED STATE of an agent's progress that survives context "
    "compaction. Update the existing state with new information from the transcript, PRESERVING "
    "still-relevant prior facts (do not drop them). Keep exactly these sections:\n"
    "## Objective\n## Files changed\n## Key findings\n## Failed approaches\n## Open TODOs\n"
    "## Current status\n"
    "Be concise and factual. Output ONLY the updated state, nothing else."
)

# Used instead of the above when the harness supplies a working-state card. The
# objective, the file list, the todos and the verification results are then known
# exactly, and asking a model to restate them is both a waste of the call and a
# chance for it to get one wrong. What no ledger can produce is judgement: what was
# learned, what was tried and abandoned, and why the current approach is the one.
_NOTES_SYSTEM = (
    "You maintain the ANALYTICAL NOTES of an agent's run — the part of its memory that "
    "cannot be read off a ledger. A separate working-state card, supplied below, already "
    "records the objective, the files changed, the todo list, the commands run with their "
    "exit codes, and the acceptance criteria. Do NOT restate any of that.\n"
    "Update the existing notes with what the new transcript reveals, PRESERVING still-relevant "
    "prior notes (do not drop them). Keep exactly these sections:\n"
    "## Key findings\n## Failed approaches\n## Rationale\n"
    "Key findings: facts established about the codebase, data, or environment, with how they "
    "were established. Failed approaches: what was tried, why it did not work, and what that "
    "rules out. Rationale: why the current approach was chosen over the alternatives.\n"
    "Be concise and factual. Never invent a result. Output ONLY the updated notes, nothing else."
)


async def summarize_incremental(
    model: Model, prior_state: str, messages: list[Message], task: str, working_state: str = ""
) -> str:
    """Fold new transcript into a running structured state (one model call).

    Unlike a full re-summarize, this preserves prior structured facts and merges,
    so quality doesn't drift over many compactions and the input stays bounded
    (after the first rebuild, only a small window is fed back in).

    With a ``working_state`` card the job narrows to analytical notes — the card
    carries the mechanical facts verbatim, which is both cheaper and more reliable
    than a paraphrase of them.
    """
    transcript = _render_history(messages)
    prior = prior_state.strip() or "(no notes yet — create them)"
    if working_state.strip():
        system = _NOTES_SYSTEM
        preamble = (
            f"Task:\n{task}\n\nWorking state (already recorded — do not restate):\n"
            f"{working_state.strip()}\n\nCurrent notes:\n{prior}\n\n"
        )
        closing = "Return the full updated notes."
    else:
        system = _STATE_SYSTEM
        preamble = f"Task:\n{task}\n\nCurrent state:\n{prior}\n\n"
        closing = "Return the full updated structured state."
    response = await model.complete(
        [
            Message(role=Role.SYSTEM, content=system),
            Message(
                role=Role.USER,
                content=f"{preamble}New transcript to fold in:\n{transcript}\n\n{closing}",
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
    return _tail_within_budget(lines, MAX_TRANSCRIPT_CHARS)


def _tail_within_budget(lines: list[str], budget: int) -> str:
    """Join the newest lines that fit in `budget` characters.

    Trimming from the oldest end keeps the part of the transcript the summary most
    needs — recent work — and notes the drop so the model does not read the result
    as a complete history.
    """
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + 1
        if used + cost > budget and kept:
            dropped = len(lines) - len(kept)
            kept.insert(0, f"[{dropped} earlier message(s) omitted to fit the summary budget]")
            break
        kept.insert(0, line)
        used += cost
    return "\n".join(kept)


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
