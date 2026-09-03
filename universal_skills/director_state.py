"""Pure, JSON-safe runtime state for Universal Skills Director plans.

The creative plan is intentionally immutable here.  A separate ledger records
execution attempts and durable output references without ever opening those
references or retaining ComfyUI tensors.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, NoReturn, Sequence, cast

from .errors import OpenAIResponseFormatError, UniversalSkillHostError
from .schemas import validate_schema_instance


DIRECTOR_LEDGER_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "director_ledger.schema.json"
)
DIRECTOR_STATE_SCHEMA_VERSION = "1.0"

DIRECTOR_STATUSES = (
    "pending",
    "approved",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "selected",
    "assembled",
    "stale",
)

_ACTIVE_ATTEMPT_STATUSES = frozenset({"queued", "running", "succeeded", "failed"})
_RETRY_ATTEMPT_STATUSES = frozenset({"approved", *_ACTIVE_ATTEMPT_STATUSES})
_TERMINAL_WITH_OUTPUT = frozenset({"succeeded", "selected", "assembled"})
_MAX_OUTPUT_REFS = 64
_MAX_OUTPUT_REF_CHARS = 4096
_MAX_ID_CHARS = 512
_MAX_ERROR_CHARS = 4096

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset(
        {"approved", "queued", "running", "succeeded", "failed", "cancelled", "stale"}
    ),
    "approved": frozenset(
        {"queued", "running", "succeeded", "failed", "cancelled", "stale"}
    ),
    "queued": frozenset({"running", "succeeded", "failed", "cancelled", "stale"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "stale"}),
    "succeeded": frozenset({"selected", "stale"}),
    "failed": frozenset(
        {"approved", "queued", "running", "succeeded", "cancelled", "stale"}
    ),
    "cancelled": frozenset({"approved", "queued", "running", "stale"}),
    "selected": frozenset({"assembled", "stale"}),
    "assembled": frozenset({"stale"}),
    "stale": frozenset(),
}


class DirectorStateError(UniversalSkillHostError):
    """Raised when a Director plan, ledger, or transition is inconsistent."""


class _DuplicateKeyError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerUpdate:
    """Result of applying one idempotent event to a Director ledger."""

    ledger: dict[str, Any]
    receipt: dict[str, Any]
    changed: bool
    warnings: tuple[str, ...] = ()


def utc_timestamp() -> str:
    """Return a compact UTC timestamp suitable for persisted JSON metadata."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: object) -> str:
    """Serialize a JSON-safe value deterministically and reject NaN/Infinity."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise DirectorStateError("Director state must contain only finite JSON values.") from exc


def content_signature(value: object) -> str:
    """Return a stable SHA-256 signature for a JSON-safe value."""

    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def parse_strict_json(text: str, *, label: str = "Director state") -> object:
    """Parse JSON while rejecting duplicate keys and non-JSON numbers."""

    if not isinstance(text, str) or not text.strip():
        raise DirectorStateError(f"{label} JSON is empty.")
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise DirectorStateError(
            f"{label} is invalid JSON at line {exc.lineno}, column {exc.colno}."
        ) from exc
    except (_DuplicateKeyError, ValueError) as exc:
        raise DirectorStateError(f"{label} is invalid JSON: {exc}.") from exc


@lru_cache(maxsize=1)
def _load_director_ledger_schema_template() -> dict[str, Any]:
    try:
        raw = DIRECTOR_LEDGER_SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - broken installation
        raise RuntimeError(
            f"Director ledger schema could not be read: {DIRECTOR_LEDGER_SCHEMA_PATH}"
        ) from exc
    value = parse_strict_json(raw, label="Director ledger schema")
    if not isinstance(value, dict):  # pragma: no cover - checked-in schema invariant
        raise RuntimeError("Director ledger schema root must be an object.")
    return cast(dict[str, Any], value)


def load_director_ledger_schema() -> dict[str, Any]:
    """Return a fresh copy of the checked-in ledger schema."""

    return copy.deepcopy(_load_director_ledger_schema_template())


def validate_ledger(value: object) -> dict[str, Any]:
    """Validate a decoded ledger and return it with a precise dict type."""

    try:
        validate_schema_instance(value, _load_director_ledger_schema_template())
    except (OpenAIResponseFormatError, ValueError) as exc:
        raise DirectorStateError(f"Director ledger is invalid: {exc}") from exc
    ledger = cast(dict[str, Any], value)
    _assert_unique_ledger_ids(ledger)
    _assert_receipt_history_matches_items(ledger)
    return ledger


def validate_ledger_for_plan(
    plan: Mapping[str, Any], ledger: object
) -> dict[str, Any]:
    """Validate a ledger and require an exact plan identity/revision match."""

    identity = _plan_identity(plan)
    checked = validate_ledger(ledger)
    mismatches = [
        name
        for name, expected in (
            ("plan_id", identity[0]),
            ("plan_revision", identity[1]),
            ("plan_signature", identity[2]),
        )
        if checked.get(name) != expected
    ]
    if mismatches:
        fields = ", ".join(mismatches)
        raise DirectorStateError(
            f"Director ledger is stale for this plan ({fields}). Rebuild or reload the session."
        )
    _assert_work_items_match_plan(plan, checked)
    return checked


def is_ledger_stale(plan: Mapping[str, Any], ledger: object) -> bool:
    """Return whether a structurally valid ledger targets another plan revision."""

    checked = validate_ledger(ledger)
    plan_id, revision, signature = _plan_identity(plan)
    if (
        checked["plan_id"] != plan_id
        or checked["plan_revision"] != revision
        or checked["plan_signature"] != signature
    ):
        return True
    try:
        _assert_work_items_match_plan(plan, checked)
    except DirectorStateError:
        return True
    return False


def create_ledger(
    plan: Mapping[str, Any],
    *,
    work_items: Iterable[Mapping[str, Any]] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Flatten a plan into a new pending ledger.

    ``work_items`` is injectable for pure tests.  In normal use the canonical
    Director core flattener is imported lazily, which avoids a state/core import
    cycle and guarantees matching shot signatures.
    """

    plan_id, revision, plan_signature = _plan_identity(plan)
    timestamp = _clean_timestamp(created_at or utc_timestamp(), "created_at")
    if work_items is None:
        try:
            from .director_core import flatten_work_items
        except ImportError as exc:  # pragma: no cover - packaging integration failure
            raise DirectorStateError(
                "Director core is unavailable; the initial ledger could not be built."
            ) from exc
        flattened = flatten_work_items(plan)
    else:
        flattened = list(work_items)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in flattened:
        if not isinstance(raw, Mapping):
            raise DirectorStateError("Every flattened Director work item must be an object.")
        item_id = _required_text(raw, "item_id", _MAX_ID_CHARS)
        if item_id in seen:
            raise DirectorStateError(f"Director work item ID is duplicated: {item_id!r}.")
        seen.add(item_id)
        shot_id = _required_text(raw, "shot_id", _MAX_ID_CHARS)
        variant_id = _required_text(raw, "variant_id", _MAX_ID_CHARS)
        stage_id = _required_text(raw, "stage_id", _MAX_ID_CHARS)
        if item_id != f"{shot_id}:{variant_id}:{stage_id}":
            raise DirectorStateError(
                f"Director work item {item_id!r} does not match its shot/variant/stage IDs."
            )
        item_plan_id = raw.get("plan_id")
        item_revision = raw.get("revision")
        item_signature = raw.get("plan_signature")
        if item_plan_id not in (None, plan_id):
            raise DirectorStateError(f"Director work item {item_id!r} belongs to another plan.")
        if item_revision not in (None, revision):
            raise DirectorStateError(f"Director work item {item_id!r} has a stale plan revision.")
        if item_signature not in (None, plan_signature):
            raise DirectorStateError(f"Director work item {item_id!r} has a stale plan signature.")
        dependency_signature = _required_signature(raw, "shot_signature")
        items.append(
            {
                "item_id": item_id,
                "shot_id": shot_id,
                "variant_id": variant_id,
                "stage_id": stage_id,
                "status": "pending",
                "attempt": 0,
                "dependency_signature": dependency_signature,
                "prompt_id": "",
                "workflow_id": "",
                "output_refs": [],
                "error_code": "",
                "error_message": "",
                "updated_at": timestamp,
            }
        )

    if not items:
        raise DirectorStateError("Director plan contains no executable work items.")
    ledger = {
        "schema_version": DIRECTOR_STATE_SCHEMA_VERSION,
        "plan_id": plan_id,
        "plan_revision": revision,
        "plan_signature": plan_signature,
        "ledger_revision": 0,
        "items": items,
        "receipts": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    return validate_ledger(ledger)


def reconcile_ledger(
    previous_plan: Mapping[str, Any],
    previous_ledger: object,
    revised_plan: Mapping[str, Any],
    *,
    reconciled_at: str | None = None,
) -> dict[str, Any]:
    """Carry unchanged execution state into a newer revision of the same plan.

    A work item is reusable only when both its stable item ID and its shot
    dependency signature still match.  Changed, added, and renamed work items
    therefore start pending, while removed items and their receipts disappear.
    """

    try:
        from .director_core import validate_director_plan
    except ImportError as exc:  # pragma: no cover - packaging integration failure
        raise DirectorStateError(
            "Director core is unavailable for ledger reconciliation."
        ) from exc

    try:
        checked_previous_plan = validate_director_plan(previous_plan)
        checked_revised_plan = validate_director_plan(revised_plan)
    except (OpenAIResponseFormatError, UniversalSkillHostError, ValueError) as exc:
        raise DirectorStateError(f"Director plan is invalid: {exc}") from exc

    previous_id, previous_revision, _ = _plan_identity(checked_previous_plan)
    revised_id, revised_revision, _ = _plan_identity(checked_revised_plan)
    if revised_id != previous_id:
        raise DirectorStateError(
            "Director ledger reconciliation requires revisions of the same plan_id."
        )
    if revised_revision <= previous_revision:
        raise DirectorStateError(
            "The revised Director plan revision must be greater than the previous revision."
        )

    checked_previous_ledger = validate_ledger_for_plan(
        checked_previous_plan, previous_ledger
    )
    timestamp = _clean_timestamp(reconciled_at or utc_timestamp(), "reconciled_at")
    reconciled = create_ledger(checked_revised_plan, created_at=timestamp)
    previous_items = {
        item["item_id"]: item for item in checked_previous_ledger["items"]
    }
    carried_ids: set[str] = set()
    for index, fresh_item in enumerate(reconciled["items"]):
        previous_item = previous_items.get(fresh_item["item_id"])
        if (
            previous_item is not None
            and previous_item["dependency_signature"]
            == fresh_item["dependency_signature"]
        ):
            reconciled["items"][index] = copy.deepcopy(previous_item)
            carried_ids.add(fresh_item["item_id"])

    reconciled["receipts"] = [
        copy.deepcopy(receipt)
        for receipt in checked_previous_ledger["receipts"]
        if receipt["item_id"] in carried_ids
        and receipt["dependency_signature"]
        == previous_items[receipt["item_id"]]["dependency_signature"]
    ]
    reconciled["ledger_revision"] = checked_previous_ledger["ledger_revision"] + 1
    reconciled["created_at"] = checked_previous_ledger["created_at"]
    reconciled["updated_at"] = timestamp
    return validate_ledger_for_plan(checked_revised_plan, reconciled)


def record_result(
    plan: Mapping[str, Any],
    ledger: object,
    work_item: Mapping[str, Any],
    *,
    status: str,
    output_ref: str = "",
    output_refs: Sequence[str] | None = None,
    prompt_id: str = "",
    workflow_id: str = "",
    error_code: str = "",
    error_message: str = "",
    recorded_at: str | None = None,
) -> LedgerUpdate:
    """Apply one validated, idempotent state transition.

    Output references are opaque strings.  They are bounded and persisted but
    never resolved, opened, downloaded, or interpreted as filesystem paths.
    """

    checked = validate_ledger_for_plan(plan, ledger)
    plan_id, revision, plan_signature = _plan_identity(plan)
    item_id = _required_text(work_item, "item_id", _MAX_ID_CHARS)
    if work_item.get("plan_id") != plan_id:
        raise DirectorStateError("The selected work item belongs to another Director plan.")
    if work_item.get("revision") != revision:
        raise DirectorStateError("The selected work item has a stale Director plan revision.")
    if work_item.get("plan_signature") != plan_signature:
        raise DirectorStateError("The selected work item has a stale Director plan signature.")
    requested_signature = _required_signature(work_item, "shot_signature")

    item_index = next(
        (index for index, item in enumerate(checked["items"]) if item["item_id"] == item_id),
        None,
    )
    if item_index is None:
        raise DirectorStateError(f"Director ledger has no work item {item_id!r}.")
    current = checked["items"][item_index]
    for key in ("shot_id", "variant_id", "stage_id"):
        if work_item.get(key) != current[key]:
            raise DirectorStateError(f"Director work item {item_id!r} has inconsistent {key}.")
    if current["dependency_signature"] != requested_signature:
        raise DirectorStateError(
            f"Director work item {item_id!r} is stale. Select it again from the current plan."
        )

    next_status = _clean_status(status)
    refs = _normalize_output_refs(output_ref=output_ref, output_refs=output_refs)
    clean_prompt_id = _bounded_text(prompt_id, "prompt_id", _MAX_ID_CHARS)
    clean_workflow_id = _bounded_text(workflow_id, "workflow_id", _MAX_ID_CHARS)
    clean_error_code = _bounded_text(error_code, "error_code", 256)
    clean_error_message = _bounded_text(error_message, "error_message", _MAX_ERROR_CHARS)
    timestamp = _clean_timestamp(recorded_at or utc_timestamp(), "recorded_at")

    attempt = _next_attempt(current["status"], next_status, current["attempt"])
    starts_retry = (
        current["status"] in {"failed", "cancelled"}
        and next_status in _RETRY_ATTEMPT_STATUSES
    )
    if not refs and next_status in _TERMINAL_WITH_OUTPUT and not starts_retry:
        refs = list(current["output_refs"])
    if next_status in _TERMINAL_WITH_OUTPUT and not refs:
        raise DirectorStateError(
            f"Director status {next_status!r} requires at least one durable output reference."
        )
    if next_status == "failed" and not (clean_error_code or clean_error_message):
        raise DirectorStateError("A failed Director result requires an error code or message.")

    receipt_event = {
        "item_id": item_id,
        "to_status": next_status,
        "attempt": attempt,
        "prompt_id": clean_prompt_id,
        "workflow_id": clean_workflow_id,
        "output_refs": refs,
        "error_code": clean_error_code,
        "error_message": clean_error_message,
        "dependency_signature": requested_signature,
    }
    receipt_id = _receipt_id(
        **receipt_event,
    )
    existing_receipt = next(
        (
            receipt
            for receipt in checked["receipts"]
            if receipt["receipt_id"] == receipt_id
        ),
        None,
    )
    if existing_receipt is not None:
        if _receipt_event(existing_receipt) != receipt_event:
            raise DirectorStateError(
                f"Director receipt ID collision detected for {receipt_id!r}."
            )
        return LedgerUpdate(
            ledger=copy.deepcopy(checked),
            receipt=copy.deepcopy(existing_receipt),
            changed=False,
            warnings=("This Director result receipt was already recorded.",),
        )

    current_status = current["status"]
    if next_status not in _ALLOWED_TRANSITIONS[current_status]:
        raise DirectorStateError(
            f"Director item {item_id!r} cannot transition from {current_status!r} "
            f"to {next_status!r}."
        )

    updated = copy.deepcopy(checked)
    target = updated["items"][item_index]
    target["status"] = next_status
    target["attempt"] = attempt
    target["prompt_id"] = clean_prompt_id or target["prompt_id"]
    target["workflow_id"] = clean_workflow_id or target["workflow_id"]
    if next_status in _RETRY_ATTEMPT_STATUSES and current_status in {"failed", "cancelled"}:
        target["output_refs"] = []
        target["error_code"] = ""
        target["error_message"] = ""
    if refs:
        target["output_refs"] = refs
    if next_status == "failed":
        target["error_code"] = clean_error_code
        target["error_message"] = clean_error_message
    elif next_status not in {"stale", "cancelled"}:
        target["error_code"] = ""
        target["error_message"] = ""
    target["updated_at"] = timestamp

    receipt = {
        "receipt_id": receipt_id,
        "item_id": item_id,
        "from_status": current_status,
        "to_status": next_status,
        "attempt": attempt,
        "dependency_signature": requested_signature,
        "prompt_id": clean_prompt_id,
        "workflow_id": clean_workflow_id,
        "output_refs": refs,
        "error_code": clean_error_code,
        "error_message": clean_error_message,
        "recorded_at": timestamp,
    }
    updated["receipts"].append(receipt)
    updated["ledger_revision"] += 1
    updated["updated_at"] = timestamp
    validate_ledger_for_plan(plan, updated)
    return LedgerUpdate(ledger=updated, receipt=receipt, changed=True)


def _plan_identity(plan: Mapping[str, Any]) -> tuple[str, int, str]:
    if not isinstance(plan, Mapping):
        raise DirectorStateError("Director plan must be an object.")
    if plan.get("schema_version") != DIRECTOR_STATE_SCHEMA_VERSION:
        raise DirectorStateError("Director plan schema_version must be '1.0'.")
    plan_id = _required_text(plan, "plan_id", _MAX_ID_CHARS)
    if not plan_id.startswith("dir_") or len(plan_id) != 24:
        raise DirectorStateError("Director plan_id is invalid.")
    revision = plan.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise DirectorStateError("Director plan revision must be a positive integer.")
    signature = _required_signature(plan, "plan_signature")
    canonical_json(plan)
    return plan_id, revision, signature


def _next_attempt(current_status: str, next_status: str, current_attempt: int) -> int:
    if next_status not in _RETRY_ATTEMPT_STATUSES:
        return current_attempt
    if current_attempt == 0 and next_status in _ACTIVE_ATTEMPT_STATUSES:
        return current_attempt + 1
    if current_status in {"failed", "cancelled"}:
        return current_attempt + 1
    return current_attempt


def _normalize_output_refs(
    *, output_ref: str, output_refs: Sequence[str] | None
) -> list[str]:
    if output_refs is not None and isinstance(output_refs, (str, bytes, bytearray)):
        raise DirectorStateError("output_refs must be an array of strings.")
    raw: list[object] = list(output_refs or ())
    if output_ref:
        raw.append(output_ref)
    if len(raw) > _MAX_OUTPUT_REFS:
        raise DirectorStateError(f"A Director result may contain at most {_MAX_OUTPUT_REFS} refs.")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise DirectorStateError(f"output_refs[{index}] must be a non-empty string.")
        clean = value.strip()
        if len(clean) > _MAX_OUTPUT_REF_CHARS:
            raise DirectorStateError(
                f"output_refs[{index}] exceeds {_MAX_OUTPUT_REF_CHARS} characters."
            )
        if clean not in seen:
            seen.add(clean)
            normalized.append(clean)
    return normalized


def _receipt_id(**event: object) -> str:
    digest = hashlib.sha256(canonical_json(event).encode("utf-8")).hexdigest()
    return f"receipt_{digest[:20]}"


def _receipt_event(receipt: Mapping[str, Any]) -> dict[str, object]:
    """Return the exact immutable event projection used by ``receipt_id``."""

    return {
        "item_id": receipt["item_id"],
        "to_status": receipt["to_status"],
        "attempt": receipt["attempt"],
        "prompt_id": receipt["prompt_id"],
        "workflow_id": receipt["workflow_id"],
        "output_refs": receipt["output_refs"],
        "error_code": receipt["error_code"],
        "error_message": receipt["error_message"],
        "dependency_signature": receipt["dependency_signature"],
    }


def _clean_status(value: object) -> str:
    if not isinstance(value, str) or value not in DIRECTOR_STATUSES:
        allowed = ", ".join(DIRECTOR_STATUSES)
        raise DirectorStateError(f"Director status must be one of: {allowed}.")
    return value


def _clean_timestamp(value: object, name: str) -> str:
    return _required_text({name: value}, name, 128)


def _required_signature(value: Mapping[str, Any], name: str) -> str:
    signature = _required_text(value, name, 71)
    if not signature.startswith("sha256:") or len(signature) != 71:
        raise DirectorStateError(f"Director {name} must be a sha256 signature.")
    suffix = signature[7:]
    if any(character not in "0123456789abcdef" for character in suffix):
        raise DirectorStateError(f"Director {name} must use lowercase hexadecimal.")
    return signature


def _required_text(value: Mapping[str, Any], name: str, maximum: int) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result.strip():
        raise DirectorStateError(f"Director {name} must be a non-empty string.")
    clean = result.strip()
    if len(clean) > maximum:
        raise DirectorStateError(f"Director {name} exceeds {maximum} characters.")
    return clean


def _bounded_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DirectorStateError(f"Director {name} must be a string.")
    clean = value.strip()
    if len(clean) > maximum:
        raise DirectorStateError(f"Director {name} exceeds {maximum} characters.")
    return clean


def _assert_unique_ledger_ids(ledger: Mapping[str, Any]) -> None:
    item_ids = [item["item_id"] for item in ledger["items"]]
    if len(item_ids) != len(set(item_ids)):
        raise DirectorStateError("Director ledger contains duplicate item IDs.")
    receipt_ids = [receipt["receipt_id"] for receipt in ledger["receipts"]]
    if len(receipt_ids) != len(set(receipt_ids)):
        raise DirectorStateError("Director ledger contains duplicate receipt IDs.")
    known_items = set(item_ids)
    item_by_id = {item["item_id"]: item for item in ledger["items"]}
    for receipt in ledger["receipts"]:
        if receipt["item_id"] not in known_items:
            raise DirectorStateError(
                f"Director receipt {receipt['receipt_id']!r} references an unknown item."
            )
        expected_receipt_id = _receipt_id(**_receipt_event(receipt))
        if receipt["receipt_id"] != expected_receipt_id:
            raise DirectorStateError(
                f"Director receipt {receipt['receipt_id']!r} failed its integrity check."
            )
        if (
            receipt["dependency_signature"]
            != item_by_id[receipt["item_id"]]["dependency_signature"]
        ):
            raise DirectorStateError(
                f"Director receipt {receipt['receipt_id']!r} is stale for its work item."
            )
    for item in ledger["items"]:
        expected_id = f"{item['shot_id']}:{item['variant_id']}:{item['stage_id']}"
        if item["item_id"] != expected_id:
            raise DirectorStateError(
                f"Director ledger item {item['item_id']!r} has inconsistent component IDs."
            )
        if item["status"] in _TERMINAL_WITH_OUTPUT and not item["output_refs"]:
            raise DirectorStateError(
                f"Director ledger item {item['item_id']!r} has no durable output reference."
            )
        if item["status"] in _ACTIVE_ATTEMPT_STATUSES and item["attempt"] < 1:
            raise DirectorStateError(
                f"Director ledger item {item['item_id']!r} has no execution attempt."
            )


def _assert_receipt_history_matches_items(ledger: Mapping[str, Any]) -> None:
    """Replay receipts and require them to explain every mutable item field."""

    replay: dict[str, dict[str, Any]] = {
        item["item_id"]: {
            "status": "pending",
            "attempt": 0,
            "prompt_id": "",
            "workflow_id": "",
            "output_refs": [],
            "error_code": "",
            "error_message": "",
            "updated_at": None,
        }
        for item in ledger["items"]
    }
    for receipt in ledger["receipts"]:
        state = replay[receipt["item_id"]]
        from_status = state["status"]
        to_status = receipt["to_status"]
        if receipt["from_status"] != from_status:
            raise DirectorStateError(
                f"Director receipt {receipt['receipt_id']!r} has a broken from_status chain."
            )
        if to_status not in _ALLOWED_TRANSITIONS[from_status]:
            raise DirectorStateError(
                f"Director receipt {receipt['receipt_id']!r} records an invalid transition."
            )
        expected_attempt = _next_attempt(from_status, to_status, state["attempt"])
        if receipt["attempt"] != expected_attempt:
            raise DirectorStateError(
                f"Director receipt {receipt['receipt_id']!r} has an inconsistent attempt."
            )

        refs = list(receipt["output_refs"])
        if to_status in _TERMINAL_WITH_OUTPUT and not refs:
            raise DirectorStateError(
                f"Director receipt {receipt['receipt_id']!r} has no durable output reference."
            )
        if to_status == "failed" and not (
            receipt["error_code"] or receipt["error_message"]
        ):
            raise DirectorStateError(
                f"Director receipt {receipt['receipt_id']!r} has no failure detail."
            )

        starts_retry = (
            from_status in {"failed", "cancelled"}
            and to_status in _RETRY_ATTEMPT_STATUSES
        )
        state["status"] = to_status
        state["attempt"] = expected_attempt
        if receipt["prompt_id"]:
            state["prompt_id"] = receipt["prompt_id"]
        if receipt["workflow_id"]:
            state["workflow_id"] = receipt["workflow_id"]
        if starts_retry:
            state["output_refs"] = []
            state["error_code"] = ""
            state["error_message"] = ""
        if refs:
            state["output_refs"] = refs
        if to_status == "failed":
            state["error_code"] = receipt["error_code"]
            state["error_message"] = receipt["error_message"]
        elif to_status not in {"stale", "cancelled"}:
            state["error_code"] = ""
            state["error_message"] = ""
        state["updated_at"] = receipt["recorded_at"]

    fields = (
        "status",
        "attempt",
        "prompt_id",
        "workflow_id",
        "output_refs",
        "error_code",
        "error_message",
    )
    for item in ledger["items"]:
        state = replay[item["item_id"]]
        for field in fields:
            if item[field] != state[field]:
                raise DirectorStateError(
                    f"Director ledger item {item['item_id']!r} is not explained by "
                    f"its receipt history ({field})."
                )
        if state["updated_at"] is not None and item["updated_at"] != state["updated_at"]:
            raise DirectorStateError(
                f"Director ledger item {item['item_id']!r} has an inconsistent updated_at."
            )


def _assert_work_items_match_plan(
    plan: Mapping[str, Any], ledger: Mapping[str, Any]
) -> None:
    try:
        from .director_core import flatten_work_items
    except ImportError as exc:  # pragma: no cover - packaging integration failure
        raise DirectorStateError("Director core is unavailable for ledger validation.") from exc
    expected = {item["item_id"]: item for item in flatten_work_items(plan)}
    actual = {item["item_id"]: item for item in ledger["items"]}
    if set(actual) != set(expected):
        raise DirectorStateError(
            "Director ledger work items do not match the current plan. Rebuild the ledger."
        )
    for item_id, state in actual.items():
        work_item = expected[item_id]
        if state["dependency_signature"] != work_item["shot_signature"]:
            raise DirectorStateError(
                f"Director ledger item {item_id!r} is stale for the current shot."
            )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-JSON numeric constant {value}")


__all__ = [
    "DIRECTOR_LEDGER_SCHEMA_PATH",
    "DIRECTOR_STATE_SCHEMA_VERSION",
    "DIRECTOR_STATUSES",
    "DirectorStateError",
    "LedgerUpdate",
    "canonical_json",
    "content_signature",
    "create_ledger",
    "is_ledger_stale",
    "load_director_ledger_schema",
    "parse_strict_json",
    "reconcile_ledger",
    "record_result",
    "utc_timestamp",
    "validate_ledger",
    "validate_ledger_for_plan",
]
