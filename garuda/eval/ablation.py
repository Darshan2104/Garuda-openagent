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
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.model.litellm_model import LitellmModel
from garuda.tools import default_tools
from garuda.types import AgentConfig


@dataclass
class AblationTask:
    id: str
    prompt: str
    setup: Callable[[Path], None] | None = None
    check: Callable[[Path], bool] | None = None


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


# Each variant is a set of AgentConfig overrides applied on top of the base.
DEFAULT_VARIANTS: dict[str, dict] = {
    "baseline": {},
    "no_verifier": {"enable_verifier": False},
    "recent_window": {"condenser": "recent_window"},
    "rigorous": {"mode": "rigorous"},
}


def _base_config(**overrides) -> AgentConfig:
    cfg = AgentConfig(
        max_turns=overrides.pop("max_turns", 20),
        permission_mode="yolo",
        enable_verifier=True,
        enable_llm_verifier=False,  # keep eval cheap/deterministic
        enable_three_step_summary=False,
        sandbox_require=False,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


async def run_variant(
    task: AblationTask, variant: str, overrides: dict, model_name: str, base_dir: Path
) -> VariantResult:
    workspace = base_dir / f"{task.id}__{variant}"
    workspace.mkdir(parents=True, exist_ok=True)
    if task.setup:
        task.setup(workspace)

    config = _base_config(**overrides)
    from garuda.workspace.local import LocalEnvironment

    env = LocalEnvironment(workspace_root=workspace)
    model = LitellmModel(model_name=model_name)
    agent = create_agent("build", mode=config.mode)
    events = EventStore()
    permissions = PermissionEngine(mode="yolo")

    start = time.monotonic()
    error = None
    try:
        result = await agent.run(
            task=task.prompt,
            model=model,
            env=env,
            tools=default_tools(),
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
    )


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
                results.append(
                    await run_variant(task, variant, overrides, model_name, base_dir)
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
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="garuda-ablation", description="Harness ablation runner")
    parser.add_argument("--model", required=True, help="LiteLLM model string")
    parser.add_argument("--variants", help="Comma-separated subset of variant names")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
