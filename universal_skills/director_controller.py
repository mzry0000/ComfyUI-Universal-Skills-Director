"""Pure validation and preview helpers for an external Director queue.

This module deliberately does not import ComfyUI and never submits a prompt.
The companion ``tools/director_queue.py`` process is the only component that
may perform an HTTP request, and it requires an explicit selector plus two
separate execution flags.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence


QUEUE_SCHEMA_VERSION = "1.0"
MAX_QUEUE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_QUEUE_ITEMS = 256
MAX_SELECTED_JOBS = 32
MAX_PROMPT_NODES = 10_000
MAX_IDENTIFIER_CHARS = 200
MAX_SELECTOR_CHARS = 240

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SELECTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "queue_id",
        "plan_id",
        "plan_revision",
        "allowed_selectors",
        "items",
    }
)
_ITEM_FIELDS = frozenset({"selector", "work_item_id", "prompt"})
_PROMPT_NODE_FIELDS = frozenset({"class_type", "inputs", "_meta"})


class DirectorQueueManifestError(ValueError):
    """Raised when an external queue manifest is unsafe or malformed."""


class _DuplicateKeyError(ValueError):
    pass


@dataclass(frozen=True)
class DirectorQueueItem:
    """One explicitly selectable ComfyUI API-format prompt graph."""

    selector: str
    work_item_id: str
    prompt: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class DirectorQueueManifest:
    """Validated immutable boundary used by the optional queue CLI."""

    schema_version: str
    queue_id: str
    plan_id: str
    plan_revision: int
    allowed_selectors: tuple[str, ...]
    items: tuple[DirectorQueueItem, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-safe representation."""

        return {
            "schema_version": self.schema_version,
            "queue_id": self.queue_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "allowed_selectors": list(self.allowed_selectors),
            "items": [
                {
                    "selector": item.selector,
                    "work_item_id": item.work_item_id,
                    "prompt": _json_copy(item.prompt),
                }
                for item in self.items
            ],
        }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> NoReturn:
    raise ValueError(value)


