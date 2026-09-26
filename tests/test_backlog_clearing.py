"""The backlog clear-out: one test per residual that BACKLOG.md carried open.

Grouped by the item each set closes, because the value of these is that the item
cannot silently come back:

* deliverable check (`answer_check`) — decidable requirements decided, not judged
* forced final submission — budget exhausted converted into a real attempt
* print-instead-of-assert — named precisely in the rejection
* transcript closure — no unanswered ``tool_calls`` after an accepted completion
* edit snippet offsets — CRLF and end-of-file deletions
* ATIF attribution, dashboard coercion, ablation setup isolation
"""

import json
from pathlib import Path

import pytest

from garuda.core import evidence
from garuda.core.loop import DefaultAgent
from garuda.core.verifier import CompletionVerifier, VerificationResult
from garuda.eval.answer_checks import (
    DeliverableCheck,
    deliverable_check,
    extract_deliverables,
)
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.local import LocalEnvironment

SUMMARY = "A fully detailed completion summary of the work that was done here."


def _complete(commands=None, summary=SUMMARY, call_id="tc"):
    return ModelResponse(
        content=None,
        tool_calls=[
            ToolCall(
                id=call_id,
                name="task_complete",
                arguments={"summary": summary, "verification_commands": commands or []},
            )
        ],
    )


# --- deliverable check: extraction -------------------------------------------


def test_extracts_a_named_output_file():
    found = extract_deliverables("Write the total to results.json when you are done.")
    assert [d.path for d in found] == ["results.json"]


def test_write_verb_binds_the_output_not_the_input():
    found = extract_deliverables(
        "Read the log at data/app.log and write a summary to out.txt"
    )
    assert [d.path for d in found] == ["out.txt"]


def test_conditional_deliverable_is_not_a_requirement():
    """The false-rejection case that matters: a run that correctly finds nothing
    to report must not be failed for the absence of the report."""
    assert extract_deliverables("If you find any mismatches, write them to diff.txt") == []


def test_no_named_output_means_no_opinion():
    assert extract_deliverables("Fix the failing test in the parser.") == []
    assert deliverable_check("Fix the failing test in the parser.") is None


