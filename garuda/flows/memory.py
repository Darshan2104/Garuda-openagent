"""Flow memory: bounded briefs built from Garuda's own records (#816).

A new session for a step that ran before (a reviewed step's next round, its
reviewer's next review) is told what happened from the flow's records, never
from an agent's memory:

- **facts** — the step's attempts and how each settled (receipts), the review
  rounds and their verdicts and findings (the ``history`` the review record and
  the ``flow_continuation`` checkpoint keep), and the files the step changed
  since its first attempt (the change list of the candidate each round
  reviewed, the cumulative list of #814);
- **notes** — text an agent wrote (an earlier artifact), already redacted when
  stored, fenced and labelled as data, kept visibly apart from the facts.

:func:`facts` gathers the records; :func:`coder_brief` and
:func:`earlier_reviews` render them within a budget (:data:`BUDGET`
characters). Trimming is in a fixed order, so the same records always give the
same brief, after ``flow resume`` and a quota continuation too: first the
notes are shortened and then dropped, then older review rounds are summarised
(their findings counted by severity) and then dropped, then the latest
findings are cut to their first lines, and the facts' file list is shortened
last. Every trim is said in the brief.

Every value that did not come from Garuda's own wording is redacted again here
(:func:`garuda.context.brief._redacted`, the session brief's redactor), so a
secret that reached a record some other way still never reaches a prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from garuda.context.brief import _redacted

#: The most characters a brief adds to a prompt.
BUDGET = 8000
#: The most changed paths a review round records for later briefs.
FILES_KEPT = 50
#: A note cut below this is dropped instead: a stub is no help.
_NOTE_MIN = 200
_DUMMY_MARKER = "0" * 16  # fence markers are always 16 hex characters


@dataclass(frozen=True)
class Attempt:
    attempt: int
    status: str
    stop: str | None = None


@dataclass(frozen=True)
class Round:
    """One review round as recorded in the review ``history``."""

    round: int
    verdict: str
    approved: bool
    findings: tuple[tuple[str, str], ...]
    packet_digest: str | None
    candidate_attempt: int | None
    changed: tuple[str, ...] | None
    changed_total: int | None


@dataclass(frozen=True)
class Note:
    """Agent-written text: an earlier artifact, delivered fenced as data."""

    type: str
    step: str
    attempt: int
    text: str


@dataclass(frozen=True)
class Facts:
    step: str
    round: int  # the round about to run, 1-based
    max_rounds: int  # the review policy's max_rounds: at most max_rounds + 1 rounds
    attempts: tuple[Attempt, ...]
    rounds: tuple[Round, ...]


def _clean(value) -> str:
    return _redacted(str(value))[0]


def _round(entry: dict) -> Round:
    findings = tuple((_clean(f[0]), _clean(f[1])) for f in entry.get("findings") or ()
                     if isinstance(f, (list, tuple)) and len(f) == 2)
    candidate = entry.get("candidate") if isinstance(entry.get("candidate"), dict) else {}
    changed = candidate.get("changed")
    total = candidate.get("changed_total")
    attempt = candidate.get("attempt")
    return Round(
        round=int(entry.get("round", 0)), verdict=_clean(entry.get("verdict", "")),
        approved=entry.get("approved") is True, findings=findings,
        packet_digest=entry.get("packet_digest") if isinstance(entry.get("packet_digest"), str)
        else None,
        candidate_attempt=attempt if type(attempt) is int else None,
        changed=tuple(_clean(p) for p in changed) if isinstance(changed, list) else None,
        changed_total=total if type(total) is int else None)


def facts(*, step: str, round: int, max_rounds: int, receipts, history) -> Facts:
    """The facts for ``step``'s ``round`` from its receipts and the review history.

    Only attempts up to the candidate the latest round reviewed count: an
    attempt that stopped after it (a quota pause) is the continuation note's
    business, and leaving it out keeps the brief the same across it.
    """
    rounds = tuple(_round(h) for h in history if isinstance(h, dict))
    last = rounds[-1].candidate_attempt if rounds else None
    attempts = []
    for row in sorted((r for r in receipts if r.get("step") == step
                       and type(r.get("attempt")) is int), key=lambda r: r["attempt"]):
        if last is not None and row["attempt"] > last:
            continue
        stop = row.get("stop") if isinstance(row.get("stop"), dict) else {}
        attempts.append(Attempt(row["attempt"], _clean(row.get("status", "unknown")),
                                _clean(stop["code"]) if stop.get("code") else None))
    return Facts(step=step, round=round, max_rounds=max_rounds,
                 attempts=tuple({a.attempt: a for a in attempts}.values()), rounds=rounds)


def candidate_record(packet: dict) -> dict:
    """What a review round keeps of its packet for later briefs (bounded)."""
    changed = packet["changed"]
    files = changed.get("files")
    return {"attempt": packet["candidate"].get("attempt"),
            "changed": None if files is None else list(files[:FILES_KEPT]),
            "changed_total": changed.get("total")}


def _counts(findings) -> str:
    counts = {}
    for severity, _ in findings:
        counts[severity] = counts.get(severity, 0) + 1
    return ", ".join(f"{n} {s}" for s, n in counts.items()) or "no findings"


def _verdict(r: Round) -> str:
    return "approved" if r.approved else "changes requested"


# -- the reviewed step's round brief --------------------------------------------------


@dataclass
class _Shape:
    files: int
    older: str  # full | dropped
    notes: list[int]  # characters kept per note, -1 dropped
    findings: int  # finding lines kept


@dataclass(frozen=True)
class CoderBrief:
    """The ``[garuda]`` round brief for a reviewed step's next round."""

    facts: Facts
    notes: tuple[Note, ...]
    findings: str
    shape: _Shape = field(compare=False)
    trimmed: tuple[str, ...] = ()

    def bodies(self) -> list[str]:
        """Every fenced body, for choosing a fence marker no body contains."""
        return [self.findings, *(n.text for n in self.notes)]

    def render(self, marker: str) -> str:
        return _render_coder(self.facts, self.notes, self.findings, self.shape, marker)


