"""Ablation runner: score a task set across harness configurations.

This is the model-agnostic, docker-free companion to the Harbor/Terminal-Bench
integration. It runs each task under several `AgentConfig` variants (verifier
on/off, condenser strategy, standard vs rigorous, …) and prints a comparison
table of pass rate, turns, and token usage — the harness-vs-harness measurement
the RFC's "the harness is the product" thesis calls for.

Tasks are graded by a ground-truth `check(workspace) -> bool`, independent of
the agent's own self-reported success, so a variant cannot pass by claiming to.

Run the built-in suite:

    python -m garuda.eval.ablation --model gemini/gemini-2.5-flash
"""

import argparse
import asyncio
import os
import shutil
import tempfile
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from garuda.core.events import EventStore
from garuda.core.modes import apply_mode_preset
from garuda.core.permissions import PermissionEngine
from garuda.eval.evidence import EvaluationEvidence
from garuda.eval.minimal import (
    MinimalConfig,
    authority_identity,
    digest,
    environment_identity,
    runtime_identity,
    source_identity,
    tool_identity,
)
from garuda.model.protocol import Model, ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig


@dataclass
class AblationTask:
    id: str
    prompt: str
    setup: Callable[[Path], None] | None = None
    check: Callable[[Path], bool] | None = None
    check_id: str | None = None
    fixture_id: str | None = None


@dataclass
class VariantResult:
    variant: str
    task_id: str
    agent_success: bool
    graded_pass: bool | None
    turns: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: int
    error: str | None = None
    evaluation: EvaluationEvidence | None = None


# Each variant is a set of AgentConfig overrides applied on top of the base.
DEFAULT_VARIANTS: dict[str, dict] = {
    "baseline": {},
    "no_verifier": {"enable_verifier": False},
    "recent_window": {"condenser": "recent_window"},
    "rigorous": {"mode": "rigorous"},
    # The posture difference is itself worth measuring: this is what a plain
    # `garuda run` now costs versus the graded configuration.
    "eval_gates": {"mode": "eval"},
}


def _base_config(**overrides) -> AgentConfig:
    # Ablation deliberately does NOT go through apply_mode_preset: the point is to
    # toggle one field at a time, so a preset overwriting siblings would confound
    # every variant. Gate fields are therefore spelled out here, and a variant that
    # names a `mode` gets its preset applied afterwards (see run_variant).
    cfg = AgentConfig(
        max_turns=overrides.pop("max_turns", 20),
        permission_mode="yolo",
        enable_verifier=True,
        enable_llm_verifier=False,  # keep eval cheap/deterministic
        enable_three_step_summary=False,
        sandbox_require=False,
    )
    # A variant naming a `mode` gets its preset first, so its own explicit fields
    # still win. Reversing these would let the preset clobber the one knob the
    # variant exists to set — e.g. `{"mode": "eval", "enable_llm_verifier": False}`
    # would silently turn the model call back on.
    if "mode" in overrides:
        cfg.mode = overrides["mode"]
        apply_mode_preset(cfg)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


