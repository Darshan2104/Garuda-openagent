"""The task's stated requirements, as things that can be checked one by one.

Requirements arrive as prose and then live only in the model's head, so "did I
satisfy every literal the task asked for?" is not a question the agent can
mechanically ask itself. The expensive failures are rarely reasoning failures —
they are a correct algorithm writing `out.lz77` where the task said
`out.txt.lz77`, or a fix that works once where the task said "create it if
missing".

The contract turns the task statement into atomic criteria at the start of the
run, keeps them pinned across compaction, and makes the completion gate check
them off. It reads only the task the caller supplied — no test discovery, no
knowledge of any grader.
"""

import json
import logging
from dataclasses import asdict, dataclass, field

from garuda.core.events import EventStore, EventType
from garuda.model.protocol import Model
from garuda.types import Message, Role

logger = logging.getLogger(__name__)

UNVERIFIED = "unverified"
VERIFIED = "verified"
ASSUMED = "assumed"
UNVERIFIABLE = "unverifiable"

_STATUSES = (UNVERIFIED, VERIFIED, ASSUMED, UNVERIFIABLE)

_MARK = {UNVERIFIED: "☐", VERIFIED: "☑", ASSUMED: "≈", UNVERIFIABLE: "⊘"}

MAX_CRITERIA = 20

EXTRACTION_SYSTEM = """You turn a task statement into a checklist of atomic, \
individually checkable acceptance criteria.

Extract only what the task actually states or unambiguously implies. Do not invent \
requirements, do not add best practices, and do not restate the same requirement twice.

Pay particular attention to details that are easy to satisfy incorrectly:
- exact output paths, filenames, and file extensions (including how an extension is \
formed from an input name)
- exact literal strings, formats, encodings, delimiters, and units
- numeric constants, counts, tolerances, and limits
- named functions, entry points, or signatures the task requires to exist
- behavioural invariants ("must be repeatable", "must create the directory if missing", \
"must handle an empty input")

For each criterion set "kind" to one of: path, literal, numeric, format, entrypoint, \
invariant, behaviour.

Set "specified" to false when the task leaves a needed value open (a count, a tolerance, \
a distribution parameter) and the agent will have to choose one. These become assumptions \
the agent must state and sanity-check rather than silently pick.

Where you can, give "check" as a concrete shell command that would exit non-zero if the \
criterion is violated. Use "" when no single command could test it.

Reply with one JSON object and nothing else:
{"criteria": [{"text": "...", "kind": "...", "specified": true, "check": "..."}]}
Emit at most %d criteria, ordered most load-bearing first.""" % MAX_CRITERIA


@dataclass
class Criterion:
    id: str
    text: str
    kind: str = "behaviour"
    check: str = ""
    specified: bool = True
    status: str = UNVERIFIED
    note: str = ""

    def render(self) -> str:
        line = f"  {_MARK.get(self.status, '☐')} [{self.id}] {self.text}"
        if not self.specified and self.status == UNVERIFIED:
            line += "  (value not given by the task — you must choose one and justify it)"
        if self.note:
            line += f"\n        note: {self.note}"
        return line


