"""One typed contract for API schemas and local boundary validation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Annotated, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .errors import OpenAIResponseFormatError, UniversalSkillHostError


Text = Annotated[str, StringConstraints(min_length=1, max_length=32_000, pattern=r"\S")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2_000, pattern=r"\S")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_utf8(self):
        def check(value):
            if isinstance(value, str):
                value.encode("utf-8")
            elif isinstance(value, list):
                for item in value:
                    check(item)

        try:
            for value in self.__dict__.values():
                check(value)
        except UnicodeError:
            raise ValueError("Text must be valid UTF-8.") from None
        return self


class ComposerResponse(Contract):
    final_prompt: Text
    warnings: Annotated[list[ShortText], Field(max_length=64)]


T = TypeVar("T", bound=Contract)


# A conservative wire policy, not a list of universally unsupported keywords.
# OpenAI Structured Outputs, checked 2026-09-07: retain documented pattern,
# array and numeric constraints. Length limits remain enforced by Pydantic.
API_OMITTED_KEYWORDS = frozenset(
    {"$schema", "title", "default", "examples", "minLength", "maxLength"}
)
_SCHEMA_MAPS = ("properties", "$defs")
_SCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")


def _schema_children(node):
    for key in _SCHEMA_MAPS:
        yield from node.get(key, {}).values()
    for key in _SCHEMA_LISTS:
        yield from node.get(key, [])
    if isinstance(node.get("items"), dict):
        yield node["items"]


def assert_strict_objects(schema: dict) -> None:
    """Check object invariants, not the server's complete keyword support."""
    if schema.get("type") != "object" or "anyOf" in schema:
        raise UniversalSkillHostError("API schema root must be a single object.")

    def visit(node):
        if not isinstance(node, dict):
            raise UniversalSkillHostError("Invalid API schema structure.")
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                raise UniversalSkillHostError(
                    "API schema objects must forbid extra properties."
                )
            if set(node.get("properties", {})) != set(node.get("required", [])):
                raise UniversalSkillHostError("API schema properties must all be required.")
        for child in _schema_children(node):
            visit(child)

    visit(schema)


def api_schema(contract: type[Contract]) -> dict:
    """Project the single host contract without stripping user field names."""
    schema = deepcopy(contract.model_json_schema())

    def project(node):
        for key in API_OMITTED_KEYWORDS:
            node.pop(key, None)
        for child in _schema_children(node):
            project(child)

    project(schema)
    assert_strict_objects(schema)
    return schema


_SAFE_VALIDATION_TYPES = frozenset(
    {
        "missing",
        "extra_forbidden",
        "string_type",
        "string_too_short",
        "string_too_long",
        "string_pattern_mismatch",
        "list_type",
        "too_short",
        "too_long",
        "int_type",
        "float_type",
        "bool_type",
        "dict_type",
        "model_type",
        "literal_error",
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "finite_number",
        "value_error",
        "recursion_loop",
    }
)


def _validation_diagnostics(contract, error):
    # loc can contain an arbitrary extra JSON key. Never echo it or ctx/msg/input.
    known_fields = set()

    def visit(node):
        known_fields.update(node.get("properties", {}))
        for child in _schema_children(node):
            visit(child)

    visit(contract.model_json_schema())
    details = []
    for item in error.errors(include_input=False, include_url=False, include_context=False)[:8]:
        path = "$"
        for part in item["loc"][:12]:
            if type(part) is int and 0 <= part <= 1_000_000:
                path += f"[{part}]"
            elif isinstance(part, str) and part in known_fields:
                path += "." + part
            else:
                path += ".<unknown>"
        kind = item["type"] if item["type"] in _SAFE_VALIDATION_TYPES else "validation_error"
        details.append(f"{path}: {kind}")
    return "; ".join(details)


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_strict_json(text: object, *, maximum: int = 8 * 1024 * 1024) -> object:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Non-JSON constant")

    try:
        if not isinstance(text, str) or len(text.encode("utf-8")) > maximum:
            raise ValueError("Invalid JSON size")
        return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise UniversalSkillHostError(
            "Expected bounded, valid JSON without duplicate keys or non-finite numbers."
        ) from None


def validate(contract: type[T], value: object) -> T:
    try:
        return contract.model_validate(value)
    except ValidationError as error:
        details = _validation_diagnostics(contract, error)
    except RecursionError:
        details = "$: recursion_loop"
    # Outside the handler: do not retain the original error as exception context.
    raise UniversalSkillHostError(f"Invalid {contract.__name__}: {details}.")


def parse_response(contract: type[T], text: str) -> T:
    try:
        return validate(contract, parse_strict_json(text))
    except UniversalSkillHostError as error:
        details = str(error)  # Only the sanitized errors produced above.
    raise OpenAIResponseFormatError(
        f"OpenAI returned an invalid {contract.__name__}. {details} Retry or revise the request."
    )
