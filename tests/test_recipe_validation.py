"""`garuda recipe run` validates the whole recipe before any step runs (#682)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from garuda.config import recipes
from garuda.config.recipes import (
    Recipe,
    RecipeError,
    RecipeStep,
    load_recipe,
    resolve_recipe_params,
)
from garuda.interfaces import main

FIX_AND_TEST = Path(recipes.__file__).parent / "defaults" / "fix-and-test.yaml"


@pytest.mark.parametrize(("text", "code", "message"), [
    ("name: t\nsteps:\n  - prompt: hi\n", "recipe.invalid", "steps[0].agent: must be"),
    ("name: t\nsteps:\n  - agent: build\n", "recipe.invalid", "steps[0].prompt: must be"),
    ("name: t\nsteps: []\n", "recipe.invalid", "steps: must be a non-empty list"),
    ("name: t\n", "recipe.invalid", "steps: must be a non-empty list"),
    ("- just a list\n", "recipe.invalid", "recipe: must be a mapping"),
    ("name: t\nsteps:\n  - agent: build\n    prompt: hi\n    bogus: 1\n",
     "recipe.invalid", "steps[0]: unknown key(s) bogus"),
    ("name: t\nbogus_top: 2\nsteps:\n  - {agent: build, prompt: hi}\n",
     "recipe.invalid", "recipe: unknown key(s) bogus_top"),
    ("name: t\nsteps:\n  - {agent: build, prompt: hi, mode: superyolo}\n",
     "recipe.invalid", "steps[0].mode: must be one of"),
    ("name: t\nsteps:\n  - {agent: build, prompt: \"{{x}}\"}\n",
     "recipe.invalid", "{{x}} is not a declared parameter"),
    ("name: t\nparameters:\n  - {name: a, required: maybe}\nsteps:\n  - {agent: build, prompt: hi}\n",
     "recipe.invalid", "parameters[0].required"),
    ("name: t\nparameters:\n  - {name: a}\n  - {name: a}\nsteps:\n  - {agent: build, prompt: hi}\n",
     "recipe.invalid", "duplicate parameter"),
    ("name: t\nsteps: !!python/object/apply:os.system [\"echo no\"]\n",
     "recipe.invalid", "not valid recipe YAML"),
])
def test_invalid_recipes_refuse_with_a_typed_error(tmp_path, text, code, message):
    path = tmp_path / "r.yaml"
    path.write_text(text)
    with pytest.raises(RecipeError) as refused:
        load_recipe(path)
    assert refused.value.code == code and message in str(refused.value)


def test_a_missing_recipe_file_is_typed(tmp_path):
    with pytest.raises(RecipeError) as refused:
        load_recipe(tmp_path / "nowhere.yaml")
    assert refused.value.code == "recipe.not_found"


def test_parameters_are_checked_before_any_step():
    recipe = load_recipe(FIX_AND_TEST)
    with pytest.raises(RecipeError) as refused:
        resolve_recipe_params(recipe, {})
    assert refused.value.code == "recipe.param_missing"
    assert isinstance(refused.value, ValueError)  # unchanged for library callers
    with pytest.raises(RecipeError) as refused:
        resolve_recipe_params(recipe, {"issue": "x", "bogus": "1"}, allow_undeclared=False)
    assert refused.value.code == "recipe.param_unknown" and "bogus" in str(refused.value)
    # A placeholder that would only fail at its own step refuses up front.
    later = Recipe("t", steps=[RecipeStep("build", "a"), RecipeStep("build", "{{late}}")])
    with pytest.raises(RecipeError, match="steps\\[1\\].prompt uses"):
        resolve_recipe_params(later, {})


@pytest.mark.parametrize(("text", "argv", "code"), [
    ("name: t\nsteps:\n  - prompt: hi\n", [], "recipe.invalid"),
    ("name: t\nsteps:\n  - {agent: nosuchagent, prompt: hi}\n", [], "recipe.agent_not_found"),
    (None, [], "recipe.param_missing"),
    (None, ["--param", "issue=x", "--param", "bogus=1"], "recipe.param_unknown"),
])
def test_the_cli_refuses_before_preparing_a_workspace(tmp_path, monkeypatch, capsys, text, argv, code):
    path = FIX_AND_TEST
    if text is not None:
        path = tmp_path / "r.yaml"
        path.write_text(text)

    async def no_environment(*args, **kwargs):
        raise AssertionError("an invalid recipe must not prepare a workspace")

    monkeypatch.setattr(main, "resolve_environment", no_environment)
    monkeypatch.setattr(sys, "argv", ["garuda", "recipe", "run", str(path),
                                      "--workspace", str(tmp_path), *argv])
    with pytest.raises(SystemExit) as exited:
        main.main()
    err = capsys.readouterr().err
    assert exited.value.code == 2
    assert err.startswith(f"Error: {code}: ") and "Traceback" not in err