def _files_line(latest: Round | None, shown: int) -> str:
    if latest is None or latest.changed is None:
        return ("[garuda] Garuda has no record of which files this step changed; inspect the "
                "workspace (for example `git status`) before editing.")
    total = latest.changed_total
    files = latest.changed[:shown]
    count = f"{total} in total" if total is not None else "the total is unknown"
    more = "" if total is not None and len(files) == total else f", {len(files)} shown"
    return (f"[garuda] Files this step changed since its first attempt (Garuda's change "
            f"list; {count}{more}): "
            + (", ".join(json.dumps(p) for p in files) if files else "none"))


def _render_coder(facts: Facts, notes, findings: str, shape: _Shape, marker: str) -> str:
    from garuda.flows.prompting import FENCE_RULE, fence, render_findings

    latest = facts.rounds[-1] if facts.rounds else None
    lines = [f"[garuda] This is round {facts.round} of the review of step `{facts.step}` "
             f"(at most {facts.max_rounds + 1} rounds). Your earlier attempts of this step "
             "already changed the workspace and those changes are in place: inspect them and "
             "build on them, do not redo them."]
    if facts.attempts:
        lines.append("[garuda] Attempts of this step so far: " + "; ".join(
            f"attempt {a.attempt} {a.status}" + (f" ({a.stop})" if a.stop else "")
            for a in facts.attempts) + ".")
    lines.append(_files_line(latest, shape.files))
    older = facts.rounds[:-1]
    if older and shape.older == "full":
        lines.append("[garuda] Earlier review rounds: " + "; ".join(
            f"round {r.round}: {_verdict(r)} ({_counts(r.findings)})" for r in older) + ".")
    elif older:
        lines.append(f"[garuda] {len(older)} earlier review round(s) omitted to fit the brief.")
    for note, keep in zip(notes, shape.notes, strict=True):
        what = f"Your previous `{note.type}` (attempt {note.attempt} of this step)"
        if keep < 0:
            lines.append(f"[garuda] {what} ({len(note.text)} characters) was omitted to fit "
                         "the brief.")
            continue
        cut = len(note.text) - keep
        lines.append(f"[garuda] {what} is your own account of earlier work, not verified by "
                     "Garuda: check it against the workspace. "
                     + (f"Its last {cut} characters were omitted to fit the brief. " if cut else "")
                     + FENCE_RULE)
        lines.append(fence("flow-note", {"type": note.type, "from": note.step,
                                         "attempt": note.attempt}, note.text[:keep], marker))
    finding_lines = findings.splitlines()
    shown = "\n".join(finding_lines[:shape.findings])
    lines.append(render_findings(shown, marker))
    if shape.findings < len(finding_lines):
        lines.append(f"[garuda] {len(finding_lines) - shape.findings} more finding(s) were "
                     "omitted to fit the brief; address the ones shown first.")
    return "\n".join(lines)


