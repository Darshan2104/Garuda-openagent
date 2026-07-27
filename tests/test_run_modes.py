"""Run postures (`core/modes.py`) and the session-meta write lock.

Two mechanisms added when the config surface was collapsed to one selector:

1. `--mode` maps to a coherent gate set, and the default posture is the cheap one.
   The risk this guards is silent: a benchmark that quietly ran without its gates
   would report numbers nobody could reproduce.
2. `meta.json` read-modify-write is serialized, so two writers cannot drop each
   other's fields.
"""

import json
from pathlib import Path

from garuda.agents.loader import load_profile
from garuda.core.modes import (
    DEFAULT_MODE,
    GATE_FIELDS,
    MODE_CHOICES,
    MODE_PRESETS,
    apply_mode_preset,
    is_rigorous,
    resolve_mode,
)
from garuda.core.rigorous import RigorousAgent, create_agent
from garuda.core.sessions import SessionStore, merge_meta
from garuda.types import AgentConfig

# --- posture presets ---------------------------------------------------------


def _gates(config: AgentConfig) -> dict:
    return {field: getattr(config, field) for field in GATE_FIELDS}


def test_bare_config_is_the_cheap_posture():
    """A plain AgentConfig() must cost no extra model calls.

    This is the flip: these gates all defaulted True, so every interactive run paid
    for an LLM judge and a contract derivation it never asked for.
    """
    config = AgentConfig()
    assert config.mode == DEFAULT_MODE == "interactive"
    assert not any(_gates(config).values())
    # The structural gate stays on in every posture — it costs nothing and it is
    # what makes task_complete carry evidence at all.
    assert config.enable_verifier


def test_eval_mode_turns_on_every_gate():
    config = apply_mode_preset(AgentConfig(), "eval")
    assert all(_gates(config).values()), "eval is the graded posture; no gate may be off"
    assert config.enable_verifier


def test_standard_is_an_alias_for_interactive():
    """Every shipped profile declares `mode: standard`, so the alias must resolve."""
    assert resolve_mode("standard") == "interactive"
    assert resolve_mode(None) == "interactive"
    assert not any(_gates(apply_mode_preset(AgentConfig(), "standard")).values())


def test_readonly_mode_actually_forces_readonly_permissions():
    """`--mode readonly` was accepted while nothing read it, so it silently ran a
    normal read-write agent. The preset is what gives it effect."""
    config = apply_mode_preset(AgentConfig(permission_mode="smart"), "readonly")
    assert config.permission_mode == "readonly"
    assert not any(_gates(config).values())


def test_rigorous_keeps_the_full_gate_stack_and_its_own_agent():
    config = apply_mode_preset(AgentConfig(), "rigorous")
    assert all(_gates(config).values())
    assert is_rigorous("rigorous")
    assert not is_rigorous("eval"), "eval is the single-agent loop, not plan/execute/critic"
    assert isinstance(create_agent("build", "rigorous"), RigorousAgent)
    assert not isinstance(create_agent("build", "eval"), RigorousAgent)
    assert not isinstance(create_agent("build"), RigorousAgent)


def test_declared_profile_fields_survive_an_opposing_preset():
    """A profile that states a gate means it; the preset must not overwrite it.

    Diffing against dataclass defaults would not do — a profile declaring a value
    that happens to equal the default still chose it — which is why the preset takes
    the declared-field set.
    """
    config = AgentConfig(enable_acceptance_contract=True)
    apply_mode_preset(config, "interactive", declared_fields={"enable_acceptance_contract"})
    assert config.enable_acceptance_contract, "authored intent lost to the preset"

    undeclared = AgentConfig(enable_acceptance_contract=True)
    apply_mode_preset(undeclared, "interactive", declared_fields=set())
    assert not undeclared.enable_acceptance_contract


def test_unknown_mode_applies_no_preset():
    """Passing an unvalidated mode through is safer than coercing it: the caller's
    own validation reports it instead of the run silently taking a posture nobody
    asked for."""
    config = apply_mode_preset(AgentConfig(), "not-a-mode")
    assert config.mode == "not-a-mode"
    assert not any(_gates(config).values())  # untouched defaults, not eval's


def test_shipped_profiles_all_declare_a_resolvable_mode():
    """A profile naming a mode no preset covers would run with untouched defaults —
    silently, since apply_mode_preset passes an unknown mode through."""
    for name in ("build", "plan", "explore", "harbor"):
        profile = load_profile(name)
        assert profile.mode in MODE_CHOICES, (
            f"{name} declares {profile.mode!r}, which --mode would reject"
        )
        assert resolve_mode(profile.mode) in MODE_PRESETS, (
            f"{name}'s mode {profile.mode!r} resolves to a posture with no preset"
        )