def test_tooling_files_are_not_deliverables():
    assert extract_deliverables("Create requirements.txt entries as needed") == []


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # A temporal clause after the verb is an ordering note, not a condition.
        ("Write the total to results.json when you are done.", ["results.json"]),
        ("Output the sorted list to sorted.txt after deduplicating.", ["sorted.txt"]),
        # The same word ahead of the verb governs it, and is a condition.
        ("When a mismatch is found, write it to diff.txt", []),
        ("Once the build passes, write the version to VERSION.txt", []),
        # Leading `after` silences an ordering note that is a real requirement. That
        # over-conservatism is deliberate — nothing in the surface form separates this
        # from the two above — and it is the limit BACKLOG.md quotes this line for, so
        # a change that starts extracting here needs the backlog entry changed with it.
        ("After you finish, write the version to VERSION.txt", []),
        ("Optionally write debug info to debug.log", []),
        # Several deliverables in one sentence.
        ("Generate summary.json and save the plot to plot.png.", ["summary.json", "plot.png"]),
        # Prose about inputs asks for nothing.
        ("The script reads input.csv and prints results.", []),
    ],
)
def test_requirement_phrasings(statement, expected):
    assert [d.path for d in extract_deliverables(statement)] == expected


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # An absolute path must keep its leading slash. Dropping it yielded a
        # *relative* path, which resolve_workspace_path joins onto the workspace
        # root — so a Harbor run with workdir /app looked for /app/app/answer.txt
        # and rejected a run that had written /app/answer.txt exactly as asked.
        ("Write the result to /app/answer.txt.", ["/app/answer.txt"]),
        ("Save your findings to /workdir/results.json for grading.", ["/workdir/results.json"]),
        ("Create the file `/app/solution.py` with your solution.", ["/app/solution.py"]),
        # A leading ./ is still normalised away; only that, not the root slash.
        ("Write the output to ./out.txt", ["out.txt"]),
        # Ignored names are matched on the basename, so an absolute one is caught.
        ("Create /app/requirements.txt entries as needed", []),
    ],
)
def test_absolute_deliverable_paths_are_preserved(statement, expected):
    assert [d.path for d in extract_deliverables(statement)] == expected


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # A colon is not a sentence end. Splitting on it stranded the condition in
        # one fragment and the write verb in the next, so `if` was thrown away and
        # a conditional deliverable became a hard requirement.
        ("If a mismatch is found: write it to diff.txt", []),
        ("If the input contains errors:\n- write each error to errors.txt", []),
        # The condition carries across every item of the list it introduces.
        (
            "If errors are found:\n- write them to errors.txt\n- write a count to count.txt",
            [],
        ),
        # A blank line ends the list, and what follows is unconditional again.
        (
            "If errors are found:\n- write them to errors.txt\n\nWrite a summary to summary.txt.",
            ["summary.txt"],
        ),
        # A colon that introduces nothing conditional still yields its requirement.
        ("Note: write the total to out.txt", ["out.txt"]),
        # `e.g.` introduces an example, so it hedges — and its full stop must not
        # split the fragment, or the hedge is stranded exactly as above.
        ("e.g. write to sample.json", []),
        ("For example, write to sample.json", []),
        # `i.e.` restates a requirement rather than exemplifying one: still required.
        ("i.e. write the total to out.txt", ["out.txt"]),
    ],
)
def test_conditions_survive_colons_and_abbreviations(statement, expected):
    assert [d.path for d in extract_deliverables(statement)] == expected


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # An example *after* the requirement is illustrating a file the task really
        # asked for. Treating the marker as sentence-wide suppressed the deliverable
        # — landing the check on vague statements and switching it off on exactly the
        # well-specified ones it has most to say about.
        ("Write the report to report.md, e.g. with one row per file.", ["report.md"]),
        ('Write the totals to /app/out.json, e.g. {"a": 1}.', ["/app/out.json"]),
        ("Save the summary to summary.txt, for example a paragraph.", ["summary.txt"]),
        # Ahead of the verb it still replaces the requirement.
        ("e.g. write to sample.json", []),
        ("For example, write to sample.json", []),
    ],
)
def test_examples_hedge_only_what_follows_them(statement, expected):
    assert [d.path for d in extract_deliverables(statement)] == expected


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # A leading hedge introduces its list the same way a sentence-wide one does.
        # The carry keyed only on `_HEDGE_RE`, so these bullets became unconditional
        # requirements — the same bug as the `if:` form above with only the marker
        # changed, and a run that correctly found no mismatch was failed for the
        # absence of the report.
        ("When a mismatch is found:\n- write it to diff.txt", []),
        ("Once the build passes:\n- write the version to VERSION.txt", []),
        ("For each failing test:\n- write the name to failures.txt", []),
        # Exemplifying markers introduce example blocks the same way. This pair
        # regressed when `for example` moved out of the sentence-wide hedge set.
        ('For example:\n- write "hello" to greeting.txt', []),
        ("e.g.:\n- write the header to head.txt", []),
        # And the other direction: a marker *behind* the verb still leaves the
        # requirement standing, even when the fragment ends in a colon that starts
        # an example block. The carry suppresses what follows, not what preceded it.
        ("Write the totals to totals.csv, e.g.:\n- 1,2\n- 3,4", ["totals.csv"]),
        ("Write the report to report.md, for example:\n- one row per file", ["report.md"]),
        # A blank line still ends the carried condition.
        (
            "When errors are found:\n- write them to errors.txt\n\nWrite a summary to summary.txt.",
            ["summary.txt"],
        ),
    ],
)
def test_leading_hedges_carry_across_the_list_they_introduce(statement, expected):
    assert [d.path for d in extract_deliverables(statement)] == expected


async def test_an_empty_txt_deliverable_is_accepted(tmp_path: Path):
    """"Write matching lines to matches.txt" with nothing matching produces an
    empty file, and that is the correct answer — rejecting it fails a correct run."""
    (tmp_path / "matches.txt").write_text("")
    verdict = await deliverable_check("Write matching lines to matches.txt")(
        LocalEnvironment(workspace_root=tmp_path)
    )
    assert verdict is None


async def test_an_empty_json_deliverable_is_still_rejected(tmp_path: Path):
    """Unlike a .txt, an empty file is not valid JSON whatever the answer was."""
    (tmp_path / "out.json").write_text("")
    verdict = await deliverable_check("Write the metrics to out.json")(
        LocalEnvironment(workspace_root=tmp_path)
    )
    assert verdict is not None and "not valid JSON" in verdict.feedback