@dataclass
class AcceptanceContract:
    criteria: list[Criterion] = field(default_factory=list)

    # -- queries -----------------------------------------------------------

    def get(self, criterion_id: str) -> Criterion | None:
        target = (criterion_id or "").strip().lower()
        for criterion in self.criteria:
            if criterion.id.lower() == target:
                return criterion
        return None

    @property
    def outstanding(self) -> list[Criterion]:
        """Criteria that are neither verified nor explicitly written off."""
        return [c for c in self.criteria if c.status == UNVERIFIED]

    @property
    def assumptions(self) -> list[Criterion]:
        return [c for c in self.criteria if c.status == ASSUMED]

    def stats(self) -> dict[str, int]:
        counts = {status: 0 for status in _STATUSES}
        for criterion in self.criteria:
            counts[criterion.status] = counts.get(criterion.status, 0) + 1
        counts["total"] = len(self.criteria)
        return counts

    # -- mutation ----------------------------------------------------------

    def mark(self, criterion_id: str, status: str, note: str = "") -> tuple[bool, str]:
        criterion = self.get(criterion_id)
        if criterion is None:
            known = ", ".join(c.id for c in self.criteria) or "(none)"
            return False, f"No criterion '{criterion_id}'. Known ids: {known}"
        if status not in _STATUSES:
            return False, f"Status must be one of {', '.join(_STATUSES)}"
        if status in (VERIFIED, UNVERIFIABLE, ASSUMED) and not note.strip():
            return False, (
                f"Marking '{criterion_id}' as {status} requires a note saying how you "
                "established it (the command you ran and what it showed)."
            )
        criterion.status = status
        criterion.note = note.strip()[:400]
        return True, f"{criterion_id} → {status}"

    def add(self, text: str, kind: str = "behaviour", check: str = "") -> Criterion:
        criterion = Criterion(
            id=f"c{len(self.criteria) + 1}", text=text.strip(), kind=kind, check=check
        )
        self.criteria.append(criterion)
        return criterion

    # -- rendering ---------------------------------------------------------

    def render(self, header: bool = False) -> str:
        if not self.criteria:
            return ""
        lines: list[str] = []
        if header:
            lines.append(
                "[acceptance criteria — derived from the task statement, retained across "
                "compaction]\n"
                "These are the specific things the task asks for. Before calling task_complete "
                "every one must be resolved: mark it verified once you have run something that "
                "would have failed if it were wrong, or unverifiable with a reason. Use the "
                "`contract` tool to mark them. Criteria the task left open must be marked "
                "assumed with the value you chose and why it is reasonable."
            )
        else:
            lines.append("[acceptance criteria]")
        lines.extend(criterion.render() for criterion in self.criteria)
        counts = self.stats()
        lines.append(
            f"  ({counts[VERIFIED]} verified, {counts[UNVERIFIED]} outstanding, "
            f"{counts[ASSUMED]} assumed, {counts[UNVERIFIABLE]} unverifiable)"
        )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"criteria": [asdict(c) for c in self.criteria]}


def _coerce(payload: dict) -> AcceptanceContract:
    contract = AcceptanceContract()
    raw = payload.get("criteria")
    if not isinstance(raw, list):
        return contract
    for index, item in enumerate(raw[:MAX_CRITERIA], start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        contract.criteria.append(
            Criterion(
                id=f"c{index}",
                text=text[:300],
                kind=str(item.get("kind") or "behaviour")[:24],
                check=str(item.get("check") or "")[:400],
                specified=bool(item.get("specified", True)),
            )
        )
    return contract


def _extract_json(text: str) -> dict | None:
    candidates = [text]
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


async def derive_contract(
    task: str, model: Model, events: EventStore | None = None
) -> AcceptanceContract | None:
    """Extract acceptance criteria from the task statement.

    Returns None on any failure. Extraction is an aid, not a gate on starting
    work: a run that cannot produce a contract must still be able to proceed.
    """
    if not task or not task.strip():
        return None
    messages = [
        Message(role=Role.SYSTEM, content=EXTRACTION_SYSTEM),
        Message(role=Role.USER, content=f"Task statement:\n\n{task}"),
    ]
    try:
        response = await model.complete(messages)
    except Exception:
        logger.warning("Acceptance-criteria extraction failed; continuing without", exc_info=True)
        return None
    payload = _extract_json((response.content or "").strip())
    if payload is None:
        logger.warning("Acceptance-criteria extraction returned no JSON object")
        return None
    contract = _coerce(payload)
    if events is not None:
        events.append(
            EventType.CONTRACT,
            {"action": "derived", "count": len(contract.criteria), "criteria": contract.to_dict()},
        )
    return contract or None
