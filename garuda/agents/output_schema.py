"""A final-output JSON Schema for an agent (plan task H.12b, #165).

``output: {schema: schemas/result.json}`` names a bounded local file, resolved
relative to the declaring definition and kept inside its root. The schema is
checked and compiled **when the definition is resolved**; a schema that can't be
trusted refuses activation rather than failing a run later.

What is accepted is deliberately small (Draft 2020-12, ``jsonschema``'s
``Draft202012Validator``):

* only keywords this module knows. An unknown or unsupported keyword refuses
  instead of being ignored — ``format`` and ``content*`` (no checker would run),
  ``pattern`` and ``patternProperties`` (a regular expression from a file is
  evaluated on model output in this process), ``$id``/``$anchor``/``$dynamic*``/
  ``$vocabulary`` (they change what a reference means);
* ``$ref`` only to the same document (``#`` or ``#/json/pointer``), so nothing
  is fetched and no file outside the approved root is read;
* bounded size, nesting depth, reference count and total reference expansion,
  and no reference cycles.

Validating the output is not verifying the task: the ordinary completion and
verification gates still run on a valid result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from garuda.agents import spec

DIALECT = "https://json-schema.org/draft/2020-12/schema"
MAX_BYTES = 64 * 1024
MAX_DEPTH = 32
MAX_REFS = 64
MAX_EXPANDED_NODES = 20_000
MAX_RESULT_BYTES = 256 * 1024
MAX_ERRORS = 5

_ANNOTATIONS = {"$comment", "title", "description", "default", "deprecated", "readOnly",
                "writeOnly", "examples"}
_ASSERTIONS = {"type", "enum", "const", "multipleOf", "maximum", "exclusiveMaximum", "minimum",
               "exclusiveMinimum", "maxLength", "minLength", "maxItems", "minItems",
               "uniqueItems", "maxContains", "minContains", "maxProperties", "minProperties",
               "required", "dependentRequired"}
_SCHEMA = {"additionalProperties", "items", "contains", "propertyNames", "not", "if", "then",
           "else", "unevaluatedItems", "unevaluatedProperties"}
_SCHEMA_LIST = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_MAP = {"properties", "dependentSchemas", "$defs"}
ALLOWED = (_ANNOTATIONS | _ASSERTIONS | _SCHEMA | _SCHEMA_LIST | _SCHEMA_MAP
           | {"$schema", "$ref"})


def _refuse(code: str, message: str, where: str = "") -> spec.AgentSpecError:
    return spec.AgentSpecError(code, "output.schema", message, source=where)


def _walk(node: Any, pointer: str, depth: int, refs: list, where: str) -> None:
    """Check keywords and collect ``(pointer, $ref)`` pairs of one subschema."""
    if depth > MAX_DEPTH:
        raise _refuse("agent.output_schema_too_large",
                      f"nested deeper than {MAX_DEPTH} levels", where)
    if isinstance(node, bool):
        return
    if not isinstance(node, dict):
        raise _refuse("agent.output_schema_invalid", f"{pointer or '#'} is not a schema", where)
    for key, value in node.items():
        if key not in ALLOWED:
            raise _refuse("agent.output_schema_unsupported",
                          f"keyword {key!r} at {pointer or '#'} is not supported", where)
        here = f"{pointer}/{key}"
        if key == "$ref":
            refs.append((pointer, value))
        elif key in _SCHEMA:
            _walk(value, here, depth + 1, refs, where)
        elif key in _SCHEMA_LIST:
            if not isinstance(value, list):
                raise _refuse("agent.output_schema_invalid", f"{here} must be a list", where)
            for index, item in enumerate(value):
                _walk(item, f"{here}/{index}", depth + 1, refs, where)
        elif key in _SCHEMA_MAP:
            if not isinstance(value, dict):
                raise _refuse("agent.output_schema_invalid", f"{here} must be a mapping", where)
            for name, item in value.items():
                _walk(item, f"{here}/{_escape(name)}", depth + 1, refs, where)
    if node.get("$schema", DIALECT) != DIALECT and pointer == "":
        raise _refuse("agent.output_schema_unsupported",
                      f"only $schema {DIALECT} is supported", where)


def _escape(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


def _lookup(doc: Any, ref: str, where: str) -> str:
    """The canonical pointer a same-document ``$ref`` names, or a refusal."""
    if not isinstance(ref, str) or not (ref == "#" or ref.startswith("#/")):
        raise _refuse("agent.output_schema_ref",
                      f"$ref {ref!r} must point inside the same schema file (#/...)", where)
    node: Any = doc
    pointer = ref[1:]
    for part in pointer.split("/")[1:] if pointer else []:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise _refuse("agent.output_schema_ref", f"$ref {ref!r} does not resolve", where)
    return pointer


def _check_references(doc: dict, refs: list, where: str) -> None:
    if len(refs) > MAX_REFS:
        raise _refuse("agent.output_schema_too_large",
                      f"{len(refs)} references; at most {MAX_REFS}", where)
    targets: dict[str, list[str]] = {}
    for owner, ref in refs:
        targets.setdefault(owner, []).append(_lookup(doc, ref, where))
    # Expanding every reference must stay small, and may not loop.
    sizes: dict[str, int] = {}

    def inside(owner: str, root: str) -> bool:
        return root == "" or owner == root or owner.startswith(root + "/")

    def size(pointer: str, active: tuple) -> int:
        if pointer in active:
            raise _refuse("agent.output_schema_ref",
                          "recursive references are not supported", where)
        if pointer not in sizes:
            total = 1
            for owner, followed in targets.items():
                if inside(owner, pointer):
                    for target in followed:
                        total += size(target, (*active, pointer))
                        if total > MAX_EXPANDED_NODES:
                            raise _refuse("agent.output_schema_too_large",
                                          f"references expand beyond {MAX_EXPANDED_NODES} nodes",
                                          where)
            sizes[pointer] = total
        return sizes[pointer]

    for followed in targets.values():
        for target in followed:
            size(target, ())


def compile_schema(doc: Any, *, where: str = "") -> dict:
    """Check ``doc`` and return it; raises :class:`~garuda.agents.spec.AgentSpecError`."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    if not isinstance(doc, (dict, bool)):
        raise _refuse("agent.output_schema_invalid", "the schema must be a JSON object", where)
    refs: list = []
    _walk(doc, "", 0, refs, where)
    if isinstance(doc, dict):
        _check_references(doc, refs, where)
    try:
        Draft202012Validator.check_schema(doc)
    except SchemaError as exc:
        raise _refuse("agent.output_schema_invalid", exc.message[:200], where) from exc
    return doc