async def test_an_empty_jsonl_deliverable_is_accepted(tmp_path: Path):
    """Zero records is a legitimate JSONL document."""
    (tmp_path / "rows.jsonl").write_text("")
    verdict = await deliverable_check("Write each row to rows.jsonl")(
        LocalEnvironment(workspace_root=tmp_path)
    )
    assert verdict is None


# --- deliverable check: verdicts ---------------------------------------------


async def test_missing_deliverable_is_rejected(tmp_path: Path):
    check = deliverable_check("Write the answer to answer.txt")
    verdict = await check(LocalEnvironment(workspace_root=tmp_path))
    assert verdict is not None and not verdict.approved
    assert "answer.txt" in verdict.feedback


async def test_malformed_json_deliverable_is_rejected(tmp_path: Path):
    (tmp_path / "results.json") .write_text("{not json,}")
    verdict = await deliverable_check("Save the metrics to results.json")(
        LocalEnvironment(workspace_root=tmp_path)
    )
    assert verdict is not None and "not valid JSON" in verdict.feedback


async def test_present_and_parseable_deliverable_yields_no_opinion(tmp_path: Path):
    """Necessary, not sufficient — so silence, and the judge still decides."""
    (tmp_path / "results.json").write_text('{"total": 5}')
    verdict = await deliverable_check("Save the metrics to results.json")(
        LocalEnvironment(workspace_root=tmp_path)
    )
    assert verdict is None


async def test_unreadable_workspace_is_not_a_rejection(tmp_path: Path):
    class Broken(LocalEnvironment):
        async def read_file(self, path, **kwargs):
            raise PermissionError("nope")

    verdict = await deliverable_check("Write the answer to answer.txt")(
        Broken(workspace_root=tmp_path)
    )
    assert verdict is None


# --- deliverable check: gate integration -------------------------------------


class _FakeEnv:
    """Minimal Environment: every verification command succeeds."""

    async def execute(self, command, timeout=None, **kwargs):
        class R:
            exit_code = 0
            stdout = ""
            stderr = ""

        return R()

    async def read_file(self, path, **kwargs):
        raise FileNotFoundError(path)


async def test_advisory_check_does_not_retire_the_evidence_screen():
    """The bug this closes: presence of *any* answer_check used to skip the
    discriminating-evidence screen, so wiring in an advisory check would have
    traded a real gate for a file-exists check."""
    config = AgentConfig(
        enable_verifier=True,
        require_discriminating_evidence=True,
        answer_check=DeliverableCheck(task="Write the answer to answer.txt"),
    )
    result = await CompletionVerifier().verify_with_commands(
        task="Write the answer to answer.txt",
        summary=SUMMARY,
        verification_commands=["cat answer.txt"],  # inspection only
        env=_FakeEnv(),
        config=config,
    )
    assert not result.approved
    assert result.checklist.get("evidence_discriminating") is False


async def test_authoritative_grader_still_stands_in_for_the_screen():
    def grader(env):
        return None

    grader.authoritative = True
    config = AgentConfig(
        enable_verifier=True,
        require_discriminating_evidence=True,
        answer_check=grader,
    )
    result = await CompletionVerifier().verify_with_commands(
        task="t",
        summary=SUMMARY,
        verification_commands=["cat answer.txt"],
        env=_FakeEnv(),
        config=config,
    )
    assert result.approved  # the screen was skipped, and nothing else objected


async def test_advisory_approval_is_downgraded_to_no_opinion():
    """A hook that says it can only reject must not be able to approve."""

    def overreaching(env):
        return VerificationResult(approved=True, checklist={"hook": True})

    overreaching.authoritative = False
    config = AgentConfig(enable_verifier=True, answer_check=overreaching)
    result = await CompletionVerifier().verify_with_commands(
        task="t", summary=SUMMARY, verification_commands=[], env=_FakeEnv(), config=config
    )
    # Approved, but by the deterministic gate below it — not by the hook's verdict.
    assert result.approved
    assert "hook" not in result.checklist


async def test_advisory_rejection_is_honoured():
    config = AgentConfig(
        enable_verifier=True,
        answer_check=DeliverableCheck(task="Write the answer to answer.txt"),
    )
    result = await CompletionVerifier().verify_with_commands(
        task="Write the answer to answer.txt",
        summary=SUMMARY,
        verification_commands=[],
        env=_FakeEnv(),
        config=config,
    )
    assert not result.approved and "answer.txt" in result.feedback