def test_harbor_profile_carries_the_contract_tool():
    """Harbor runs under `eval`, so enable_acceptance_contract is on — and the gate
    refuses a completion while criteria are outstanding. Without the `contract` tool
    every task_complete is unsatisfiable, which is the shape of the original
    livelock."""
    assert "contract" in (load_profile("harbor").tools or [])


def test_harbor_adapter_pins_eval_mode():
    """Pinned, not inherited: the default posture is interactive, and a benchmark
    that silently ran without its gates would report unreproducible numbers."""
    source = Path("garuda/eval/harbor_adapter.py").read_text()
    assert 'mode="eval"' in source


# --- session meta locking ----------------------------------------------------


def test_merge_meta_preserves_existing_fields(tmp_path: Path):
    path = tmp_path / "meta.json"
    merge_meta(path, {"session_id": "s1", "status": "running", "task": "t"})
    merge_meta(path, {"status": "success", "turns": 4})
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["task"] == "t", "a later write must not drop earlier fields"
    assert meta["status"] == "success"
    assert meta["turns"] == 4
    assert "updated_at" in meta


def test_meta_is_published_atomically(tmp_path: Path, monkeypatch):
    """os.replace, not truncate-then-write: a session index that no longer parses is
    worse than a stale one."""
    import garuda.core.sessions as sessions

    calls = []
    real_replace = sessions.os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(sessions.os, "replace", spy)
    merge_meta(tmp_path / "meta.json", {"session_id": "s1"})
    assert calls, "meta must be published via os.replace"


def test_temp_file_name_is_per_process(tmp_path: Path, monkeypatch):
    """A shared `.tmp` name means two concurrent writers scribble over each other's
    staging file and one publishes the other's half-written bytes — the exact
    corruption os.replace is there to prevent."""
    import os

    import garuda.core.sessions as sessions

    staged: list[str] = []
    real_replace = sessions.os.replace

    def spy(src, dst):
        staged.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(sessions.os, "replace", spy)
    target = tmp_path / "meta.json"
    sessions._atomic_write_text(target, "{}")

    assert target.read_text(encoding="utf-8") == "{}"
    assert staged, "nothing was staged through os.replace"
    assert str(os.getpid()) in staged[0], (
        f"staging file {staged[0]!r} is not pid-scoped, so concurrent writers collide"
    )
    assert not list(tmp_path.glob("*.tmp")), "staging file must not be left behind"


def test_corrupt_meta_is_rebuilt_rather_than_crashing(tmp_path: Path):
    path = tmp_path / "meta.json"
    path.write_text("{not json", encoding="utf-8")
    merge_meta(path, {"session_id": "s1", "status": "success"})
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["session_id"] == "s1"


def test_store_finish_and_patch_share_one_lock(tmp_path: Path):
    """`SessionStore.finish` and `runner.update_session_meta` were independent
    read-modify-write cycles, so whichever replaced last dropped the other's fields:
    a finished run could lose its usage totals to a concurrent provenance patch."""
    from garuda.interfaces.runner import update_session_meta

    store = SessionStore(root=tmp_path)
    store.begin(
        session_id="s1", task="t", model="m", agent="build", workspace=str(tmp_path)
    )
    store.update_meta("s1", {"status": "success", "usage": {"total_tokens": 10}})
    update_session_meta(store, "s1", {"resumed_from": "s0"})

    meta = store.load_meta("s1")
    assert meta["usage"] == {"total_tokens": 10}, "patch dropped the finish payload"
    assert meta["resumed_from"] == "s0"
    assert meta["task"] == "t", "both writers dropped the begin payload"


def test_no_lost_updates_under_concurrent_writers(tmp_path: Path):
    """The property the lock exists for. Without it this loses roughly half the
    writers' fields; the sidecar lock file is what serializes the cycle (locking
    meta.json itself would not, because os.replace detaches the inode)."""
    import multiprocessing

    path = tmp_path / "meta.json"
    merge_meta(path, {"seed": 1})
    procs = [
        multiprocessing.Process(target=_hammer_meta, args=(str(path), i)) for i in range(6)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)

    meta = json.loads(path.read_text(encoding="utf-8"))
    missing = [f"k{i}" for i in range(6) if f"k{i}" not in meta]
    assert not missing, f"lost updates from concurrent writers: {missing}"
    assert meta["seed"] == 1


def _hammer_meta(path: str, index: int) -> None:
    """Module-level so it is picklable by multiprocessing's spawn start method."""
    from garuda.core.sessions import merge_meta as merge

    for _ in range(25):
        merge(Path(path), {f"k{index}": index})