def load(source, rel: str) -> dict:
    """Read, check and return the schema ``rel`` names beside ``source``'s file."""
    from garuda.agents import resolve

    where = str(source.path)
    target = Path(rel)
    if target.is_absolute() or ".." in target.parts:
        raise spec.AgentSpecError("agent.path_escapes", "output.schema",
                                  f"{rel} must be a relative path inside the agent's root",
                                  source=where)
    base = source.path.parent if source.path else Path.cwd()
    path = base / target
    if not path.is_file():
        raise _refuse("agent.output_schema_invalid", f"{rel} does not exist", where)
    data = resolve._read(path, source.root or base)
    if len(data) > MAX_BYTES:
        raise _refuse("agent.output_schema_too_large",
                      f"{rel} is over {MAX_BYTES} bytes", where)
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _refuse("agent.output_schema_invalid", f"{rel} is not valid JSON: {exc}",
                      where) from exc
    return compile_schema(doc, where=where)


def validation_errors(schema: Any, instance: Any) -> list[str]:
    """Why ``instance`` does not satisfy ``schema`` (empty when it does). Bounded."""
    from jsonschema import Draft202012Validator

    try:
        encoded = json.dumps(instance)
    except (TypeError, ValueError):
        return ["the result is not valid JSON data"]
    if len(encoded) > MAX_RESULT_BYTES:
        return [f"the result is over {MAX_RESULT_BYTES} bytes"]
    errors = sorted(Draft202012Validator(schema).iter_errors(instance),
                    key=lambda e: [str(p) for p in e.absolute_path])
    out = []
    for error in errors[:MAX_ERRORS]:
        path = "/".join(str(p) for p in error.absolute_path) or "(the result)"
        out.append(f"{path}: {error.message[:200]}")
    if len(errors) > MAX_ERRORS:
        out.append(f"... and {len(errors) - MAX_ERRORS} more")
    return out
