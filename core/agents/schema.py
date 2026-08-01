"""Contract validation for the agent registry.

``config/agents.toml`` is user-editable, so a typo in it must fail with a pointed
message rather than surfacing later as an adapter that silently never runs. The
shape lives in ``contracts/agents.schema.json`` -- one declaration, read at
runtime, not mirrored here.

Hand-rolled for the same reason ``core.events`` is: the alternative is a runtime
dependency whose only job is reading two small files we wrote ourselves. The
supported subset is exactly what those files use and is listed in
``_check`` below; anything outside it is *ignored*, not silently accepted as
valid, so the schema must not grow constructs this module does not implement.
There is a test asserting the schema stays inside the subset.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
AGENTS_SCHEMA_PATH = _ROOT / "contracts" / "agents.schema.json"

#: JSON Schema keywords ``_check`` implements. A schema using anything else is
#: over-declaring: the extra keyword would read as enforced while doing nothing.
SUPPORTED_KEYWORDS = frozenset(
    {
        "$ref",
        "additionalProperties",
        "const",
        "enum",
        "exclusiveMinimum",
        "items",
        "maximum",
        "minLength",
        "minimum",
        "properties",
        "required",
        "type",
    }
)

#: Keywords that carry documentation or plumbing rather than constraints, so the
#: validator passing over them is correct rather than a gap.
SCHEMA_ANNOTATIONS = frozenset({"$schema", "$defs", "title", "description"})

_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


class ConfigContractError(ValueError):
    """A configuration file does not satisfy its declared contract."""


@lru_cache(maxsize=4)
def load_schema(path: Path = AGENTS_SCHEMA_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_agents_config(
    data: Any, *, schema_path: Path = AGENTS_SCHEMA_PATH
) -> Any:
    """Check parsed ``agents.toml`` data and return it unchanged.

    Raises ``ConfigContractError`` naming the offending path, e.g.
    ``agents[1].kind``, so the message points at the line to edit.
    """
    schema = load_schema(schema_path)
    _check(data, schema, schema, "config")
    return data


def _check(value: Any, schema: dict[str, Any], root: dict[str, Any], at: str) -> None:
    ref = schema.get("$ref")
    if ref is not None:
        schema = _resolve(ref, root)

    declared = schema.get("type")
    if declared is not None:
        expected = _TYPES[declared]
        # bool is a subclass of int in Python; a schema asking for a number must
        # not quietly accept ``true``.
        wrong_type = not isinstance(value, expected) or (
            declared in {"integer", "number"} and isinstance(value, bool)
        )
        if wrong_type:
            raise ConfigContractError(
                f"{at}: expected {declared}, got {type(value).__name__}"
            )

    if "const" in schema and value != schema["const"]:
        raise ConfigContractError(f"{at}: must be {schema['const']!r}")

    if "enum" in schema and value not in schema["enum"]:
        raise ConfigContractError(
            f"{at}: {value!r} is not one of {sorted(schema['enum'])}"
        )

    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise ConfigContractError(f"{at}: must not be shorter than "
                                  f"{schema['minLength']} characters")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            raise ConfigContractError(f"{at}: must be at least {minimum}")
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            raise ConfigContractError(f"{at}: must be at most {maximum}")
        exclusive = schema.get("exclusiveMinimum")
        if exclusive is not None and value <= exclusive:
            raise ConfigContractError(f"{at}: must be greater than {exclusive}")

    if isinstance(value, dict):
        properties: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ConfigContractError(f"{at}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ConfigContractError(f"{at}: unknown keys {extra}")
        for key, sub in properties.items():
            if key in value:
                _check(value[key], sub, root, f"{at}.{key}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check(item, schema["items"], root, f"{at}[{index}]")


def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ConfigContractError(f"only local $ref is supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node