# --- forced final submission --------------------------------------------------


async def test_budget_exhausted_converts_into_a_completion_attempt(tmp_path: Path):
    """The `bash-tree-diff-sync` shape: turns spent, work done, never submitted."""
    (tmp_path / "out.txt").write_text("done\n")
    env = LocalEnvironment(workspace_root=tmp_path)
    # Two working turns that never call task_complete, then the submission turn.
    working = ModelResponse(
        content=None,
        tool_calls=[ToolCall(id="r", name="read_file", arguments={"path": "out.txt"})],
    )
    model = ScriptModel(responses=[working, working, _complete(["test -f out.txt"])])
    result = await DefaultAgent().run(
        task="t",
        model=model,
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=2, enable_verifier=True, enable_llm_verifier=False),
    )
    assert result.success
    assert result.turns == 2  # the submission exchange is not a working turn


async def test_final_submission_offers_only_task_complete(tmp_path: Path):
    seen: list[list[str]] = []

    class Recording(ScriptModel):
        async def complete(self, messages, tools=None, **kwargs):
            seen.append([t["function"]["name"] for t in (tools or [])])
            return await super().complete(messages, tools=tools, **kwargs)

    working = ModelResponse(content=None, tool_calls=[
        ToolCall(id="r", name="read_file", arguments={"path": "x"})
    ])
    await DefaultAgent().run(
        task="t",
        model=Recording(responses=[working, _complete()]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(max_turns=1, enable_verifier=True),
    )
    assert len(seen) == 2
    assert len(seen[0]) > 1  # the working turn had the full toolset
    assert seen[1] == ["task_complete"]  # the submission turn had exactly one


async def test_final_submission_cannot_pass_on_empty_evidence(tmp_path: Path):
    """A harness-issued commit that carries nothing must not become a pass."""
    working = ModelResponse(content=None, tool_calls=[
        ToolCall(id="r", name="read_file", arguments={"path": "x"})
    ])
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=[working, _complete(commands=[])]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(
            max_turns=1,
            enable_verifier=True,
            require_discriminating_evidence=True,
        ),
    )
    assert not result.success


async def test_final_submission_recovers_from_a_context_overflow(tmp_path: Path):
    """The submission call happens at the largest context the run ever assembles —
    the last turn's preflight ran before its tool results landed, and the prompt is
    appended on top. Calling the model raw meant an overflow became a logged warning
    and the mechanism silently did not fire on long runs, which are exactly the runs
    that exhaust their budget."""
    from garuda.model.protocol import ContextOverflowError

    class Overflowing(ScriptModel):
        def __init__(self, responses):
            super().__init__(responses=responses)
            self.overflowed = False

        async def complete(self, messages, tools=None, **kwargs):
            # Overflow only the submission call (the one with a restricted schema).
            if tools and len(tools) == 1 and not self.overflowed:
                self.overflowed = True
                raise ContextOverflowError("prompt too long")
            return await super().complete(messages, tools=tools, **kwargs)

    (tmp_path / "out.txt").write_text("x" * 4000 + "\n")
    # Enough turns that force_compact has something to drop; a two-message history
    # cannot shrink, and would legitimately re-raise.
    working = ModelResponse(
        content="thinking about it at some length " * 20,
        tool_calls=[ToolCall(id="r", name="read_file", arguments={"path": "out.txt"})],
    )
    # force_compact runs the condenser, which makes a summarizer call of its own —
    # so the queue has to feed it before the retried submission.
    summarizer = ModelResponse(content="## Objective\nt\n## Files changed\nnone\n", tool_calls=[])
    model = Overflowing(
        responses=[working] * 6 + [summarizer, _complete(["test -f out.txt"])]
    )
    result = await DefaultAgent().run(
        task="t",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(max_turns=6, enable_verifier=True),
    )
    assert model.overflowed  # the overflow happened on the submission call
    recovered = [
        event["payload"].get("recovered")
        for event in result.metadata["events"]
        if event["type"] == "summarization"
        and event["payload"].get("reason") == "context_overflow"
    ]
    assert recovered == [True]  # it force-compacted rather than giving up
    assert result.success  # and the retried submission was accepted


