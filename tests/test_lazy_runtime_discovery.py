"""Initial runtime selection probes only runtimes it could choose (#148).

Every ``garuda run`` — including a plain native one — used to run the version and
auth probes of every configured ACP runtime before the task started. Sentinel
executables named like two built-in adapters record each time they are invoked.
"""

import os
import stat
from pathlib import Path

import pytest

import garuda.interfaces.main as main
from garuda.agents.setup import prepare_runtime_catalog


class _Started(Exception):
    pass


@pytest.fixture
def sentinels(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "probes.log"
    for name in ("pi-acp", "goose"):
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\necho {name} >> {log}\necho 'RAW-PROBE-OUTPUT 9.9'\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(home / "settings.yaml"))

    async def acp(args, task, catalog):
        raise _Started(args.runtime)

    async def native(*args, **kwargs):
        raise _Started("native")

    monkeypatch.setattr(main, "run_acp_command", acp)
    monkeypatch.setattr("garuda.agents.setup.prepare_agent_run", native)

    def probed() -> list[str]:
        return log.read_text().split() if log.exists() else []

    return home, probed


async def _run(tmp_path: Path, *extra: str) -> str:
    args = main.build_parser().parse_args(["run", "-t", "do it", "--workspace", str(tmp_path), *extra])
    with pytest.raises(_Started) as started:
        await main.run_task(args)
    return str(started.value)


async def test_a_plain_native_run_probes_nothing(tmp_path, sentinels):
    _, probed = sentinels

    assert await _run(tmp_path) == "native"

    assert probed() == []


async def test_an_explicit_runtime_probes_only_itself(tmp_path, sentinels):
    _, probed = sentinels

    await _run(tmp_path, "--runtime", "pi")

    assert set(probed()) == {"pi-acp"}


async def test_a_rule_probes_only_its_target_and_reuses_the_result(tmp_path, sentinels):
    home, probed = sentinels
    (home / "settings.yaml").write_text(
        "routing:\n  rules:\n    - id: to-goose\n      runtime: goose\n      when:\n        agents: [build]\n"
    )

    assert await _run(tmp_path) == "goose"
    assert await _run(tmp_path) == "goose"

    assert probed() == ["goose"]
    cache = (home / "cache" / "runtime-probes.json").read_text()
    assert "RAW-PROBE-OUTPUT" not in cache
    assert oct((home / "cache" / "runtime-probes.json").stat().st_mode & 0o777) == "0o600"


async def test_listing_runtimes_still_probes_every_runtime(tmp_path, sentinels):
    _, probed = sentinels

    prepare_runtime_catalog(str(tmp_path)).discover()

    assert set(probed()) == {"pi-acp", "goose"}
