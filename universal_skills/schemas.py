"""Load and validate the Universal Skill Plan JSON Schema without extra deps."""

from __future__ import annotations

import copy
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence, cast

from .errors import OpenAIResponseFormatError


PLAN_SCHEMA_NAME = "universal_skill_plan"
PLAN_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "plan.schema.json"

# The Responses API Structured Outputs subset does not accept these local
# validation keywords. Keep them in the checked-in schemas and enforce them
# again after parsing, but omit them from the API-only schema projection.
OPENAI_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {"$schema", "title", "minLength", "maxLength", "uniqueItems", "const"}
)

_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)
_SUPPORTED_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


class _DuplicateKeyError(ValueError):
    pass


class _InstanceValidationError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-JSON numeric constant {value}")


@lru_cache(maxsize=1)
def _load_plan_schema_template() -> dict[str, Any]:
    try:
        raw = PLAN_SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - installation/environment failure
        raise RuntimeError(f"Plan schema could not be read: {PLAN_SCHEMA_PATH}") from exc

    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, _DuplicateKeyError, ValueError) as exc:
        raise RuntimeError(f"Plan schema is not valid JSON: {PLAN_SCHEMA_PATH}") from exc

    if not isinstance(value, dict):
        raise RuntimeError("Plan schema root must be an object.")
    _check_supported_schema(value)
    _assert_strict_objects(value)
    return value


def load_plan_schema() -> dict[str, Any]:
    """Return a fresh copy of the repository schema, its single source of truth."""

    return copy.deepcopy(_load_plan_schema_template())


def api_plan_schema() -> dict[str, Any]:
    """Return a fresh strict schema suitable for Responses ``text.format``."""

    schema = schema_for_openai_structured_outputs(load_plan_schema())
    _assert_strict_objects(schema)
    return schema


