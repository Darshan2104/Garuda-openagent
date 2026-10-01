"""``garuda run --runtime native`` pins the native runtime (#147).

The flag's default used to be the string ``"native"``, so naming native was
indistinguishable from omitting the flag: a trusted routing rule, the policy router
or the classifier could still move the run to an ACP harness. Driven through the
real parser and ``run_task``; the executor that would start is observed by
standing in for the two launch points.
"""

import pytest

import garuda.interfaces.main as main


class _NativeStarted(Exception):
    pass


@pytest.fixture
def launches(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.yaml").write_text(
        "routing:\n"
        "  rules:\n"
        "    - id: everything-to-codex\n"
        "      priority: 100\n"
        "      runtime: codex\n"
        "      when:\n"
        "        agents: [build]\n"
    )
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(home / "settings.yaml"))
    seen: list[str] = []

    async def acp(args, task, catalog):
        seen.append(args.runtime)
        return 0

    async def native(*args, **kwargs):
        seen.append("native")
        raise _NativeStarted

    # No vendor CLI is installed in CI: report every configured runtime as
    # available so the rule, not a missing binary, decides.
    monkeypatch.setattr("garuda.agents.setup.RuntimeCatalog.discover", lambda self, **_: ())
    monkeypatch.setattr(main, "run_acp_command", acp)
    monkeypatch.setattr("garuda.agents.setup.prepare_agent_run", native)
    return seen


def _args(tmp_path, *extra):
    return main.build_parser().parse_args(["run", "-t", "do it", "--workspace", str(tmp_path), *extra])


async def test_without_the_flag_a_trusted_rule_still_routes(tmp_path, launches):
    args = _args(tmp_path)
    assert args.runtime is None

    await main.run_task(args)

    assert launches == ["codex"]


async def test_naming_native_beats_a_trusted_rule(tmp_path, launches):
    args = _args(tmp_path, "--runtime", "native")

    with pytest.raises(_NativeStarted):
        await main.run_task(args)

    assert launches == ["native"]
    selection = args._initial_selection
    assert selection.selected == "native"
    assert "explicit" in " ".join(selection.rationale)


async def test_naming_native_skips_the_policy_router_and_classifier(tmp_path, launches, monkeypatch):
    def router(*args, **kwargs):
        raise AssertionError("the policy router must not run for an explicit runtime")

    def classifier(*args, **kwargs):
        raise AssertionError("the classifier must not run for an explicit runtime")

    monkeypatch.setattr("garuda.agents.setup.select_runtime", router)
    monkeypatch.setattr("garuda.agents.setup.resolve_runtime_classifier", classifier)

    with pytest.raises(_NativeStarted):
        await main.run_task(_args(tmp_path, "--runtime", "native"))

    assert launches == ["native"]
