"""Optional file-based deployment configuration next to the node package.

``ush_config.json`` lets operators avoid putting secrets into launch scripts.
Environment variables always take precedence so shared deployments keep
process-level control. The file location is fixed to the package directory and
is never taken from node inputs or workflow JSON.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NoReturn, Union

from .errors import LocalConfigError


CONFIG_FILENAME = "ush_config.json"
CONFIG_PATH = Path(__file__).resolve().parent.parent / CONFIG_FILENAME

_ALLOWED_KEYS = frozenset(
    {"openai_api_key", "timeout_seconds", "specification_roots", "enable_director"}
)
_MAX_SPECIFICATION_ROOTS = 32
_MAX_PATH_CHARS = 4096


@dataclass(frozen=True)
class LocalConfig:
    """Values found in ``ush_config.json``; ``None`` means not configured."""

    api_key: Union[str, None] = None
    timeout_seconds: Union[float, None] = None
    specification_roots: tuple[str, ...] = ()
    enable_director: bool = False


_EMPTY_CONFIG = LocalConfig()


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


def load_local_config(path: Union[Path, None] = None) -> LocalConfig:
    """Read and validate ``ush_config.json``; a missing file is not an error.

    Values are validated for type and shape only. Secrets and file contents
    are never included in raised error messages.
    """

    config_path = CONFIG_PATH if path is None else path
    try:
        # utf-8-sig tolerates the BOM that Windows editors often add.
        raw = config_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return _EMPTY_CONFIG
    except OSError:
        raise LocalConfigError(
            f"{CONFIG_FILENAME} exists but could not be read. Check the file "
            "permissions of the node folder."
        ) from None

    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise LocalConfigError(
            f"{CONFIG_FILENAME} is not valid JSON at line {exc.lineno}, column {exc.colno}."
        ) from None
    except (_DuplicateKeyError, ValueError):
        raise LocalConfigError(
            f"{CONFIG_FILENAME} contains duplicate keys or non-JSON constants."
        ) from None

    if not isinstance(value, Mapping):
        raise LocalConfigError(f"The {CONFIG_FILENAME} root must be a JSON object.")

    unknown = sorted(
        str(key) for key in value if not isinstance(key, str) or key not in _ALLOWED_KEYS
    )
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_KEYS))
        raise LocalConfigError(
            f"{CONFIG_FILENAME} contains unknown key(s): {', '.join(unknown)}. "
            f"Allowed keys: {allowed}."
        )

    return LocalConfig(
        api_key=_normalized_api_key(value.get("openai_api_key")),
        timeout_seconds=_normalized_timeout(value.get("timeout_seconds")),
        specification_roots=_normalized_specification_roots(value.get("specification_roots")),
        enable_director=_normalized_enable_director(value.get("enable_director", False)),
    )


def _normalized_enable_director(value: object) -> bool:
    if type(value) is not bool:
        raise LocalConfigError("enable_director must be true or false.")
    return value


def _normalized_api_key(value: object) -> Union[str, None]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LocalConfigError(f"openai_api_key in {CONFIG_FILENAME} must be a string.")
    normalized = value.strip()
    # Blank means "template placeholder left unfilled": treat as unset.
    return normalized or None


def _normalized_timeout(value: object) -> Union[float, None]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalConfigError(
            f"timeout_seconds in {CONFIG_FILENAME} must be a number of seconds."
        )
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise LocalConfigError(
            f"timeout_seconds in {CONFIG_FILENAME} must be a positive number."
        )
    return normalized


def _normalized_specification_roots(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise LocalConfigError(
            f"specification_roots in {CONFIG_FILENAME} must be an array of paths."
        )
    if len(value) > _MAX_SPECIFICATION_ROOTS:
        raise LocalConfigError(
            f"specification_roots in {CONFIG_FILENAME} accepts at most "
            f"{_MAX_SPECIFICATION_ROOTS} paths."
        )

    normalized_roots: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str):
            raise LocalConfigError(
                f"specification_roots item {index} in {CONFIG_FILENAME} must be a string."
            )
        normalized = item.strip()
        if (
            not normalized
            or normalized != item
            or "\x00" in normalized
            or len(normalized) > _MAX_PATH_CHARS
        ):
            raise LocalConfigError(
                f"specification_roots item {index} in {CONFIG_FILENAME} is not a valid path."
            )
        try:
            normalized.encode("utf-8")
        except UnicodeEncodeError:
            raise LocalConfigError(
                f"specification_roots item {index} in {CONFIG_FILENAME} is not valid UTF-8."
            ) from None
        identity = normalized.casefold()
        if identity in seen:
            raise LocalConfigError(
                f"specification_roots in {CONFIG_FILENAME} contains a duplicate path."
            )
        seen.add(identity)
        normalized_roots.append(normalized)
    return tuple(normalized_roots)


__all__ = ["CONFIG_FILENAME", "CONFIG_PATH", "LocalConfig", "load_local_config"]
