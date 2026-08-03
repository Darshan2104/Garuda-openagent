"""Decidable completion checks derived from the task statement.

The completion gate's last word is the LLM verdict, and a judge can only read the
task statement back against observed output. What it cannot do is check the thing
the *grader* checks. That gap is where the recorded false positives lived: a run
committed work the grader rejected, the judge approved it, and nothing downstream
disagreed.

This module closes the decidable part of that gap. When a task statement says in
so many words "write the result to ``answer.txt``" or "produce ``results.json``",
whether that file exists and parses is not a matter of judgement — so it is not
left to one. Everything else stays the judge's call.

Two properties make this safe to wire in by default:

* **It only ever rejects.** A returned rejection is authoritative; anything else
  is ``None`` — "no opinion" — and the gate proceeds to the LLM verdict exactly as
  before. It cannot approve, so it can never rubber-stamp work the judge would
  have caught. `answer_check` *may* be given an authoritative grader that approves
  (see ``CompletionVerifier``), which is why this one advertises
  ``authoritative = False``.
* **It stays silent unless the task named something.** No named deliverable, no
  opinion — so on the tasks it does not cover it costs one regex pass and nothing
  else.

Requirement extraction is deliberately narrow. A path counts as a deliverable
only when the statement ties it to a *writing* verb, and never when the sentence
hedges ("if you find any", "optionally"). A false rejection here fails a run whose
work was fine, which is strictly worse than the missing check.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass

from garuda.core.verifier import VerificationResult
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

# Extensions whose contents can be checked structurally. Anything else is checked
# for existence only — not even for emptiness, since an empty file is the correct
# answer to "write the matching lines to matches.txt" when nothing matched.
# Guessing at the shape of a `.txt` deliverable is how a checker starts rejecting
# correct answers.
PARSEABLE_SUFFIXES = frozenset({".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".yaml", ".yml"})

# Formats for which an empty file is itself malformed. Only JSON: every other
# parseable suffix has a legitimate empty reading (zero records, null document,
# zero rows), and a task whose correct answer is "nothing matched" writes exactly
# that. See `_check_one`.
EMPTY_IS_INVALID_SUFFIXES = frozenset({".json"})

# How much of a deliverable is read for the parse check. A parser only needs the
# prefix to prove the file is not the format it claims; pulling a multi-GB CSV
# into the gate to check its header is its own failure mode.
MAX_PARSE_BYTES = 256_000

# Verbs that make a named path an *output*. Matched within a short window before
# the path, so "read the log at data/app.log and write a summary to out.txt"
# yields `out.txt` and not `data/app.log`.
_WRITE_VERBS = (
    r"writ(?:e|es|ing|ten)|creat(?:e|es|ing|ed)|sav(?:e|es|ing|ed)|"
    r"stor(?:e|es|ing|ed)|output|outputs|produc(?:e|es|ing|ed)|"
    r"generat(?:e|es|ing|ed)|emit(?:s|ting|ted)?|plac(?:e|es|ing|ed)|put(?:s|ting)?"
)

# Characters between the verb and the path. Deliberately bounded and newline-free:
# an unbounded window turns any write verb anywhere in a long instruction into a
# requirement on every path that follows it.
_GAP = r"[^.\n]{0,80}?"

_REQUIREMENT_RE = re.compile(
    rf"\b(?:{_WRITE_VERBS})\b{_GAP}"
    # The path: bare, backticked, or quoted, absolute or relative. Requires a
    # suffix, so prose like "write a summary" matches nothing.
    #
    # The leading `/` is part of the group and must stay that way. Without it the
    # slash was eaten by the gap, so "write the result to /app/answer.txt" yielded
    # the *relative* `app/answer.txt` — which `resolve_workspace_path` then joins
    # onto the workspace root, looking for `/app/app/answer.txt` in a Harbor run
    # whose workdir is `/app`. The file was exactly where the task asked for it and
    # the check rejected the run for its absence. Terminal-bench statements name
    # absolute paths constantly, so this was the default-on failure mode.
    r"[`'\"]?(?P<path>/?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z][A-Za-z0-9]{0,7})[`'\"]?",
    re.IGNORECASE,
)

# A sentence carrying any of these does not state an unconditional requirement,
# wherever it sits. `if` is the load-bearing one: "if any mismatches are found,
# write them to diff.txt" must not fail a run that correctly found none.
_HEDGE_RE = re.compile(
    r"\b(?:if|unless|should you|in case|optional(?:ly)?|"
    r"may|might|could|can|either|otherwise)\b",
    re.IGNORECASE,
)

# These hedge only what *follows* them, so position decides whether they apply at
# all. Two groups, one rule:
#
# * *Temporal* — "when", "once", "after" are usually ordering notes rather than
#   conditions: "write the total to results.json when you are done" is an
#   unconditional requirement, and reading the word as a condition switched the
#   check off on ordinary phrasing.
# * *Exemplifying* — "e.g." and "for example" introduce an illustration of whatever
#   follows them. Ahead of the verb they replace the requirement ("e.g. write to
#   sample.json" asks for nothing); *behind* it they are showing the shape of a file
#   the task genuinely asked for. `write the totals to /app/out.json, e.g. {"a": 1}`
#   is close to the most common way a well-specified statement is written, so
#   matching these sentence-wide turned the check off on exactly the tasks it has
#   most to say about — landing it on the vague statements and nothing else.
#
# `e.g.` carries no trailing `\b`: a word boundary after the final `.` needs a word
# character next, which "e.g. write to sample.json" does not have.
_LEADING_HEDGE_RE = re.compile(
    r"\b(?:when|whenever|once|after|while|for each|for example)\b|\be\.g\.",
    re.IGNORECASE,
)

# Paths that name the tooling rather than a deliverable. A task statement often
# mentions the file it wants *edited* or the script it wants run; neither is
# something the gate should demand be newly created, and both are already covered
# by the agent's own verification commands.
_IGNORED_NAMES = frozenset(
    {
        "requirements.txt", "setup.py", "pyproject.toml", "package.json", "makefile",
        "dockerfile", "readme.md", "conftest.py", "__init__.py",
    }
)


@dataclass(frozen=True)
class Deliverable:
    """A file the task statement asks for, and the format its name implies."""

    path: str
    suffix: str

    @property
    def parseable(self) -> bool:
        return self.suffix in PARSEABLE_SUFFIXES


def _paragraphs(text: str) -> list[str]:
    """Split on blank lines. A paragraph break resets a carried condition."""
    return [block for block in re.split(r"\n\s*\n+", text or "") if block.strip()]


# Abbreviations whose full stop does not end a sentence. Without these, "e.g. write
# to sample.json" split into "e.g." and "write to sample.json", stranding the hedge
# in a fragment of its own — the same defect as splitting on `:`, from a period
# instead of a colon.
_SENTENCE_BREAK_RE = re.compile(
    r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\betc\.)(?<!\bvs\.)(?<=[.!?;])\s+|\n+"
)


def _sentences(text: str) -> list[str]:
    """Split on sentence and list-item boundaries.

    Hedges scope to a fragment, so where the split falls decides what a condition
    governs. `:` is deliberately **not** a boundary: splitting on it put the
    condition in one fragment and the write verb in the next, so "If a mismatch is
    found: write it to diff.txt" lost its `if` entirely and became an
    unconditional requirement — the exact false rejection the hedge exists to
    prevent, in the markdown shape task statements use most (condition, colon,
    bulleted requirements). A colon does not end a sentence anyway.
    """
    return [part for part in _SENTENCE_BREAK_RE.split(text or "") if part.strip()]


def extract_deliverables(task: str) -> list[Deliverable]:
    """Files the task statement unconditionally asks the agent to produce.

    Empty for the (many) statements that name no output file, which is what keeps
    this free on the tasks it does not cover.
    """
    found: dict[str, Deliverable] = {}
    for paragraph in _paragraphs(task):
        # A condition introducing a list governs the items under it, so it is
        # carried across fragments until a sentence actually ends. Reset per
        # paragraph: a blank line is where a list stops and new prose starts.
        carried_hedge = False
        for sentence in _sentences(paragraph):
            stripped = sentence.strip()
            leading = _LEADING_HEDGE_RE.search(sentence)
            hedged = carried_hedge or bool(_HEDGE_RE.search(sentence))
            # A leading hedge counts for the carry as well as a sentence-wide one.
            # It introduces its list exactly the same way — "When a mismatch is
            # found:" followed by bullets is the same statement as the comma form
            # this file already reads as conditional, and without the marker here
            # the bullets became unconditional requirements. Only the punctuation
            # differs, which is the third variant of that bug in this function.
            if stripped.endswith(":") and (hedged or leading is not None):
                carried_hedge = True
            elif stripped.endswith((".", "!", "?")):
                carried_hedge = False
            if hedged:
                continue
            for match in _REQUIREMENT_RE.finditer(sentence):
                # A temporal word ahead of the write verb is governing it; behind
                # it, it is describing when to do something the task still requires.
                if leading is not None and leading.start() < match.start():
                    continue
                raw = match.group("path")
                path = raw.strip("`'\"")
                # Only a leading `./` — a bare lstrip("./") also ate the leading
                # slash of an absolute path, undoing the fix above.
                if path.startswith("./"):
                    path = path[2:]
                # Matched on the basename so `/app/setup.py` is skipped too.
                if not path or path.rsplit("/", 1)[-1].lower() in _IGNORED_NAMES:
                    continue
                suffix = "." + path.rsplit(".", 1)[-1].lower()
                found.setdefault(path, Deliverable(path=path, suffix=suffix))
    return list(found.values())


def _parse_failure(deliverable: Deliverable, text: str) -> str | None:
    """Why ``text`` is not the format ``deliverable``'s name claims, or None.

    Returns None for anything it cannot judge, including formats where a partial
    read is legitimately incomplete — a truncated JSON prefix is not evidence of
    a malformed file.
    """
    suffix = deliverable.suffix
    truncated = len(text.encode("utf-8", "ignore")) >= MAX_PARSE_BYTES
    if suffix in (".json",):
        if truncated:
            return None
        try:
            json.loads(text)
        except ValueError as exc:
            return f"is not valid JSON ({exc})"
        return None
    if suffix in (".jsonl", ".ndjson"):
        lines = text.splitlines()
        # A truncated read can cut the final line mid-object; judge only whole lines.
        if truncated and lines:
            lines = lines[:-1]
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except ValueError as exc:
                return f"is not valid JSONL — line {number} does not parse ({exc})"
        return None
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:  # pragma: no cover - yaml is a hard dependency
            return None
        if truncated:
            return None
        try:
            yaml.safe_load(text)
        except Exception as exc:
            return f"is not valid YAML ({exc})"
        return None
    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        try:
            list(csv.reader(io.StringIO(text), delimiter=delimiter))
        except csv.Error as exc:
            return f"is not readable as {suffix.lstrip('.').upper()} ({exc})"
        # No row-count check: "zero rows" is a legitimate answer, and a file of
        # empty cells is odd rather than wrong. Only an unreadable file is a defect.
        return None
    return None


@dataclass
class DeliverableCheck:
    """``answer_check`` implementation: the task's named outputs must exist and parse.

    Advisory by construction — see the module docstring. Reads through the
    ``Environment`` so it works the same against a local workspace, a Docker
    container, or a Harbor-provided environment, and treats an unreadable
    workspace as "no opinion" rather than as a failure: a checker that turns an
    infrastructure hiccup into a rejected completion is a worse bug than the one
    it is here to catch.
    """

    task: str
    # False so the gate keeps demanding the agent's own discriminating evidence.
    # This check is a necessary condition, never a sufficient one: a file can
    # exist, parse, and hold the wrong answer.
    authoritative: bool = False

    def __post_init__(self) -> None:
        self.deliverables = extract_deliverables(self.task)

    def __bool__(self) -> bool:
        """False when the task named nothing checkable, so callers can skip wiring it."""
        return bool(self.deliverables)

    async def __call__(self, env: Environment) -> VerificationResult | None:
        if not self.deliverables:
            return None
        checklist: dict[str, bool] = {}
        problems: list[str] = []
        for deliverable in self.deliverables:
            problem = await self._check_one(env, deliverable)
            checklist[f"deliverable:{deliverable.path}"] = problem is None
            if problem is not None:
                problems.append(problem)
        if not problems:
            # Necessary, not sufficient: say nothing and let the judge decide.
            return None
        listing = "\n".join(f"  - {problem}" for problem in problems)
        return VerificationResult(
            approved=False,
            checklist=checklist,
            feedback=(
                "Completion rejected: the task statement names output files that are not "
                f"there or not usable:\n{listing}\n\n"
                "These are requirements from the task itself, not from the verifier. Produce "
                "each file at the stated path in the stated format, check it by reading it "
                "back, then call task_complete again."
            ),
        )

    async def _check_one(self, env: Environment, deliverable: Deliverable) -> str | None:
        """One deliverable's problem, or None if it is present and usable."""
        try:
            content = await env.read_file(deliverable.path)
        except FileNotFoundError:
            return f"`{deliverable.path}` does not exist"
        except IsADirectoryError:
            return f"`{deliverable.path}` is a directory, not a file"
        except Exception:
            # Unreadable for any other reason (permissions, a dead workspace, an
            # environment that cannot express this path): not something to fail a
            # completion over.
            logger.debug("Deliverable check could not read %s", deliverable.path, exc_info=True)
            return None
        if not content.strip():
            # Empty is a defect only where the name promises a structure an empty
            # file cannot be, which is `.json` and nothing else: an empty `.jsonl`
            # is zero records, an empty `.yaml` parses as null, an empty `.csv` is
            # zero rows, and an empty `.txt` is frequently the right answer —
            # "write matching lines to matches.txt" with nothing matching produces
            # exactly this, and rejecting it would fail a correct run.
            if deliverable.suffix not in EMPTY_IS_INVALID_SUFFIXES:
                return None
            return f"`{deliverable.path}` is empty, which is not valid JSON"
        if deliverable.parseable:
            failure = _parse_failure(deliverable, content[:MAX_PARSE_BYTES])
            if failure is not None:
                return f"`{deliverable.path}` {failure}"
        return None


def deliverable_check(task: str) -> DeliverableCheck | None:
    """A ``DeliverableCheck`` for ``task``, or None when it names no deliverables.

    Returning None rather than an inert checker keeps ``config.answer_check`` an
    honest signal: something is there only when it can actually decide something.
    """
    check = DeliverableCheck(task=task)
    return check or None