def schema_for_openai_structured_outputs(
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a local strict schema onto OpenAI's supported JSON subset.

    The traversal understands schema containers, so a user field named
    ``title`` inside ``properties`` is retained while the schema annotation
    keyword ``title`` is removed.
    """

    projected = copy.deepcopy(dict(schema))
    _remove_schema_keywords(projected, OPENAI_UNSUPPORTED_SCHEMA_KEYWORDS)
    return projected


# A readable alias for clients that build a complete Responses payload.
plan_schema_for_api = api_plan_schema


def parse_plan_json(text: str) -> dict[str, Any]:
    """Parse one model JSON result and validate it against the plan schema."""

    if not isinstance(text, str):
        raise OpenAIResponseFormatError("OpenAI plan output must be a JSON string.")
    if not text.strip():
        raise OpenAIResponseFormatError("OpenAI returned an empty plan.")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise OpenAIResponseFormatError(
            f"OpenAI returned invalid plan JSON at line {exc.lineno}, column {exc.colno}."
        ) from exc
    except (_DuplicateKeyError, ValueError) as exc:
        raise OpenAIResponseFormatError(f"OpenAI returned invalid plan JSON: {exc}.") from exc

    return validate_plan(value)


def validate_plan(value: object) -> dict[str, Any]:
    """Validate an already-decoded plan and return it with a precise dict type."""

    validate_schema_instance(value, _load_plan_schema_template())
    return cast(dict[str, Any], value)


def validate_schema_instance(instance: object, schema: Mapping[str, Any]) -> None:
    """Validate JSON-like data using the strict subset supported by this package.

    This intentionally implements the schema keywords used by ``plan.schema.json``
    instead of maintaining a second plan-shaped validator or adding a runtime
    dependency on a full JSON Schema implementation.
    """

    _check_supported_schema(schema)
    try:
        _validate_instance(instance, schema, "$")
    except _InstanceValidationError as exc:
        raise OpenAIResponseFormatError(f"Plan does not match its schema: {exc}") from exc


def _check_supported_schema(schema: Mapping[str, Any], path: str = "$") -> None:
    if not isinstance(schema, Mapping):
        raise ValueError(f"Schema at {path} must be an object.")

    unknown = set(schema) - _SUPPORTED_SCHEMA_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ValueError(f"Unsupported schema keyword(s) at {path}: {names}.")

    declared = schema.get("type")
    if declared is not None:
        if isinstance(declared, str):
            declared_types = (declared,)
        elif isinstance(declared, list) and declared and all(
            isinstance(item, str) for item in declared
        ):
            declared_types = tuple(declared)
        else:
            raise ValueError(f"Schema type at {path} must be a string or non-empty list.")
        invalid_types = set(declared_types) - _SUPPORTED_TYPES
        if invalid_types:
            raise ValueError(f"Unsupported schema type at {path}: {sorted(invalid_types)!r}.")
        if len(set(declared_types)) != len(declared_types):
            raise ValueError(f"Schema type list at {path} contains duplicates.")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping) or not all(
            isinstance(key, str) for key in properties
        ):
            raise ValueError(f"Schema properties at {path} must be an object.")
        for name, child in properties.items():
            if not isinstance(child, Mapping):
                raise ValueError(f"Schema at {path}.{name} must be an object.")
            _check_supported_schema(child, f"{path}.{name}")

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError(f"Schema required at {path} must be a string array.")
        if len(set(required)) != len(required):
            raise ValueError(f"Schema required at {path} contains duplicates.")
        if isinstance(properties, Mapping) and not set(required).issubset(properties):
            raise ValueError(f"Schema required at {path} names an unknown property.")

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, (bool, Mapping)):
        raise ValueError(f"additionalProperties at {path} must be boolean or a schema.")
    if isinstance(additional, Mapping):
        _check_supported_schema(additional, f"{path}.*")

    items = schema.get("items")
    if items is not None:
        if not isinstance(items, Mapping):
            raise ValueError(f"Schema items at {path} must be an object.")
        _check_supported_schema(items, f"{path}[]")

    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ValueError(f"Schema enum at {path} must be a non-empty array.")

    for keyword in ("minimum", "maximum"):
        boundary = schema.get(keyword)
        if boundary is not None and (
            isinstance(boundary, bool) or not isinstance(boundary, (int, float))
        ):
            raise ValueError(f"Schema {keyword} at {path} must be a number.")
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        boundary = schema.get(keyword)
        if boundary is not None and (
            isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0
        ):
            raise ValueError(f"Schema {keyword} at {path} must be a non-negative integer.")
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            raise ValueError(f"Schema pattern at {path} must be a string.")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Schema pattern at {path} is invalid.") from exc
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise ValueError(f"Schema uniqueItems at {path} must be boolean.")


def _assert_strict_objects(schema: Mapping[str, Any], path: str = "$") -> None:
    declared = schema.get("type")
    declared_types = {declared} if isinstance(declared, str) else set(declared or ())
    if "object" in declared_types:
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping):
            raise RuntimeError(f"Strict schema object at {path} has no properties object.")
        if schema.get("additionalProperties") is not False:
            raise RuntimeError(f"Strict schema object at {path} must reject additional properties.")
        if not isinstance(required, list) or set(required) != set(properties):
            raise RuntimeError(f"Strict schema object at {path} must require every property.")

    properties = schema.get("properties", {})
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            if isinstance(child, Mapping):
                _assert_strict_objects(child, f"{path}.{name}")
    items = schema.get("items")
    if isinstance(items, Mapping):
        _assert_strict_objects(items, f"{path}[]")
    additional = schema.get("additionalProperties")
    if isinstance(additional, Mapping):
        _assert_strict_objects(additional, f"{path}.*")


def _remove_schema_keywords(
    schema: dict[str, Any], keywords: frozenset[str]
) -> None:
    for keyword in keywords:
        schema.pop(keyword, None)

    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for child in properties.values():
            if isinstance(child, dict):
                _remove_schema_keywords(child, keywords)

    items = schema.get("items")
    if isinstance(items, dict):
        _remove_schema_keywords(items, keywords)

    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        _remove_schema_keywords(additional, keywords)

    for keyword in ("anyOf", "oneOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for child in variants:
                if isinstance(child, dict):
                    _remove_schema_keywords(child, keywords)


def _validate_instance(instance: object, schema: Mapping[str, Any], path: str) -> None:
    declared = schema.get("type")
    if declared is not None:
        declared_types = (declared,) if isinstance(declared, str) else tuple(declared)
        if not any(_matches_type(instance, item) for item in declared_types):
            expected = " or ".join(declared_types)
            raise _InstanceValidationError(f"{path} must be {expected}.")

    if "enum" in schema and not any(instance == candidate for candidate in schema["enum"]):
        raise _InstanceValidationError(f"{path} is not an allowed value.")
    if "const" in schema and instance != schema["const"]:
        raise _InstanceValidationError(f"{path} does not match the required constant.")

    if isinstance(instance, Mapping):
        properties = cast(Mapping[str, Mapping[str, Any]], schema.get("properties", {}))
        required = cast(Sequence[str], schema.get("required", ()))
        missing = [name for name in required if name not in instance]
        if missing:
            raise _InstanceValidationError(f"{path} is missing required field {missing[0]!r}.")

        additional = schema.get("additionalProperties", True)
        for key, child in instance.items():
            if not isinstance(key, str):
                raise _InstanceValidationError(f"{path} contains a non-string object key.")
            if key in properties:
                _validate_instance(child, properties[key], f"{path}.{key}")
            elif additional is False:
                raise _InstanceValidationError(f"{path} contains unknown field {key!r}.")
            elif isinstance(additional, Mapping):
                _validate_instance(child, additional, f"{path}.{key}")

    if isinstance(instance, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(instance) < minimum:
            raise _InstanceValidationError(f"{path} must contain at least {minimum} item(s).")
        if maximum is not None and len(instance) > maximum:
            raise _InstanceValidationError(f"{path} must contain at most {maximum} item(s).")
        if schema.get("uniqueItems") and any(
            instance[left] == instance[right]
            for left in range(len(instance))
            for right in range(left + 1, len(instance))
        ):
            raise _InstanceValidationError(f"{path} must contain unique items.")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, child in enumerate(instance):
                _validate_instance(child, items, f"{path}[{index}]")

    if isinstance(instance, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(instance) < minimum:
            raise _InstanceValidationError(f"{path} is shorter than {minimum} character(s).")
        if maximum is not None and len(instance) > maximum:
            raise _InstanceValidationError(f"{path} is longer than {maximum} character(s).")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, instance) is None:
            raise _InstanceValidationError(f"{path} does not match the required pattern.")

    if _is_number(instance):
        if not math.isfinite(float(cast(Any, instance))):
            raise _InstanceValidationError(f"{path} must be finite.")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and cast(Any, instance) < minimum:
            raise _InstanceValidationError(f"{path} must be at least {minimum}.")
        if maximum is not None and cast(Any, instance) > maximum:
            raise _InstanceValidationError(f"{path} must be at most {maximum}.")


def _matches_type(instance: object, declared_type: str) -> bool:
    if declared_type == "null":
        return instance is None
    if declared_type == "boolean":
        return isinstance(instance, bool)
    if declared_type == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if declared_type == "number":
        return _is_number(instance)
    if declared_type == "string":
        return isinstance(instance, str)
    if declared_type == "array":
        return isinstance(instance, list)
    if declared_type == "object":
        return isinstance(instance, Mapping)
    return False  # pragma: no cover - schema definition check rejects this


def _is_number(instance: object) -> bool:
    return isinstance(instance, (int, float)) and not isinstance(instance, bool)


__all__ = [
    "OPENAI_UNSUPPORTED_SCHEMA_KEYWORDS",
    "PLAN_SCHEMA_NAME",
    "PLAN_SCHEMA_PATH",
    "api_plan_schema",
    "load_plan_schema",
    "parse_plan_json",
    "plan_schema_for_api",
    "schema_for_openai_structured_outputs",
    "validate_plan",
    "validate_schema_instance",
]
