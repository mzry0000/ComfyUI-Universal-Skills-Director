"""Strict JSON Schemas for the versioned Director draft and runtime plan."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, NoReturn, cast

from .errors import OpenAIResponseFormatError
from .schemas import schema_for_openai_structured_outputs, validate_schema_instance


DIRECTOR_SCHEMA_VERSION = "1.0"
DIRECTOR_DRAFT_SCHEMA_NAME = "universal_director_draft"
DIRECTOR_PLAN_SCHEMA_NAME = "universal_director_plan"
_SCHEMA_DIRECTORY = Path(__file__).resolve().parent.parent / "schemas"
DIRECTOR_DRAFT_SCHEMA_PATH = _SCHEMA_DIRECTORY / "director_draft.schema.json"
DIRECTOR_PLAN_SCHEMA_PATH = _SCHEMA_DIRECTORY / "director_plan.schema.json"


class _DuplicateKeyError(ValueError):
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


def _assert_strict_objects(schema: Mapping[str, Any], path: str = "$") -> None:
    declared = schema.get("type")
    declared_types = {declared} if isinstance(declared, str) else set(declared or ())
    if "object" in declared_types:
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping):
            raise RuntimeError(f"Strict Director schema object at {path} has no properties.")
        if schema.get("additionalProperties") is not False:
            raise RuntimeError(
                f"Strict Director schema object at {path} must reject additional properties."
            )
        if not isinstance(required, list) or set(required) != set(properties):
            raise RuntimeError(
                f"Strict Director schema object at {path} must require every property."
            )

    properties = schema.get("properties", {})
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            if isinstance(child, Mapping):
                _assert_strict_objects(child, f"{path}.{name}")
    items = schema.get("items")
    if isinstance(items, Mapping):
        _assert_strict_objects(items, f"{path}[]")


@lru_cache(maxsize=2)
def _load_schema_template(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - broken installation
        raise RuntimeError(f"Director schema could not be read: {path}") from exc

    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, _DuplicateKeyError, ValueError) as exc:
        raise RuntimeError(f"Director schema is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Director schema root must be an object: {path}")

    # Instance parsing below invokes the shared dependency-free validator, while
    # this eager check enforces the strict-object contract needed by OpenAI.
    _assert_strict_objects(value)
    return value


def load_director_draft_schema() -> dict[str, Any]:
    """Return a fresh copy of the model-output draft schema."""

    return copy.deepcopy(_load_schema_template(DIRECTOR_DRAFT_SCHEMA_PATH))


def load_director_plan_schema() -> dict[str, Any]:
    """Return a fresh copy of the runtime ``DIRECTOR_PLAN`` schema."""

    return copy.deepcopy(_load_schema_template(DIRECTOR_PLAN_SCHEMA_PATH))


def api_director_draft_schema() -> dict[str, Any]:
    """Return the strict schema used in Responses API ``text.format``."""

    schema = schema_for_openai_structured_outputs(load_director_draft_schema())
    _assert_strict_objects(schema)
    return schema


def _parse_json_object(text: str, *, label: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise OpenAIResponseFormatError(f"{label} must be a JSON string.")
    if not text.strip():
        raise OpenAIResponseFormatError(f"OpenAI returned an empty {label.lower()}.")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise OpenAIResponseFormatError(
            f"OpenAI returned invalid {label.lower()} JSON at line {exc.lineno}, "
            f"column {exc.colno}."
        ) from exc
    except (_DuplicateKeyError, ValueError) as exc:
        raise OpenAIResponseFormatError(
            f"OpenAI returned invalid {label.lower()} JSON: {exc}."
        ) from exc
    if not isinstance(value, dict):
        raise OpenAIResponseFormatError(f"{label} JSON root must be an object.")
    return value


def validate_director_draft(value: object) -> dict[str, Any]:
    """Validate one already-decoded model draft."""

    validate_schema_instance(value, _load_schema_template(DIRECTOR_DRAFT_SCHEMA_PATH))
    return cast(dict[str, Any], value)


def validate_director_plan_schema(value: object) -> dict[str, Any]:
    """Validate the structural portion of one runtime Director plan."""

    validate_schema_instance(value, _load_schema_template(DIRECTOR_PLAN_SCHEMA_PATH))
    return cast(dict[str, Any], value)


def parse_director_draft_json(text: str) -> dict[str, Any]:
    """Parse and structurally validate one OpenAI Director draft."""

    return validate_director_draft(_parse_json_object(text, label="Director draft"))


def parse_director_plan_json(text: str) -> dict[str, Any]:
    """Parse and structurally validate one serialized runtime Director plan."""

    return validate_director_plan_schema(_parse_json_object(text, label="Director plan"))


__all__ = [
    "DIRECTOR_DRAFT_SCHEMA_NAME",
    "DIRECTOR_DRAFT_SCHEMA_PATH",
    "DIRECTOR_PLAN_SCHEMA_NAME",
    "DIRECTOR_PLAN_SCHEMA_PATH",
    "DIRECTOR_SCHEMA_VERSION",
    "api_director_draft_schema",
    "load_director_draft_schema",
    "load_director_plan_schema",
    "parse_director_draft_json",
    "parse_director_plan_json",
    "validate_director_draft",
    "validate_director_plan_schema",
]