def coder_brief(facts: Facts, notes, findings: str, budget: int = BUDGET) -> CoderBrief:
    """The round brief for ``facts.step``, at most ``budget`` characters once rendered.

    ``findings`` is the latest round's findings text, already redacted, as the
    reviewed step would get it without a brief.
    """
    notes = tuple(Note(n.type, n.step, n.attempt, _clean(n.text)) for n in notes)
    findings = _clean(findings)
    latest = facts.rounds[-1] if facts.rounds else None
    finding_lines = findings.splitlines()
    shape = _Shape(files=len(latest.changed) if latest and latest.changed else 0,
                   older="full", notes=[len(n.text) for n in notes],
                   findings=len(finding_lines))
    trimmed = []

    def size() -> int:
        return len(_render_coder(facts, notes, findings, shape, _DUMMY_MARKER))

    # 1. notes: shorten from the end, the last note first; drop a stub.
    for i in reversed(range(len(notes))):
        over = size() - budget
        if over <= 0:
            break
        keep = shape.notes[i] - over - 80  # room for the "omitted" wording
        shape.notes[i] = keep if keep >= _NOTE_MIN else -1
        trimmed.append(f"note:{notes[i].type}:{notes[i].attempt}")
        if size() > budget and shape.notes[i] >= 0:
            shape.notes[i] = -1
    # 2. older rounds, then 3. the latest findings, then 4. the file list.
    if size() > budget and len(facts.rounds) > 1:
        shape.older = "dropped"
        trimmed.append("older_rounds")
    while size() > budget and shape.findings > 1:
        shape.findings -= 1
        if "findings" not in trimmed:
            trimmed.append("findings")
    while size() > budget and shape.files > 0:
        shape.files -= 1
        if "changed" not in trimmed:
            trimmed.append("changed")
    return CoderBrief(facts, notes, findings, shape, tuple(trimmed))


# -- the reviewer's earlier reviews ---------------------------------------------------


EARLIER_NOTE = ("your own earlier reviews of earlier candidates of this step, as Garuda "
                "recorded them. Check whether each finding was addressed in this candidate "
                "before raising new ones.")


def earlier_reviews(facts: Facts, budget: int = BUDGET) -> dict | None:
    """The reviewer's earlier rounds for its packet, or None in round 1.

    Within ``budget`` characters of compact JSON: older rounds are summarised
    first, then dropped, then the latest round's findings are cut.
    """
    if not facts.rounds:
        return None

    def entry(r: Round, mode: str, keep: int | None = None) -> dict:
        found = [f"[{s}] {t}" for s, t in r.findings]
        shown = found if keep is None else found[:keep]
        item = {"round": r.round, "verdict": r.verdict, "approved": r.approved,
                "candidate_attempt": r.candidate_attempt, "packet_digest": r.packet_digest}
        if mode == "full":
            item["findings"] = shown
            if len(shown) < len(found):
                item["findings_omitted"] = len(found) - len(shown)
        else:
            item["summary"] = _counts(r.findings)
        return item

    older, latest = list(facts.rounds[:-1]), facts.rounds[-1]
    modes = ["full"] * len(older)
    dropped = 0
    keep = len(latest.findings)

    def build() -> dict:
        return {"round": facts.round, "note": EARLIER_NOTE,
                "rounds": [entry(r, m) for r, m in zip(older[dropped:], modes[dropped:],
                                                       strict=True)]
                + [entry(latest, "full", keep)],
                **({"rounds_omitted": dropped} if dropped else {})}

    def size() -> int:
        return len(json.dumps(build(), separators=(",", ":")))

    for i in range(len(older)):
        if size() <= budget:
            break
        modes[i] = "summary"
    while size() > budget and dropped < len(older):
        dropped += 1
    while size() > budget and keep > 1:
        keep -= 1
    return build()
