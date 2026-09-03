"""Safely load operator-authored Markdown or JSON prompt specifications."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Literal, Mapping, NoReturn, Sequence, cast

from .config import (
    MAX_SPECIFICATION_BYTES,
    MAX_SPECIFICATION_NAME_CHARS,
    MAX_SPECIFICATION_VERSION_CHARS,
)
from .errors import InvalidSpecificationError
from .local_config import CONFIG_PATH
from .specification_roots import allowed_specification_roots
from .types import Specification


SUPPORTED_SPECIFICATION_SUFFIXES = (".md", ".json")

_SOURCE_FORMATS = {".md": "markdown", ".json": "json"}
_FORBIDDEN_SOURCE_NAMES = frozenset({"ush_config.json", ".env"})
_MAX_SOURCE_FILE_CHARS = 255
_REQUIRED_FIELDS = frozenset(
    {"name", "version", "source_format", "content", "source_file", "source_path"}
)
_MARKDOWN_HEADING = re.compile(r"^\s*#\s+(.+?)\s*$")


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


def _source_path(source_path: object, *, require_absolute: bool) -> Path:
    """Validate a user-selected path without reading it or silently trimming it."""

    if isinstance(source_path, Path):
        source_text = str(source_path)
    elif isinstance(source_path, str):
        source_text = source_path
    else:
        raise InvalidSpecificationError(
            "Choose a .md or .json specification file path."
        )
    if not source_text:
        raise InvalidSpecificationError(
            "Choose a .md or .json specification file path."
        )
    if source_text != source_text.strip():
        raise InvalidSpecificationError(
            "Specification file paths cannot contain outer whitespace."
        )
    if "\x00" in source_text:
        raise InvalidSpecificationError(
            "Specification file paths cannot contain NUL characters."
        )
    _require_utf8(source_text, "source_file")
    try:
        candidate = Path(source_text)
    except (OSError, ValueError):
        raise InvalidSpecificationError(
            "The specification file path is invalid."
        ) from None
    if candidate.anchor.startswith("\\\\"):
        raise InvalidSpecificationError(
            "UNC and Windows device specification paths are not supported."
        )
    if require_absolute and not candidate.is_absolute():
        raise InvalidSpecificationError(
            "Specification source_path must be an absolute file path."
        )
    if candidate.suffix.lower() not in _SOURCE_FORMATS:
        raise InvalidSpecificationError("Only .md and .json specifications are supported.")
    source_name = candidate.name.casefold()
    if source_name in _FORBIDDEN_SOURCE_NAMES or source_name.startswith(".env."):
        raise InvalidSpecificationError(
            "Configuration and environment files cannot be used as specifications."
        )
    return candidate


def _source_file_name(source_file: object) -> str:
    """Validate one browser-supplied basename without treating it as a path."""

    if not isinstance(source_file, str):
        raise InvalidSpecificationError(
            "Dropped specification filename must be a .md or .json basename."
        )
    if (
        not source_file
        or source_file != source_file.strip()
        or len(source_file) > _MAX_SOURCE_FILE_CHARS
        or "\x00" in source_file
        or "/" in source_file
        or "\\" in source_file
        or any(ord(character) < 32 or ord(character) == 127 for character in source_file)
    ):
        raise InvalidSpecificationError(
            "Dropped specification filename must be a safe .md or .json basename."
        )
    _require_utf8(source_file, "source_file")
    candidate = _source_path(source_file, require_absolute=False)
    if candidate.is_absolute() or candidate.name != source_file:
        raise InvalidSpecificationError(
            "Dropped specification filename must be a safe .md or .json basename."
        )
    return source_file


def _resolve_allowed_roots(
    allowed_roots: Sequence[str | Path] | None,
) -> tuple[Path, ...]:
    """Resolve trusted directory boundaries supplied by the host integration."""

    configured_roots = (
        allowed_specification_roots() if allowed_roots is None else allowed_roots
    )
    if isinstance(configured_roots, (str, Path)) or not configured_roots:
        raise InvalidSpecificationError(
            "At least one allowed specification root directory is required."
        )
    resolved_roots: list[Path] = []
    for root in configured_roots:
        if not isinstance(root, (str, Path)):
            raise InvalidSpecificationError(
                "Allowed specification roots must be directory paths."
            )
        root_text = str(root)
        if not root_text or root_text != root_text.strip() or "\x00" in root_text:
            raise InvalidSpecificationError(
                "Allowed specification roots must be valid directory paths."
            )
        _require_utf8(root_text, "allowed root")
        try:
            candidate = Path(root_text)
        except (OSError, ValueError):
            raise InvalidSpecificationError(
                "An allowed specification root path is invalid."
            ) from None
        if candidate.anchor.startswith("\\\\"):
            raise InvalidSpecificationError(
                "UNC and Windows device specification roots are not supported."
            )
        try:
            resolved = candidate.resolve(strict=True)
            is_directory = resolved.is_dir()
        except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError):
            raise InvalidSpecificationError(
                "An allowed specification root is missing or cannot be resolved."
            ) from None
        if not is_directory:
            raise InvalidSpecificationError(
                "Allowed specification roots must resolve to directories."
            )
        if resolved not in resolved_roots:
            resolved_roots.append(resolved)
    return tuple(resolved_roots)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_specification_file(
    source_path: object,
    allowed_roots: Sequence[str | Path] | None,
) -> tuple[Path, tuple[Path, ...]]:
    """Resolve one explicit file path to a canonical regular-file identity."""

    candidate = _source_path(source_path, require_absolute=False)
    roots = _resolve_allowed_roots(allowed_roots)
    if candidate.is_absolute():
        candidates = (candidate,)
    else:
        # Floyo and ComfyUI workers need relative paths to be portable across
        # hosts. Resolve them against the authorized roots in their documented
        # priority order, never against the process current working directory.
        candidates = tuple(root / candidate for root in roots)

    resolved: Path | None = None
    for unresolved in candidates:
        try:
            current = unresolved.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError):
            continue
        if not any(_is_within(current, root) for root in roots):
            raise InvalidSpecificationError(
                "The selected specification file is outside the allowed roots."
            )
        resolved = current
        break
    if resolved is None:
        raise InvalidSpecificationError(
            "The selected specification file was not found in the allowed roots."
        )
    if resolved.suffix.lower() not in _SOURCE_FORMATS:
        raise InvalidSpecificationError(
            "The resolved specification file must have a .md or .json extension."
        )
    try:
        is_file = resolved.is_file()
    except OSError:
        is_file = False
    if not is_file:
        raise InvalidSpecificationError(
            "The selected specification path must resolve to a regular file."
        )
    return resolved, roots


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _decode_specification_bytes(raw: bytes, source_file: str) -> str:
    if len(raw) > MAX_SPECIFICATION_BYTES:
        raise InvalidSpecificationError(
            f"Specification {source_file!r} exceeds the {MAX_SPECIFICATION_BYTES}-byte limit."
        )
    try:
        # UTF-8 with a BOM is still accepted for Windows editor compatibility.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidSpecificationError(
            f"Specification {source_file!r} must be UTF-8 text."
        ) from None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise InvalidSpecificationError(
            f"Specification {source_file!r} must not be empty."
        )
    return normalized


def _read_utf8(
    candidate: Path,
    source_file: str,
    allowed_roots: Sequence[Path],
) -> str:
    try:
        with candidate.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise InvalidSpecificationError(
                    f"Specification {source_file!r} must be a regular file."
                )
            if opened.st_nlink > 1:
                raise InvalidSpecificationError(
                    f"Specification {source_file!r} must not be a hard-linked file."
                )
            if opened.st_size > MAX_SPECIFICATION_BYTES:
                raise InvalidSpecificationError(
                    f"Specification {source_file!r} exceeds the "
                    f"{MAX_SPECIFICATION_BYTES}-byte limit."
                )

            # Re-resolve after opening. If an ancestor was exchanged for a
            # symlink/junction between the initial containment check and open,
            # either containment or the opened-file identity will now differ.
            canonical = candidate.resolve(strict=True)
            inspected = canonical.stat()
            if not any(_is_within(canonical, root) for root in allowed_roots):
                raise InvalidSpecificationError(
                    "The selected specification file changed outside the allowed roots."
                )
            if canonical != candidate:
                raise InvalidSpecificationError(
                    f"Specification {source_file!r} changed while it was being opened."
                )
            if not _same_file_identity(opened, inspected):
                raise InvalidSpecificationError(
                    f"Specification {source_file!r} changed while it was being opened."
                )
            if CONFIG_PATH.exists() and os.path.samefile(canonical, CONFIG_PATH):
                raise InvalidSpecificationError(
                    "The local configuration file cannot be used as a specification."
                )
            raw = source.read(MAX_SPECIFICATION_BYTES + 1)
    except InvalidSpecificationError:
        raise
    except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError):
        raise InvalidSpecificationError(
            f"Specification {source_file!r} could not be read."
        ) from None
    return _decode_specification_bytes(raw, source_file)


def _parse_json_object(text: str, source_file: str) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise InvalidSpecificationError(
            f"Specification {source_file!r} is not valid JSON at line {exc.lineno}, "
            f"column {exc.colno}."
        ) from None
    except (_DuplicateKeyError, ValueError):
        raise InvalidSpecificationError(
            f"Specification {source_file!r} contains duplicate keys or non-JSON constants."
        ) from None
    except RecursionError:
        raise InvalidSpecificationError(
            f"Specification {source_file!r} contains JSON nested too deeply."
        ) from None
    if not isinstance(value, dict):
        raise InvalidSpecificationError(
            f"JSON specification {source_file!r} must have an object at its root."
        )
    return value


def _normalized_name(value: object, fallback: str) -> str:
    if value is None:
        normalized = fallback.strip()
    elif isinstance(value, str):
        normalized = value.strip()
    else:
        raise InvalidSpecificationError("Specification name must be a string when provided.")
    if not normalized:
        raise InvalidSpecificationError("Specification name must not be empty.")
    if len(normalized) > MAX_SPECIFICATION_NAME_CHARS:
        raise InvalidSpecificationError(
            f"Specification name exceeds {MAX_SPECIFICATION_NAME_CHARS} characters."
        )
    _require_utf8(normalized, "name")
    return normalized


def _normalized_version(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidSpecificationError(
            "Specification version must be a string or integer when provided."
        )
    normalized = str(value).strip()
    if len(normalized) > MAX_SPECIFICATION_VERSION_CHARS:
        raise InvalidSpecificationError(
            f"Specification version exceeds {MAX_SPECIFICATION_VERSION_CHARS} characters."
        )
    _require_utf8(normalized, "version")
    return normalized


def _require_utf8(value: str, field_name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidSpecificationError(
            f"Specification {field_name} must be valid UTF-8 text."
        ) from None


def _markdown_name(text: str, fallback: str) -> str:
    for line in text.splitlines():
        match = _MARKDOWN_HEADING.match(line)
        if match:
            return _normalized_name(match.group(1), fallback)
    return _normalized_name(None, fallback)


def _specification_from_text(
    source_file: str,
    text: str,
    *,
    source_path: str | None,
) -> Specification:
    source_file = _source_file_name(source_file)
    suffix = Path(source_file).suffix.lower()
    source_format = _SOURCE_FORMATS[suffix]
    if source_format == "json":
        value = _parse_json_object(text, source_file)
        name = _normalized_name(value.get("name"), Path(source_file).stem)
        version = _normalized_version(value.get("version"))
    else:
        name = _markdown_name(text, Path(source_file).stem)
        version = ""
    return Specification(
        name=name,
        version=version,
        source_format=cast(Literal["markdown", "json"], source_format),
        content=text,
        source_file=source_file,
        source_path=source_path,
    )


def load_specification(
    source_path: object,
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> Specification:
    """Load an explicitly selected local file for the ``USH_SPEC`` boundary."""

    candidate, roots = _resolve_specification_file(source_path, allowed_roots)
    source_file = str(candidate)
    display_name = candidate.name
    text = _read_utf8(candidate, display_name, roots)
    return _specification_from_text(
        display_name,
        text,
        source_path=source_file,
    )


def load_specification_content(source_file: object, content: object) -> Specification:
    """Load UTF-8 text embedded by the browser without touching worker storage."""

    display_name = _source_file_name(source_file)
    if not isinstance(content, str):
        raise InvalidSpecificationError(
            "Dropped specification content must be UTF-8 text."
        )
    if len(content) > MAX_SPECIFICATION_BYTES:
        raise InvalidSpecificationError(
            f"Specification {display_name!r} exceeds the "
            f"{MAX_SPECIFICATION_BYTES}-byte limit."
        )
    try:
        raw = content.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidSpecificationError(
            f"Specification {display_name!r} must be UTF-8 text."
        ) from None
    text = _decode_specification_bytes(raw, display_name)
    return _specification_from_text(display_name, text, source_path=None)


def load_specification_selection(
    spec_file: object,
    uploaded_filename: object = "",
    uploaded_content: object = "",
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> Specification:
    """Select exactly one browser-embedded or trusted-path Specification source."""

    if not isinstance(spec_file, (str, Path)):
        raise InvalidSpecificationError(
            "The trusted path input must be a string, even when it is blank."
        )
    if not isinstance(uploaded_filename, str) or not isinstance(uploaded_content, str):
        raise InvalidSpecificationError(
            "Dropped specification filename and content must be strings."
        )
    has_path = bool(str(spec_file))
    has_upload_name = uploaded_filename != ""
    has_upload_content = uploaded_content != ""
    if has_upload_name or has_upload_content:
        if has_path:
            raise InvalidSpecificationError(
                "Use either a dropped specification or a trusted server path, not both."
            )
        if not has_upload_name or not has_upload_content:
            raise InvalidSpecificationError(
                "Dropped specification filename and content must both be provided."
            )
        return load_specification_content(uploaded_filename, uploaded_content)
    return load_specification(spec_file, allowed_roots=allowed_roots)


def normalize_specification(value: object) -> Specification:
    """Revalidate a JSON-safe ``USH_SPEC`` value at the Planner boundary."""

    if not isinstance(value, Mapping):
        raise InvalidSpecificationError("Specification input must come from Load Specification.")
    unknown = set(value) - _REQUIRED_FIELDS
    missing = _REQUIRED_FIELDS - set(value)
    if missing or unknown:
        raise InvalidSpecificationError(
            "Specification input has an invalid shape; reconnect Load Specification."
        )

    try:
        source_file = _source_file_name(value.get("source_file"))
    except InvalidSpecificationError:
        raise InvalidSpecificationError(
            "Specification source_file must be a safe .md or .json file name."
        ) from None

    source_path_value = value.get("source_path")
    if source_path_value is not None and not isinstance(source_path_value, str):
        raise InvalidSpecificationError(
            "Specification source_path must be an absolute file path or null."
        )
    if source_path_value is not None:
        source_path = _source_path(source_path_value, require_absolute=True)
        if source_path.name != source_file:
            raise InvalidSpecificationError(
                "Specification source_file does not match source_path."
            )

    source_format = value.get("source_format")
    expected_format = _SOURCE_FORMATS[Path(source_file).suffix.lower()]
    if source_format != expected_format:
        raise InvalidSpecificationError(
            "Specification source_format does not match its file extension."
        )
    name = _normalized_name(value.get("name"), Path(source_file).stem)
    version = _normalized_version(value.get("version"))
    content = value.get("content")
    if not isinstance(content, str):
        raise InvalidSpecificationError("Specification content must be non-empty text.")
    if len(content) > MAX_SPECIFICATION_BYTES:
        raise InvalidSpecificationError(
            f"Specification {source_file!r} exceeds the "
            f"{MAX_SPECIFICATION_BYTES}-byte limit."
        )
    try:
        normalized_content = _decode_specification_bytes(
            content.encode("utf-8"), source_file
        )
    except UnicodeEncodeError:
        raise InvalidSpecificationError(
            "Specification content must be valid UTF-8 text."
        ) from None
    if source_format == "json":
        _parse_json_object(normalized_content, source_file)

    return Specification(
        name=name,
        version=version,
        source_format=cast(Literal["markdown", "json"], source_format),
        content=normalized_content,
        source_file=source_file,
        source_path=source_path_value,
    )


def resolve_specification(
    specification: object,
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> Specification:
    """Resolve one validated browser snapshot or reload one trusted disk source.

    ComfyUI custom types are a UI connection contract, not a security boundary.
    Path-backed values are always reloaded within trusted host-supplied roots.
    Browser-embedded values have no worker path and are revalidated in memory.
    """

    normalized = normalize_specification(specification)
    if normalized["source_path"] is None:
        return normalized
    resolved = load_specification(
        normalized["source_path"], allowed_roots=allowed_roots
    )
    if resolved["source_file"] != normalized["source_file"]:
        raise InvalidSpecificationError(
            "Specification source_file does not match the selected disk file."
        )
    return resolved


def specification_fingerprint(
    source_path: object,
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> str:
    """Return a stable content hash so ComfyUI invalidates edited specs."""

    specification = load_specification(source_path, allowed_roots=allowed_roots)
    return _specification_value_fingerprint(specification)


def _specification_value_fingerprint(specification: object) -> str:
    normalized = normalize_specification(specification)
    canonical = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def specification_selection_fingerprint(
    spec_file: object,
    uploaded_filename: object = "",
    uploaded_content: object = "",
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> str:
    """Hash the selected browser content or trusted path for ComfyUI caching."""

    specification = load_specification_selection(
        spec_file,
        uploaded_filename,
        uploaded_content,
        allowed_roots=allowed_roots,
    )
    return _specification_value_fingerprint(specification)


def specification_metadata(specification: object) -> dict[str, str]:
    """Return content-free metadata without exposing a host filesystem path."""

    normalized = normalize_specification(specification)
    return {
        "name": normalized["name"],
        "version": normalized["version"],
        "source_format": normalized["source_format"],
        "source_file": normalized["source_file"],
    }


def specification_signature_payload(specification: object) -> dict[str, str]:
    """Return portable identity data for signatures without a host path."""

    normalized = normalize_specification(specification)
    return {
        "name": normalized["name"],
        "version": normalized["version"],
        "source_format": normalized["source_format"],
        "source_file": normalized["source_file"],
        "content": normalized["content"],
    }


__all__ = [
    "SUPPORTED_SPECIFICATION_SUFFIXES",
    "load_specification",
    "load_specification_content",
    "load_specification_selection",
    "normalize_specification",
    "resolve_specification",
    "specification_fingerprint",
    "specification_selection_fingerprint",
    "specification_metadata",
    "specification_signature_payload",
]
