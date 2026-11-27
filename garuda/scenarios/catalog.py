"""Strict packaged-only starter data, independent of the garuda.yaml schema."""

from __future__ import annotations

import re
from importlib.resources import files

import yaml

from garuda.agents.frontmatter import load_yaml_unique
from garuda.scenarios.types import Starter, StarterError

VERSION = 1
RUN_OPTIONS = frozenset({"name", "isolation", "bg", "checks"})
FIELD_TYPES = frozenset({"text", "source-refs", "execution-options", "flow-name"})
#: ``launch.select``: the flow target is the starter's one ``flow-name`` field,
#: checked against the effective flows (packaged, user, project).
SELECTIONS = frozenset({"configured"})
#: The execution options a selecting flow starter may carry: explicit checks only.
SELECT_OPTIONS = frozenset({"checks"})
#: Flow starters that run the effective checks at flow finalization: their
#: owned and CLI starts pass ``checks`` (the explicit ``options.checks``, else
#: ``()``), never ``None``.
CHECKED_FLOW_STARTERS = frozenset({"build-review", "run-flow"})


def _mapping(value, allowed, path):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise StarterError("starter.catalog_invalid", f"{path} must be a string-keyed mapping")
    if unknown := set(value) - set(allowed):
        raise StarterError("starter.catalog_invalid", f"{path}: unknown keys {sorted(unknown)}")
    return value