async def test_final_submission_is_accounted_in_turn_metrics(tmp_path: Path):
    """Its tokens reach accumulate_usage, so a turn_metrics record has to describe
    them or the rollup is short one call on exactly the runs that used this."""
    working = ModelResponse(content=None, tool_calls=[
        ToolCall(id="r", name="read_file", arguments={"path": "x"})
    ])
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=[working, _complete()]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(max_turns=1, enable_verifier=True),
    )
    records = [
        event["payload"]
        for event in result.metadata["events"]
        if event["type"] == "turn_metrics"
    ]
    labels = [r.get("label") for r in records]
    assert "final_submission" in labels
    # An ordinary turn's payload is unchanged — no stray label key.
    assert any("label" not in r for r in records)

    # And the rollup agrees with the reported turn count. Counting the submission
    # record as a turn moved the same reconciliation mismatch the label exists to
    # close from tokens to turn count.
    metrics = result.metadata["metrics"]
    assert metrics["turns"] == result.turns
    # Its cost stays visible without being counted as a turn.
    assert metrics["extra_calls"] == 1
    assert metrics["extra_labels"] == ["final_submission"]
    assert metrics["model_ms_total"] >= metrics["extra_model_ms"]


def test_rollup_omits_labelled_records_from_the_latency_distribution():
    """A non-working call must not dilute the per-turn p50/p95, which is the stated
    reason for measuring per turn rather than per run."""
    from garuda.core.metrics import RunMetrics

    metrics = RunMetrics()
    for turn in (1, 2, 3):
        metrics.open_turn(turn).model_ms = 100.0
    metrics.open_turn(3, label="final_submission").model_ms = 9000.0
    summary = metrics.summary()
    assert summary["turns"] == 3
    assert summary["model_ms_p50"] == 100.0
    assert summary["model_ms_p95"] == 100.0  # the 9s outlier is not a turn
    assert summary["model_ms_mean"] == 100.0
    assert summary["model_ms_total"] == 9300.0  # but its time is still in the total
    assert summary["extra_calls"] == 1 and summary["extra_model_ms"] == 9000.0


def test_every_rollup_total_includes_labelled_records():
    """Totals include labelled records; only the distributions exclude them. A total
    sourced from the working-turn list would drop a labelled record's cost while its
    sibling total still reported it — zero impact while the submission call runs no
    tools, and a silent hole the moment one does."""
    from garuda.core.metrics import RunMetrics

    metrics = RunMetrics()
    turn = metrics.open_turn(1)
    turn.model_ms, turn.tool_ms_total, turn.tool_wall_ms = 100.0, 50.0, 40.0
    turn.tool_calls, turn.prompt_tokens, turn.compaction_ms = 2, 10, 5.0
    # A labelled record that did use tools — the case that would have vanished.
    labelled = metrics.open_turn(1, label="final_submission")
    labelled.model_ms, labelled.tool_ms_total, labelled.tool_wall_ms = 900.0, 70.0, 60.0
    labelled.tool_calls, labelled.prompt_tokens, labelled.compaction_ms = 3, 20, 7.0

    summary = metrics.summary()
    assert summary["tool_ms_total"] == 120.0  # 50 + 70, not 50
    assert summary["tool_wall_ms_total"] == 100.0
    assert summary["model_ms_total"] == 1000.0
    assert summary["tool_calls"] == 5
    assert summary["prompt_tokens"] == 30
    assert summary["compaction_ms_total"] == 12.0
    # The distributions still describe working turns only.
    assert summary["turns"] == 1
    assert summary["tool_ms_p95"] == 50.0


def test_rollup_of_an_ordinary_run_keeps_its_exact_keys():
    from garuda.core.metrics import RunMetrics

    metrics = RunMetrics()
    metrics.open_turn(1).model_ms = 10.0
    summary = metrics.summary()
    assert "extra_calls" not in summary
    assert "extra_model_ms" not in summary
    assert "extra_labels" not in summary


async def test_final_submission_without_a_submission_leaves_no_open_calls(tmp_path: Path):
    """A response naming some other tool still lands an assistant tool_calls block
    in the transcript; leaving it unanswered is the bug answer_open_calls fixes."""
    working = ModelResponse(content=None, tool_calls=[
        ToolCall(id="r", name="read_file", arguments={"path": "x"})
    ])
    # The submission turn answers with a hallucinated tool instead of task_complete.
    hallucinated = ModelResponse(
        content="I think I am done.",
        tool_calls=[ToolCall(id="ghost", name="not_a_real_tool", arguments={})],
    )
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=[working, hallucinated]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(max_turns=1, enable_verifier=True),
    )
    assert not result.success
    assert _unanswered(result.messages) == []