async def run_variant(
    task: AblationTask, variant: str, overrides: dict, model_name: str, base_dir: Path,
    *, minimal: MinimalConfig | None = None, mechanism: str | None = None,
    enabled: bool | None = None, model: Model | None = None, model_identity: str | None = None,
) -> VariantResult:
    if minimal is not None and overrides:
        raise ValueError("minimal trials accept only the selected mechanism")
    if minimal is not None and not isinstance(model, ScriptModel):
        raise ValueError("minimal-v1 trials currently require an offline ScriptModel")
    if minimal is not None:
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    from garuda.core.rigorous import create_agent

    workspace = base_dir / f"{task.id}__{variant}"
    workspace.mkdir(parents=True, exist_ok=True)
    if task.setup:
        try:
            task.setup(workspace)
        except Exception as exc:  # noqa: BLE001 - eval harness records failures
            # Setup used to run outside any handler, so one task that could not build
            # its fixture (a full disk, a permission error, a typo in a new task) took
            # the entire matrix down and discarded every variant already scored. A
            # failed fixture is one recorded cell, like any other failure here.
            return VariantResult(
                variant=variant,
                task_id=task.id,
                agent_success=False,
                graded_pass=None,
                turns=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                duration_ms=0,
                error=f"setup failed: {type(exc).__name__}: {exc}",
            )

    config = (minimal.config(mechanism=mechanism, enabled=enabled) if minimal is not None
              else _base_config(**overrides))
    config_identity = digest({"version": minimal.version, "settings": asdict(config)}) if minimal is not None else None
    from garuda.model.config import ModelBindings, ModelSpec
    from garuda.model.factory import ModelFactory
    from garuda.workspace.local import LocalEnvironment

    env = LocalEnvironment(workspace_root=workspace)
    # Built fresh per variant through the shared factory: eval trials never
    # share a client, and an explicit eval model keeps its explicit provenance.
    if model is None:
        model = ModelFactory().build(ModelBindings(reasoning=ModelSpec(model=model_name))).reasoning
    agent = create_agent("build", mode=config.mode)
    events = EventStore()
    permissions = (PermissionEngine(mode=config.permission_mode, workspace_root=workspace)
                   if minimal is not None else PermissionEngine(mode="yolo"))
    tools = default_tools()
    if minimal is not None:
        tools = [tool for tool in tools if tool.name in config.allowed_tools]

    start = time.monotonic()
    error = None
    try:
        result = await agent.run(
            task=task.prompt,
            model=model,
            env=env,
            tools=tools,
            config=config,
            events=events,
            permissions=permissions,
        )
        agent_success = result.success
        turns = result.turns
        usage = result.metadata.get("usage", {})
    except Exception as exc:  # noqa: BLE001 - eval harness records failures
        agent_success = False
        turns = 0
        usage = {}
        error = f"{type(exc).__name__}: {exc}"

    duration_ms = int((time.monotonic() - start) * 1000)
    graded = None
    if task.check is not None:
        try:
            graded = bool(task.check(workspace))
        except Exception:  # noqa: BLE001
            graded = False

    evaluation = None
    if minimal is not None:
        prompt_ids = [e["payload"]["digest"] for e in events.get_all()
                      if e["type"] == "system_prompt"]
        evaluation = EvaluationEvidence(
            task=(digest({"id": task.id, "prompt": task.prompt, "fixture": task.fixture_id})
                  if task.setup is None or task.fixture_id is not None else None), model=model_identity,
            runtime=runtime_identity(),
            prompt=digest(prompt_ids) if prompt_ids else None,
            tools=tool_identity(tools),
            environment=environment_identity(), checks=task.check_id, config=config_identity,
            authority=authority_identity(permissions), mechanism=mechanism or "minimal-v1",
            model_completed=agent_success if error is None else None,
            authorized_acceptance="unavailable", coverage="unknown" if graded is None else (
                "pass" if graded else "fail"), cost_reason="not measured by ablation",
            variant=variant, minimal_config=minimal.identity,
        )
    return VariantResult(
        variant=variant,
        task_id=task.id,
        agent_success=agent_success,
        graded_pass=graded,
        turns=turns,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        duration_ms=duration_ms,
        error=error,
        evaluation=evaluation,
    )