def _name(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", value):
        raise StarterError("starter.catalog_invalid", f"{path} must be a plain name")
    return value


def _text(value, path):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise StarterError("starter.catalog_invalid", f"{path} must be non-empty text")
    return value


def _yaml(text):
    try:
        return load_yaml_unique(text)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        raise StarterError("starter.catalog_invalid", "invalid or duplicate-key YAML") from exc


def parse_definition(text: str) -> Starter:
    doc = _mapping(_yaml(text), {"version", "id", "title", "launch", "fields", "brief", "example"},
                   "starter")
    if type(doc.get("version")) is not int or doc["version"] != VERSION:
        raise StarterError("starter.catalog_invalid", "starter.version must be 1")
    launch = _mapping(doc.get("launch"), {"kind", "flow", "role", "no_edits", "variants", "options",
                                          "select"}, "launch")
    kind = launch.get("kind")
    if not isinstance(kind, str) or kind not in {"flow", "run"}:
        raise StarterError("starter.catalog_invalid", "launch.kind must be flow or run")
    selects = "select" in launch
    allowed = RUN_OPTIONS if kind == "run" else SELECT_OPTIONS if selects else set()
    options = launch.get("options", [])
    if (not isinstance(options, list) or any(not isinstance(o, str) for o in options)
            or len(set(options)) != len(options) or set(options) - allowed):
        raise StarterError("starter.catalog_invalid", "unsupported or duplicate launch options")
    fields = doc.get("fields")
    if selects:
        _selection(launch, fields)
    elif isinstance(fields, dict) and any(isinstance(field, dict) and field.get("type") == "flow-name"
                                          for field in fields.values()):
        raise StarterError("starter.catalog_invalid", "a flow-name field needs launch.select")
    if kind == "flow":
        if not selects:
            _name(launch.get("flow"), "launch.flow")
        if set(launch) & {"role", "no_edits"}:
            raise StarterError("starter.catalog_invalid", "flow launch cannot carry run settings")
        variants = launch.get("variants", {})
        if not isinstance(variants, dict):
            raise StarterError("starter.catalog_invalid", "launch.variants must be a mapping")
        for key, target in variants.items():
            _name(key, "variant")
            _name(target, "variant.flow")
    else:
        _name(launch.get("role"), "launch.role")
        if set(launch) & {"flow", "variants"}:
            raise StarterError("starter.catalog_invalid", "run launch cannot carry flow settings")
        if "no_edits" in launch and type(launch["no_edits"]) is not bool:
            raise StarterError("starter.catalog_invalid", "launch.no_edits must be boolean")
    if not isinstance(fields, dict) or not fields:
        raise StarterError("starter.catalog_invalid", "fields must be a non-empty mapping")
    for name, value in fields.items():
        _name(name, "field")
        field = _mapping(value, {"type", "required", "label"}, f"fields.{name}")
        if not isinstance(field.get("type"), str) or field["type"] not in FIELD_TYPES:
            raise StarterError("starter.catalog_invalid", f"fields.{name}: unknown field type")
        if "required" in field and type(field["required"]) is not bool:
            raise StarterError("starter.catalog_invalid", f"fields.{name}.required must be boolean")
        if "label" in field:
            _text(field["label"], f"fields.{name}.label")
    return Starter(VERSION, _name(doc.get("id"), "id"), _text(doc.get("title"), "title"),
                   launch, fields, _name(doc.get("brief"), "brief"), _name(doc.get("example"), "example"))


def _selection(launch: dict, fields) -> None:
    """A selecting launch: a flow with exactly one required ``flow-name`` field, ``flow``."""
    if launch.get("kind") != "flow" or not isinstance(launch.get("select"), str) \
            or launch["select"] not in SELECTIONS:
        raise StarterError("starter.catalog_invalid", "launch.select must be configured on a flow launch")
    if set(launch) & {"flow", "variants"}:
        raise StarterError("starter.catalog_invalid", "a selecting launch names no fixed flow or variants")
    names = [name for name, field in (fields if isinstance(fields, dict) else {}).items()
             if isinstance(field, dict) and field.get("type") == "flow-name"]
    if names != ["flow"] or fields["flow"].get("required") is not True:
        raise StarterError("starter.catalog_invalid",
                           "launch.select needs exactly one required flow-name field named flow")


def selects_flow(entry: Starter) -> bool:
    """A starter that selects its flow by name; reachable by id, never listed."""
    return "select" in entry.launch


def load_catalog() -> dict[str, Starter]:
    """Load installed package resources; never look for a project catalog."""
    root = files("garuda.scenarios")
    examples = _mapping(_yaml(root.joinpath("data", "examples.yaml").read_text(encoding="utf-8")),
                        {"version", "examples"}, "examples")
    if type(examples.get("version")) is not int or examples["version"] != VERSION:
        raise StarterError("starter.catalog_invalid", "examples.version must be 1")
    if not isinstance(examples.get("examples"), dict):
        raise StarterError("starter.catalog_invalid", "examples.examples must be a mapping")
    entries = {}
    for path in sorted(root.joinpath("data").iterdir(), key=lambda p: p.name):
        if path.name == "examples.yaml" or not path.name.endswith(".yaml"):
            continue
        entry = parse_definition(path.read_text(encoding="utf-8"))
        if entry.id in entries:
            raise StarterError("starter.catalog_invalid", f"duplicate starter {entry.id}")
        if not root.joinpath("briefs", entry.brief + ".md").is_file():
            raise StarterError("starter.catalog_invalid", f"missing brief {entry.brief}")
        _text(brief_text(entry), f"brief.{entry.brief}")
        example = examples["examples"].get(entry.example)
        if not isinstance(example, dict) or example.get("starter") != entry.id:
            raise StarterError("starter.catalog_invalid", f"missing or mismatched example {entry.example}")
        _mapping(example, {"starter", "inputs"}, "example")
        if not isinstance(example.get("inputs"), dict) or set(example["inputs"]) - set(entry.fields):
            raise StarterError("starter.catalog_invalid", f"invalid example inputs for {entry.id}")
        entries[entry.id] = entry
    for name, example in examples["examples"].items():
        _name(name, "example.id")
        _mapping(example, {"starter", "inputs"}, "example")
        if not isinstance(example.get("starter"), str) or example["starter"] not in entries:
            raise StarterError("starter.catalog_invalid", "example names no installed starter")
        from garuda.scenarios.inputs import validate_inputs

        validate_inputs(entries[example["starter"]], example.get("inputs"))
    return entries


def brief_text(entry: Starter) -> str:
    return files("garuda.scenarios").joinpath("briefs", entry.brief + ".md").read_text(encoding="utf-8")


def example_inputs(entry: Starter) -> dict:
    """Return the installed example's form data; never read project templates."""
    from copy import deepcopy

    examples = _yaml(files("garuda.scenarios").joinpath("data", "examples.yaml").read_text(encoding="utf-8"))
    return deepcopy(examples["examples"][entry.example]["inputs"])