async def test_rejected_final_submission_leaves_no_open_calls(tmp_path: Path):
    working = ModelResponse(content=None, tool_calls=[
        ToolCall(id="r", name="read_file", arguments={"path": "x"})
    ])
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=[working, _complete(commands=[])]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(
            max_turns=1, enable_verifier=True, require_discriminating_evidence=True
        ),
    )
    assert not result.success
    assert _unanswered(result.messages) == []


async def test_final_submission_can_be_switched_off(tmp_path: Path):
    calls: list[int] = []

    class Counting(ScriptModel):
        async def complete(self, messages, tools=None, **kwargs):
            calls.append(1)
            return await super().complete(messages, tools=tools, **kwargs)

    working = ModelResponse(content=None, tool_calls=[
        ToolCall(id="r", name="read_file", arguments={"path": "x"})
    ])
    await DefaultAgent().run(
        task="t",
        model=Counting(responses=[working, _complete()]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(max_turns=1, enable_verifier=True, force_final_submission=False),
    )
    assert len(calls) == 1


# --- print-instead-of-assert --------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        'python -c "print(all(x > 0 for x in vals))"',
        'python3 -c "import json; print(json.load(open(\'o.json\'))[\'n\'] == 42)"',
        'python -c "print(isinstance(model, Booster))"',
    ],
)
def test_printed_predicate_is_named_in_the_feedback(command):
    assert evidence.classify_command(command) == evidence.SYNTAX
    assert evidence.prints_a_predicate(command)
    assert "throws the answer away by printing it" in evidence.weakness_reason(command)


@pytest.mark.parametrize(
    "command",
    [
        'python -c "import pickle; m=pickle.load(open(\'m.pkl\')); print(m.shape)"',
        'python -c "print(\'value in range\')"',
    ],
)
def test_plain_prints_keep_the_general_explanation(command):
    assert not evidence.prints_a_predicate(command)
    assert "throws the answer away" not in evidence.weakness_reason(command)


def test_asserting_the_same_predicate_is_evidence():
    command = 'python -c "assert all(x > 0 for x in vals)"'
    assert evidence.classify_command(command) == evidence.ASSERTION
    assert evidence.is_discriminating(command)


# --- transcript closure -------------------------------------------------------


async def test_accepted_completion_answers_its_own_call(tmp_path: Path):
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=[_complete(call_id="solo")]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(max_turns=3, enable_verifier=True),
    )
    assert result.success
    assert _unanswered(result.messages) == []


async def test_completion_alongside_siblings_answers_every_call(tmp_path: Path):
    """A response mixing task_complete with other calls used to return with the
    whole tool_calls block unanswered — a transcript no provider will accept back."""
    (tmp_path / "a.txt").write_text("a")
    response = ModelResponse(
        content=None,
        tool_calls=[
            ToolCall(id="tc", name="task_complete", arguments={"summary": SUMMARY}),
            ToolCall(id="sib", name="read_file", arguments={"path": "a.txt"}),
        ],
    )
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=[response]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(max_turns=3, enable_verifier=True),
    )
    assert result.success
    assert _unanswered(result.messages) == []


