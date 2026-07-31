"""Guards on the model-facing prompts and tool contracts.

These prompts are tuned by measuring behaviour on a benchmark, which is exactly
the setup where guidance drifts into encoding the benchmark. Two rules keep that
honest:

  1. Shared guidance lives in the shared surface. A profile prompt *replaces*
     DEFAULT_SYSTEM_PROMPT rather than extending it, so advice put in `harbor`
     reaches only the profile the benchmark numbers come from. Verification
     guidance therefore belongs to the `task_complete` contract, which every
     profile gets by construction.
  2. No prompt or tool contract may name an artifact from the tasks used to
     validate it. Naming one is what overfitting actually looks like in a diff.

The prompts were also measured to be load-bearing in a way that is easy to get
backwards: on 2026-07-30, adding cost-framed batching guidance cut investigation
32% because the model read it as licence to check less. Guidance about *how to
know you are right* must not carry a hint about what it costs.
"""

import pathlib

import yaml

from garuda.tools.task_complete import TaskCompleteTool
from garuda.types import DEFAULT_SYSTEM_PROMPT

DEFAULTS = pathlib.Path(__file__).resolve().parents[1] / "garuda" / "agents" / "defaults"

# The idea, not a verbatim string: phrasing differs across surfaces (one is a
# numbered principle, one is prose), so assert on the load-bearing clauses.
# Wording that must NOT be duplicated into per-profile prompts — see
# test_falsification_guidance_lives_in_the_shared_tool_contract.
FALSIFICATION_CLAUSES = (
    "prove yourself wrong",
    "if you had got it wrong",
    "instead of what was asked",
)

# Artifacts of the four terminal-bench-pro tasks these prompts were measured on.
# If one of these ever appears in a prompt, the guidance stopped being general.
VALIDATION_TASK_ARTIFACTS = (
    "rfc4180",
    "fasttext",
    "jinja2",
    "ddos",
    "pickle",
    "anomaly.json",
    "traffic",
    "subword",
    "sanitize_template",
    "terminal-bench",
    "reward",
)


def _normalized(text: str) -> str:
    """Collapse whitespace so a clause is found regardless of where it wraps.

    These prompts are hand-wrapped YAML block scalars, so matching raw text would
    make the guard fail on a reflow rather than on a change of meaning.
    """
    return " ".join(text.lower().split())


def _profile_prompt(name: str) -> str:
    data = yaml.safe_load((DEFAULTS / f"{name}.yaml").read_text())
    return data.get("system_prompt") or ""


def _all_model_facing_text() -> dict[str, str]:
    text = {
        "DEFAULT_SYSTEM_PROMPT": DEFAULT_SYSTEM_PROMPT,
        "task_complete.description": TaskCompleteTool.description,
        "task_complete.parameters": str(TaskCompleteTool.parameters),
    }
    for path in sorted(DEFAULTS.glob("*.yaml")):
        prompt = _profile_prompt(path.stem)
        if prompt:
            text[f"{path.stem}.yaml"] = prompt
    return text


def test_no_prompt_names_a_task_it_was_tuned_on():
    """The anti-overfitting guard. A prompt that mentions a validation task's
    file format, library or output path has encoded the suite, not the skill."""
    offenders = []
    for where, text in _all_model_facing_text().items():
        lowered = _normalized(text)
        for artifact in VALIDATION_TASK_ARTIFACTS:
            if artifact in lowered:
                offenders.append(f"{where}: {artifact!r}")
    assert not offenders, "prompts must not name validation-task artifacts: " + "; ".join(offenders)


def test_falsification_guidance_lives_in_the_shared_tool_contract():
    """It belongs to the `task_complete` contract, not to any profile prompt.

    Two reasons, one principled and one measured. Principled: a profile prompt
    *replaces* DEFAULT_SYSTEM_PROMPT rather than extending it, so guidance placed
    in `harbor` reaches only the profile the benchmark numbers come from — which
    is the definition of tuning to the scoreboard. The tool contract reaches every
    profile by construction.

    Measured (2026-07-31, 4 tasks): the same wording added to all three prompts as
    well moved deliberation to the completion step and lifted the assertion share
    of accepted evidence 17% -> 27%, but investigation fell 31% and grep went to
    zero on every task — the prompt-dilution hazard this repo has now hit twice.
    The contract change kept the mechanism; the prompt copies bought nothing
    separately measurable, so they were reverted.
    """
    contract = _normalized(
        TaskCompleteTool.description
        + " "
        + TaskCompleteTool.parameters["properties"]["verification_commands"]["description"]
    )
    assert "would change if the work were wrong" in contract
    for name in ("harbor", "build"):
        prompt = _normalized(_profile_prompt(name))
        for clause in FALSIFICATION_CLAUSES:
            assert clause not in prompt, (
                f"{name}.yaml duplicates shared verification guidance ({clause!r}); "
                "it belongs in the task_complete contract so every profile gets it"
            )
    for clause in FALSIFICATION_CLAUSES:
        assert clause not in _normalized(DEFAULT_SYSTEM_PROMPT)


def test_the_completion_contract_asks_for_a_falsifiable_check():
    """The decision point. Three of four false-negatives in the 2026-07-30 run
    were an oracle that could not distinguish done from apparently-done, so the
    tool that accepts them has to say what makes one worth running."""
    commands = _normalized(
        TaskCompleteTool.parameters["properties"]["verification_commands"]["description"]
    )
    assert "exit status would change if the work were wrong" in commands
    assert "still exit 0" in commands
    assert "not merely" in commands, "must distinguish checking the ask from checking the build"

    summary = _normalized(TaskCompleteTool.parameters["properties"]["summary"]["description"])
    assert "observation" in summary and "not" in summary


def test_verification_guidance_carries_no_cost_framing():
    """Measured 2026-07-30: a cost-framed sentence in the prompt was read as
    permission to do less work (investigation fell 32%, grep to zero on every
    task). Guidance about correctness must not mention what correctness costs."""
    cost_words = ("expensive", "cheaper", "cost", "token", "round-trip", "spend")
    surfaces = {
        "task_complete.description": TaskCompleteTool.description,
        "verification_commands": TaskCompleteTool.parameters["properties"][
            "verification_commands"
        ]["description"],
        "summary": TaskCompleteTool.parameters["properties"]["summary"]["description"],
    }
    for where, text in surfaces.items():
        found = [w for w in cost_words if w in _normalized(text)]
        assert not found, f"{where} frames verification in cost terms: {found}"
