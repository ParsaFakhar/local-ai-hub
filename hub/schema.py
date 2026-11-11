"""A tiny, dependency-free validator for the JSON-Schema subset used by
plugin manifests and structured LLM output.

Supported keywords: type, properties, required, items, enum, const,
minimum, maximum, minLength, maxLength, minItems, maxItems, default.

This is deliberately small: it validates *plugin input* and *LLM output
shape*.  It is not a full JSON-Schema implementation.
"""

from __future__ import annotations

import copy
from typing import Any

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
    "null": type(None),
}


def _type_ok(value: Any, expected: str) -> bool:
    python_type = _TYPES.get(expected)
    if python_type is None:
        return True
    if expected in ("number", "integer") and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, int):
        return True
    return isinstance(value, python_type)


def validate(value: Any, schema: dict | None, path: str = "$") -> list[str]:
    """Return a list of human readable errors (empty == valid)."""
    if not schema or not isinstance(schema, dict):
        return []

    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, option) for option in options):
            return [f"{path}: expected type {expected}, got {type(value).__name__}"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']!r}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property '{key}'")
        properties = schema.get("properties", {})
        for key, sub_schema in properties.items():
            if key in value:
                errors.extend(validate(value[key], sub_schema, f"{path}.{key}"))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                errors.extend(validate(item, item_schema, f"{path}[{index}]"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")

    return errors


def apply_defaults(value: Any, schema: dict | None) -> Any:
    """Fill object defaults declared in a schema (non-destructive)."""
    if not schema or not isinstance(schema, dict):
        return value
    if schema.get("type") == "object" or "properties" in schema:
        result = dict(value) if isinstance(value, dict) else {}
        for key, sub_schema in schema.get("properties", {}).items():
            if key in result:
                result[key] = apply_defaults(result[key], sub_schema)
            elif isinstance(sub_schema, dict) and "default" in sub_schema:
                result[key] = copy.deepcopy(sub_schema["default"])
        return result
    return value


def example_from_schema(schema: dict | None, depth: int = 0) -> Any:
    """Deterministic sample value for a schema.  Used by the offline mock model."""
    if not schema or depth > 6:
        return None
    if "default" in schema:
        return copy.deepcopy(schema["default"])
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]

    kind = schema.get("type")
    if isinstance(kind, list):
        kind = kind[0]
    if kind == "object" or "properties" in schema:
        return {
            key: example_from_schema(sub, depth + 1)
            for key, sub in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [example_from_schema(schema.get("items", {}), depth + 1)]
    if kind == "boolean":
        return False
    if kind in ("number", "integer"):
        return schema.get("minimum", 0)
    if kind == "string":
        return ""
    return None