async def run_minimal_pair(
    task: AblationTask, mechanism: str, responses: list[ModelResponse], base_dir: Path,
) -> list[VariantResult]:
    """Replay one offline script with exactly one selected config difference.

    This is mechanism evidence only; a paired run cannot establish model uplift.
    No provider client or credentials are loaded.
    """
    minimal = MinimalConfig.load()
    minimal.config(mechanism=mechanism, enabled=False)
    identity = digest({"implementation": source_identity("model/script_model.py"),
                       "responses": [asdict(response) for response in responses]})
    results = []
    for variant, enabled in (("baseline", False), ("candidate", True)):
        result = await run_variant(
            task, variant, {}, "script/incident-v1", base_dir, minimal=minimal,
            mechanism=mechanism, enabled=enabled,
            model=ScriptModel(deepcopy(responses), model_name="script/incident-v1"), model_identity=identity)
        if result.evaluation is None:
            result.evaluation = EvaluationEvidence(
                task=None, model=identity, runtime=runtime_identity(), prompt=None, tools=None,
                environment=environment_identity(), checks=task.check_id, config=None, authority=None,
                mechanism=mechanism, model_completed=None, authorized_acceptance="unavailable",
                coverage="unknown", cost_reason="trial refused before inference", variant=variant,
                minimal_config=minimal.identity)
        results.append(result)
    return results