def _json_copy(value: Any) -> Any:
    """Copy a value through strict JSON without retaining caller aliases."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise DirectorQueueManifestError(
            "Queue manifest values must be finite, JSON-safe data."
        ) from None


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        raise DirectorQueueManifestError(
            f"{label} must contain exactly the documented fields."
        )


def _safe_identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_IDENTIFIER_CHARS
        or _IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise DirectorQueueManifestError(
            f"{label} must be a short identifier using letters, numbers, '.', '_', ':', or '-'."
        )
    return value


def _safe_selector(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SELECTOR_CHARS
        or _SELECTOR_PATTERN.fullmatch(value) is None
    ):
        raise DirectorQueueManifestError(
            "Each selector must be an exact, non-wildcard identifier."
        )
    return value


def _validate_prompt_graph(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or not value:
        raise DirectorQueueManifestError(
            "Each queue item prompt must be a non-empty ComfyUI API-format object."
        )
    if len(value) > MAX_PROMPT_NODES:
        raise DirectorQueueManifestError(
            f"A prompt graph cannot contain more than {MAX_PROMPT_NODES} nodes."
        )

    normalized: dict[str, dict[str, Any]] = {}
    for node_id, node in value.items():
        if (
            not isinstance(node_id, str)
            or not node_id
            or len(node_id) > MAX_IDENTIFIER_CHARS
            or any(ord(character) < 32 for character in node_id)
        ):
            raise DirectorQueueManifestError("Prompt node IDs must be short text values.")
        if not isinstance(node, Mapping):
            raise DirectorQueueManifestError(
                "Every prompt node must be an object with class_type and inputs."
            )
        unknown = set(node) - _PROMPT_NODE_FIELDS
        if unknown or "class_type" not in node or "inputs" not in node:
            raise DirectorQueueManifestError(
                "Every prompt node must contain class_type and inputs; only optional _meta is accepted."
            )
        class_type = node.get("class_type")
        if (
            not isinstance(class_type, str)
            or not class_type.strip()
            or len(class_type) > MAX_IDENTIFIER_CHARS
            or any(ord(character) < 32 for character in class_type)
        ):
            raise DirectorQueueManifestError(
                "Prompt class_type values must be non-empty short text."
            )
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            raise DirectorQueueManifestError("Prompt node inputs must be an object.")
        meta = node.get("_meta")
        if meta is not None and not isinstance(meta, Mapping):
            raise DirectorQueueManifestError("Prompt node _meta must be an object when present.")

        copied = _json_copy(dict(node))
        normalized[node_id] = copied

    return normalized


def normalize_queue_manifest(value: Any) -> DirectorQueueManifest:
    """Validate an in-memory queue manifest without changing the input."""

    if not isinstance(value, Mapping):
        raise DirectorQueueManifestError("Queue manifest root must be a JSON object.")
    _exact_fields(value, _ROOT_FIELDS, "Queue manifest")

    if value.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise DirectorQueueManifestError(
            f"Unsupported queue schema_version; expected {QUEUE_SCHEMA_VERSION!r}."
        )
    queue_id = _safe_identifier(value.get("queue_id"), "queue_id")
    plan_id = _safe_identifier(value.get("plan_id"), "plan_id")
    plan_revision = value.get("plan_revision")
    if isinstance(plan_revision, bool) or not isinstance(plan_revision, int) or plan_revision < 1:
        raise DirectorQueueManifestError("plan_revision must be an integer of at least 1.")

    raw_items = value.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise DirectorQueueManifestError("Queue manifest items must be a non-empty array.")
    if len(raw_items) > MAX_QUEUE_ITEMS:
        raise DirectorQueueManifestError(
            f"Queue manifest cannot contain more than {MAX_QUEUE_ITEMS} items."
        )

    items: list[DirectorQueueItem] = []
    item_selectors: set[str] = set()
    work_item_ids: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise DirectorQueueManifestError("Every queue item must be an object.")
        _exact_fields(raw_item, _ITEM_FIELDS, "Queue item")
        selector = _safe_selector(raw_item.get("selector"))
        work_item_id = _safe_identifier(raw_item.get("work_item_id"), "work_item_id")
        if selector in item_selectors:
            raise DirectorQueueManifestError("Queue item selectors must be unique.")
        if work_item_id in work_item_ids:
            raise DirectorQueueManifestError("Queue work_item_id values must be unique.")
        item_selectors.add(selector)
        work_item_ids.add(work_item_id)
        items.append(
            DirectorQueueItem(
                selector=selector,
                work_item_id=work_item_id,
                prompt=_validate_prompt_graph(raw_item.get("prompt")),
            )
        )

    raw_allowlist = value.get("allowed_selectors")
    if not isinstance(raw_allowlist, list) or not raw_allowlist:
        raise DirectorQueueManifestError(
            "allowed_selectors must be a non-empty array of exact selectors."
        )
    allowed_selectors: list[str] = []
    allowed_seen: set[str] = set()
    for raw_selector in raw_allowlist:
        selector = _safe_selector(raw_selector)
        if selector in allowed_seen:
            raise DirectorQueueManifestError("allowed_selectors must not contain duplicates.")
        if selector not in item_selectors:
            raise DirectorQueueManifestError(
                "Every allowed selector must identify one queue item."
            )
        allowed_seen.add(selector)
        allowed_selectors.append(selector)

    return DirectorQueueManifest(
        schema_version=QUEUE_SCHEMA_VERSION,
        queue_id=queue_id,
        plan_id=plan_id,
        plan_revision=plan_revision,
        allowed_selectors=tuple(allowed_selectors),
        items=tuple(items),
    )


def parse_queue_manifest_json(text: str) -> DirectorQueueManifest:
    """Parse strict JSON, rejecting duplicate keys and non-JSON numbers."""

    if not isinstance(text, str):
        raise DirectorQueueManifestError("Queue manifest must be UTF-8 JSON text.")
    try:
        byte_length = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise DirectorQueueManifestError("Queue manifest must be valid UTF-8 text.") from None
    if byte_length > MAX_QUEUE_MANIFEST_BYTES:
        raise DirectorQueueManifestError(
            f"Queue manifest exceeds the {MAX_QUEUE_MANIFEST_BYTES}-byte limit."
        )
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, _DuplicateKeyError, ValueError, RecursionError):
        raise DirectorQueueManifestError(
            "Queue manifest is not strict JSON or contains duplicate keys."
        ) from None
    return normalize_queue_manifest(value)


def load_queue_manifest(path: Path | str) -> DirectorQueueManifest:
    """Read one explicitly supplied manifest path for the external CLI."""

    candidate = Path(path)
    try:
        size = candidate.stat().st_size
        if size > MAX_QUEUE_MANIFEST_BYTES:
            raise DirectorQueueManifestError(
                f"Queue manifest exceeds the {MAX_QUEUE_MANIFEST_BYTES}-byte limit."
            )
        raw = candidate.read_bytes()
    except DirectorQueueManifestError:
        raise
    except OSError:
        raise DirectorQueueManifestError("Queue manifest could not be read.") from None
    if len(raw) > MAX_QUEUE_MANIFEST_BYTES:
        raise DirectorQueueManifestError(
            f"Queue manifest exceeds the {MAX_QUEUE_MANIFEST_BYTES}-byte limit."
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DirectorQueueManifestError("Queue manifest must be UTF-8 JSON.") from None
    return parse_queue_manifest_json(text)


def select_queue_items(
    manifest: DirectorQueueManifest,
    selectors: Sequence[str] = (),
) -> tuple[DirectorQueueItem, ...]:
    """Select allowed items in requested order, or all allowlisted items."""

    requested = tuple(selectors) if selectors else manifest.allowed_selectors
    if len(requested) > MAX_SELECTED_JOBS:
        raise DirectorQueueManifestError(
            f"Select at most {MAX_SELECTED_JOBS} queue jobs per invocation."
        )
    if len(set(requested)) != len(requested):
        raise DirectorQueueManifestError("Do not select the same work item twice.")
    allowlist = set(manifest.allowed_selectors)
    item_by_selector = {item.selector: item for item in manifest.items}
    selected: list[DirectorQueueItem] = []
    for raw_selector in requested:
        selector = _safe_selector(raw_selector)
        if selector not in allowlist:
            raise DirectorQueueManifestError(
                f"Selector {selector!r} is not present in allowed_selectors."
            )
        selected.append(item_by_selector[selector])
    return tuple(selected)


def queue_manifest_fingerprint(manifest: DirectorQueueManifest) -> str:
    """Return a stable SHA-256 for audit output without printing prompts."""

    canonical = json.dumps(
        manifest.as_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_queue_preview(
    manifest: DirectorQueueManifest,
    selectors: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a content-redacted preview suitable for CLI or UI display."""

    selected = select_queue_items(manifest, selectors)
    return {
        "mode": "preview",
        "queue_id": manifest.queue_id,
        "plan_id": manifest.plan_id,
        "plan_revision": manifest.plan_revision,
        "manifest_sha256": queue_manifest_fingerprint(manifest),
        "selected_count": len(selected),
        "items": [
            {
                "selector": item.selector,
                "work_item_id": item.work_item_id,
                "node_count": len(item.prompt),
                "class_types": sorted(
                    {str(node["class_type"]) for node in item.prompt.values()}
                ),
            }
            for item in selected
        ],
    }


def build_prompt_payload(item: DirectorQueueItem) -> dict[str, Any]:
    """Return the minimal ComfyUI ``POST /prompt`` JSON body."""

    return {"prompt": _json_copy(item.prompt)}


__all__ = [
    "DirectorQueueItem",
    "DirectorQueueManifest",
    "DirectorQueueManifestError",
    "MAX_PROMPT_NODES",
    "MAX_QUEUE_ITEMS",
    "MAX_QUEUE_MANIFEST_BYTES",
    "MAX_SELECTED_JOBS",
    "QUEUE_SCHEMA_VERSION",
    "build_prompt_payload",
    "build_queue_preview",
    "load_queue_manifest",
    "normalize_queue_manifest",
    "parse_queue_manifest_json",
    "queue_manifest_fingerprint",
    "select_queue_items",
]