async def test_a_dead_workspace_mid_response_leaves_no_open_calls(tmp_path: Path):
    """The more reachable abort path: a tool raises partway through a response, so
    every call after it in the same response never runs. Those were left unanswered
    in the transcript the result hands out."""
    from garuda.workspace.health import EnvironmentUnavailableError

    class Dying(LocalEnvironment):
        async def execute(self, command, **kwargs):
            raise EnvironmentUnavailableError(reason="container gone", detail="probe failed")

    response = ModelResponse(
        content=None,
        tool_calls=[
            ToolCall(id="dies", name="bash", arguments={"command": "ls"}),
            ToolCall(id="never", name="bash", arguments={"command": "echo later"}),
        ],
    )
    result = await DefaultAgent().run(
        task="t",
        model=ScriptModel(responses=[response]),
        env=Dying(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(max_turns=2, enable_verifier=True, permission_mode="yolo"),
    )
    assert not result.success
    assert "unavailable" in result.final_message
    assert _unanswered(result.messages) == []


def _unanswered(messages) -> list[str]:
    """Ids of tool calls with no matching tool-result message."""
    answered = {m.tool_call_id for m in messages if m.role == Role.TOOL and m.tool_call_id}
    return [
        call.id
        for message in messages
        for call in (message.tool_calls or [])
        if call.id not in answered
    ]


# --- subagent handoff anchors on the subagent's own task ----------------------


def test_brief_handoff_retargets_the_task(tmp_path: Path):
    """A `brief` fork inherits the parent's working-state card but must anchor its
    own summaries to its own assignment — the parent's task string leaking through
    is how a subagent's compaction ends up summarizing toward the wrong goal. The
    `full` path is covered by test_backlog_loop_fixes::test_seed_preserves_explicit_task;
    this is the fork+set_task path, which had no test."""
    from garuda.context.manager import FORK_BRIEF, ContextManager
    from garuda.core.events import EventStore
    from garuda.core.subagent import SubagentRunner
    from garuda.types import Message

    model = ScriptModel(responses=[])
    parent = ContextManager(model=model, task="PARENT TASK")
    parent.seed([Message(role=Role.USER, content="PARENT TASK")])
    runner = SubagentRunner(
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        events=EventStore(),
        parent_context=parent,
    )
    child = runner._handoff_context(FORK_BRIEF, "explore", "CHILD TASK", AgentConfig())
    assert child is not None
    assert child._task == "CHILD TASK"
    assert parent._task == "PARENT TASK"  # and the parent is untouched


# --- edit snippet offsets -----------------------------------------------------


def test_snippet_lands_on_the_edit_site_in_a_crlf_file():
    """The drift is cumulative, so the file has to be long enough to show it: at
    line 50 of a CRLF file the old one-char-per-newline walk pointed seven lines
    past the edit, well outside the two lines of context it renders.

    Unit-level on purpose. ``LocalEnvironment.read_file`` goes through
    ``Path.read_text``, whose universal-newline translation hands the tool ``\\n``
    regardless — so CRLF content only reaches this code from an Environment that
    preserves it (a container ``cat``), which is exactly why the bug survived.
    """
    from garuda.tools.edit import _first_diff, _snippet_around

    before = "\r\n".join(f"line{i}" for i in range(60)) + "\r\n"
    after = before.replace("line50", "EDITED")
    snippet = _snippet_around(after, _first_diff(before, after))
    assert "EDITED" in snippet


def test_snippet_lands_on_the_edit_site_in_an_lf_file():
    from garuda.tools.edit import _first_diff, _snippet_around

    before = "\n".join(f"line{i}" for i in range(30)) + "\n"
    after = before.replace("line20", "EDITED")
    assert "EDITED" in _snippet_around(after, _first_diff(before, after))


def test_snippet_survives_a_deletion_at_end_of_file():
    from garuda.tools.edit import _first_diff, _snippet_around

    before = "keep1\nkeep2\ndrop\n"
    after = "keep1\nkeep2\n"
    snippet = _snippet_around(after, _first_diff(before, after))
    assert "keep2" in snippet and "drop" not in snippet


def test_snippet_handles_a_file_with_no_trailing_newline():
    from garuda.tools.edit import _snippet_around

    assert "c" in _snippet_around("a\nb\nc", 4)


async def test_edit_tool_reports_the_snippet_around_the_edit(tmp_path: Path):
    from garuda.tools.edit import EditTool
    from garuda.tools.protocol import ToolContext

    target = tmp_path / "f.txt"
    target.write_text("\n".join(f"line{i}" for i in range(60)) + "\n")
    result = await EditTool().execute(
        {"path": "f.txt", "old_string": "line50", "new_string": "CHANGED"},
        LocalEnvironment(workspace_root=tmp_path),
        ToolContext(session_id="s", post_edit_diagnostics=False, post_edit_lint=False),
    )
    assert not result.is_error
    assert "CHANGED" in result.content


# --- ATIF attribution ---------------------------------------------------------


def test_repeated_tool_results_attribute_to_distinct_calls():
    from garuda.eval.atif_export import _append_tool_result

    step = {
        "tool_calls": [
            {"tool_call_id": "c1", "function_name": "read_file", "arguments": {}},
            {"tool_call_id": "c2", "function_name": "read_file", "arguments": {}},
        ]
    }
    for content in ("first", "second"):
        _append_tool_result(step, tool_name="read_file", content=content, is_error=False)
    assert [r["source_call_id"] for r in step["observation"]["results"]] == ["c1", "c2"]


def test_more_results_than_calls_omits_attribution():
    """When every call is claimed there is no honest answer, so the key is omitted
    rather than pointing at a call that already has its result. Consumers must not
    assume `source_call_id` is always present — pinned here because it is a shape
    change, and no attribution beats wrong attribution."""
    from garuda.eval.atif_export import _append_tool_result

    step = {"tool_calls": [{"tool_call_id": "c1", "function_name": "read_file", "arguments": {}}]}
    for content in ("first", "extra"):
        _append_tool_result(step, tool_name="read_file", content=content, is_error=False)
    results = step["observation"]["results"]
    assert results[0]["source_call_id"] == "c1"
    assert "source_call_id" not in results[1]


def test_exact_id_still_wins_over_order():
    from garuda.eval.atif_export import _append_tool_result

    step = {
        "tool_calls": [
            {"tool_call_id": "c1", "function_name": "read_file", "arguments": {}},
            {"tool_call_id": "c2", "function_name": "read_file", "arguments": {}},
        ]
    }
    _append_tool_result(
        step, tool_name="read_file", tool_call_id="c2", content="out-of-order", is_error=False
    )
    _append_tool_result(step, tool_name="read_file", content="the other", is_error=False)
    assert [r["source_call_id"] for r in step["observation"]["results"]] == ["c2", "c1"]


# --- dashboard coercion -------------------------------------------------------


def test_dashboard_survives_a_non_numeric_metric(tmp_path: Path):
    from garuda.eval.dashboard import collect_rows, render_dashboard

    path = tmp_path / "t.json"
    path.write_text(
        json.dumps(
            {
                "agent": {"model_name": "m"},
                "final_metrics": {
                    "total_prompt_tokens": "not-a-number",
                    "total_completion_tokens": None,
                    "total_cost_usd": "free",
                    "extra": {"success": True, "turns": "many", "total_tokens": 12},
                },
            }
        )
    )
    rows = collect_rows(atif_files=[path])
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 0 and rows[0].total_tokens == 12
    assert rows[0].cost_usd is None
    assert "TOTAL" in render_dashboard(rows)  # summation did not raise


def test_dashboard_reads_a_well_formed_trajectory(tmp_path: Path):
    from garuda.eval.dashboard import collect_rows

    path = tmp_path / "ok.json"
    path.write_text(
        json.dumps(
            {
                "agent": {"model_name": "m"},
                "final_metrics": {
                    "total_prompt_tokens": 10,
                    "total_completion_tokens": 5,
                    "total_cost_usd": 0.25,
                    "extra": {"success": True, "turns": 3, "duration_ms": 1500},
                },
            }
        )
    )
    row = collect_rows(atif_files=[path])[0]
    assert (row.prompt_tokens, row.total_tokens, row.turns) == (10, 15, 3)
    assert row.cost_usd == 0.25


# --- ablation isolation -------------------------------------------------------


async def test_one_tasks_failed_setup_does_not_lose_the_matrix(tmp_path: Path, monkeypatch):
    import garuda.model.factory as factory_module
    from garuda.eval.ablation import AblationTask, run_ablation

    # Eval trials resolve through the shared ModelFactory (fresh per variant),
    # so the stub hooks the transport registry with a fresh script per build.
    monkeypatch.setitem(
        factory_module._registry,
        "litellm",
        lambda spec, **kwargs: ScriptModel(responses=[_complete()]),
    )

    def explode(_workspace):
        raise RuntimeError("no fixture for you")

    tasks = [
        AblationTask(id="broken", prompt="p", setup=explode, check=lambda w: False),
        AblationTask(id="fine", prompt="p", check=lambda w: True),
    ]
    results = await run_ablation(
        tasks, {"baseline": {"max_turns": 2}}, "script/test", base_dir=tmp_path
    )
    # The broken task is one recorded cell; the task after it still ran and scored.
    assert len(results) == 2
    broken = next(r for r in results if r.task_id == "broken")
    assert broken.error is not None and "setup failed" in broken.error
    assert broken.graded_pass is None  # ungraded, not a scored zero
    assert next(r for r in results if r.task_id == "fine").graded_pass is True