async def run_ablation(
    tasks: list[AblationTask],
    variants: dict[str, dict],
    model_name: str,
    base_dir: Path | None = None,
) -> list[VariantResult]:
    cleanup = base_dir is None
    base_dir = base_dir or Path(tempfile.mkdtemp(prefix="garuda-ablation-"))
    results: list[VariantResult] = []
    try:
        for task in tasks:
            for variant, overrides in variants.items():
                try:
                    results.append(
                        await run_variant(task, variant, overrides, model_name, base_dir)
                    )
                except Exception as exc:  # noqa: BLE001 - one cell must not lose the matrix
                    # run_variant handles the agent's own failures; this catches the
                    # rest of the cell (workspace creation, model construction, an
                    # override naming a field AgentConfig does not have). The matrix
                    # is expensive to produce and every completed cell is real data.
                    results.append(
                        VariantResult(
                            variant=variant,
                            task_id=task.id,
                            agent_success=False,
                            graded_pass=None,
                            turns=0,
                            prompt_tokens=0,
                            completion_tokens=0,
                            total_tokens=0,
                            duration_ms=0,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
    finally:
        if cleanup:
            shutil.rmtree(base_dir, ignore_errors=True)
    return results


def summarize(results: list[VariantResult]) -> dict[str, dict]:
    """Aggregate per-variant pass rate and averages."""
    by_variant: dict[str, list[VariantResult]] = {}
    for r in results:
        by_variant.setdefault(r.variant, []).append(r)
    summary = {}
    for variant, rs in by_variant.items():
        graded = [r for r in rs if r.graded_pass is not None]
        passes = sum(1 for r in graded if r.graded_pass)
        summary[variant] = {
            "tasks": len(rs),
            "graded_pass": passes,
            "graded_total": len(graded),
            "pass_rate": (passes / len(graded)) if graded else None,
            "avg_turns": sum(r.turns for r in rs) / len(rs) if rs else 0,
            "avg_total_tokens": sum(r.total_tokens for r in rs) / len(rs) if rs else 0,
        }
    return summary


def render_table(results: list[VariantResult]) -> str:
    summary = summarize(results)
    lines = [
        "| Variant | Pass | Pass rate | Avg turns | Avg tokens |",
        "|---------|------|-----------|-----------|------------|",
    ]
    for variant, s in summary.items():
        rate = f"{s['pass_rate']*100:.0f}%" if s["pass_rate"] is not None else "n/a"
        lines.append(
            f"| {variant} | {s['graded_pass']}/{s['graded_total']} | {rate} "
            f"| {s['avg_turns']:.1f} | {s['avg_total_tokens']:.0f} |"
        )
    return "\n".join(lines)


# --- built-in local task suite (no docker required) --------------------------

def _setup_count(workspace: Path) -> None:
    (workspace / "data.txt").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")


def _check_count(workspace: Path) -> bool:
    target = workspace / "count.txt"
    return target.exists() and target.read_text().strip() == "5"


def _check_hello(workspace: Path) -> bool:
    target = workspace / "hello.txt"
    return target.exists() and target.read_text().strip() == "hi"


# --- search: find a value buried in a noisy config (exercises grep/read) ------

def _setup_find_value(workspace: Path) -> None:
    lines = [f"NOISE_{i}=x{i}" for i in range(40)]
    lines.insert(23, "API_KEY=sk-abc123xyz")
    (workspace / "config.env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _check_find_value(workspace: Path) -> bool:
    target = workspace / "answer.txt"
    return target.exists() and target.read_text().strip() == "sk-abc123xyz"


# --- edit: modify an existing file in place (exercises the edit tool) ---------

def _setup_edit(workspace: Path) -> None:
    (workspace / "greeting.py").write_text('print("hello")\n', encoding="utf-8")


def _check_edit(workspace: Path) -> bool:
    target = workspace / "greeting.py"
    if not target.exists():
        return False
    body = target.read_text()
    return 'print("goodbye")' in body and "hello" not in body


# --- compute: sum integers from a file (exercises bash + verification) --------

def _setup_sum(workspace: Path) -> None:
    (workspace / "numbers.txt").write_text("\n".join(str(n) for n in range(1, 21)) + "\n", encoding="utf-8")


def _check_sum(workspace: Path) -> bool:
    target = workspace / "sum.txt"
    return target.exists() and target.read_text().strip() == "210"  # sum(1..20)


BUILTIN_TASKS = [
    AblationTask(
        id="create_file",
        prompt="Create a file named hello.txt in the current directory containing exactly: hi",
        check=_check_hello,
    ),
    AblationTask(
        id="count_lines",
        prompt="Count the number of lines in data.txt and write just that number into count.txt.",
        setup=_setup_count,
        check=_check_count,
    ),
    AblationTask(
        id="find_value",
        prompt="Find the value of API_KEY in config.env and write just that value into answer.txt.",
        setup=_setup_find_value,
        check=_check_find_value,
    ),
    AblationTask(
        id="edit_greeting",
        prompt='Edit greeting.py so it prints "goodbye" instead of "hello". Do not add new print statements.',
        setup=_setup_edit,
        check=_check_edit,
    ),
    AblationTask(
        id="sum_numbers",
        prompt="Compute the sum of the integers in numbers.txt and write just the sum into sum.txt.",
        setup=_setup_sum,
        check=_check_sum,
    ),
]


async def _main_async(args) -> int:
    variants = DEFAULT_VARIANTS
    if args.variants:
        wanted = set(args.variants.split(","))
        variants = {k: v for k, v in DEFAULT_VARIANTS.items() if k in wanted}
    results = await run_ablation(BUILTIN_TASKS, variants, args.model)
    print(render_table(results))
    print("\nPer-task detail:")
    for r in results:
        status = "PASS" if r.graded_pass else "FAIL"
        detail = f" ({r.error})" if r.error else ""
        print(f"  [{status}] {r.task_id:14s} {r.variant:14s} turns={r.turns} tokens={r.total_tokens}{detail}")
    matrix_out = getattr(args, "matrix_out", None)
    if matrix_out:
        from garuda.eval.harness_matrix import (
            load_trials,
            trials_from_ablation,
            write_matrix,
        )
        from garuda.eval.harness_matrix import (
            render_table as matrix_render_table,
        )

        trials = trials_from_ablation(
            BUILTIN_TASKS, results, model=args.model,
            harness=getattr(args, "harness", "native"),
        )
        for feed in getattr(args, "external_trials", []):
            trials.extend(load_trials(feed))
        cells = write_matrix(matrix_out, trials)
        print("\nHarness matrix:")
        print(matrix_render_table(cells))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="garuda-ablation", description="Harness ablation runner")
    parser.add_argument("--model", required=True, help="LiteLLM model string")
    parser.add_argument("--variants", help="Comma-separated subset of variant names")
    parser.add_argument("--harness", default="native", help="Harness label for ablation trials")
    parser.add_argument(
        "--matrix-out", type=Path,
        help="Persist a model × harness JSON matrix artifact after the run",
    )
    parser.add_argument(
        "--external-trials", action="append", default=[], metavar="PATH",
        help="Validated external-harness trial feed to combine with this run",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
