"""Explicit attempts, idempotent terminal results, and dependency-aware selection."""

from __future__ import annotations

from datetime import datetime, timezone

from ..contracts import fingerprint, validate
from .core import DirectorError, definition_hashes, validate_director_plan
from .models import Artifact, Ledger, Ticket

DIRECTOR_STATE_SCHEMA_VERSION = "2.0"
DirectorStateError = DirectorError


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_ledger(plan: object) -> dict:
    plan = validate_director_plan(plan)
    hashes = definition_hashes(plan)
    return {
        "schema_version": "2.0",
        "plan_id": plan["plan_id"],
        "plan_revision": plan["revision"],
        "plan_signature": plan["plan_signature"],
        "ledger_revision": 0,
        "entries": [
            {
                "stage_id": s["stage_id"],
                "definition_hash": hashes[s["stage_id"]],
                "attempts": [],
            }
            for s in plan["stages"]
        ],
    }


def _entries(ledger):
    return {entry["stage_id"]: entry for entry in ledger["entries"]}


def current(entry):
    return entry["attempts"][-1] if entry["attempts"] else None


def _execution_hash(stage, entry, entries):
    results = []
    for binding in sorted(stage["input_bindings"], key=lambda b: b["slot"]):
        if binding["source_type"] == "stage":
            attempt = current(entries[binding["source_id"]])
            if attempt is None or attempt["status"] != "succeeded":
                return None
            results.append(
                {"slot": binding["slot"], "fingerprint": attempt["result"]["fingerprint"]}
            )
    return fingerprint(
        {"definition_hash": entry["definition_hash"], "dependency_results": results}
    )


def validate_artifact(value):
    artifact = validate(Artifact, value).model_dump()
    expected = fingerprint(artifact["value"]) if artifact["kind"] == "text" else None
    file_hash = artifact.get("file_sha256", artifact["fingerprint"])
    if artifact["kind"] == "text" and "file_sha256" in artifact:
        raise DirectorError("Text results must not contain image file metadata.")
    if artifact["kind"] == "image" and artifact["value"] != file_hash + ".png":
        raise DirectorError("Image results must use a content-addressed internal filename.")
    if expected is not None and expected != artifact["fingerprint"]:
        raise DirectorError("Text result fingerprint mismatch.")
    return artifact


def validate_ledger_for_plan(plan: object, ledger: object) -> dict:
    plan = validate_director_plan(plan)
    checked = validate(Ledger, ledger).model_dump()
    if (checked["plan_id"], checked["plan_revision"], checked["plan_signature"]) != (
        plan["plan_id"],
        plan["revision"],
        plan["plan_signature"],
    ):
        raise DirectorError("Ledger belongs to a different plan revision.")
    entries = _entries(checked)
    hashes = definition_hashes(plan)
    if len(entries) != len(checked["entries"]) or set(entries) != set(hashes):
        raise DirectorError("Ledger stage membership mismatch.")
    ticket_ids = set()
    for stage in plan["stages"]:
        entry = entries[stage["stage_id"]]
        if entry["definition_hash"] != hashes[stage["stage_id"]]:
            raise DirectorError("Stale stage definition in ledger.")
        for number, attempt in enumerate(entry["attempts"], 1):
            if attempt["number"] != number or attempt["ticket_id"] in ticket_ids:
                raise DirectorError("Invalid or duplicate attempt identity.")
            ticket_ids.add(attempt["ticket_id"])
            status = attempt["status"]
            if (status == "succeeded") != (attempt["result"] is not None):
                raise DirectorError("Only succeeded attempts may contain a result.")
            if (status in {"failed", "cancelled"}) != (attempt["error"] is not None):
                raise DirectorError(
                    "Failed/cancelled attempts require a reason; other states must not have one."
                )
            if status == "running" and number != len(entry["attempts"]):
                raise DirectorError("Only the latest attempt may be running.")
            if attempt["result"] is not None:
                result = validate_artifact(attempt["result"])
                if result["kind"] != stage["media_kind"]:
                    raise DirectorError("Result media kind does not match the stage.")
        latest = current(entry)
        if latest and latest["status"] in {"running", "succeeded"}:
            if latest["execution_hash"] != _execution_hash(stage, entry, entries):
                raise DirectorError("Stage has stale or unavailable dependency results.")
    return checked


def reconcile_ledger(previous_plan, previous_ledger, plan):
    previous_plan = validate_director_plan(previous_plan)
    previous = validate_ledger_for_plan(previous_plan, previous_ledger)
    plan = validate_director_plan(plan)
    if (
        previous_plan["plan_id"] != plan["plan_id"]
        or plan["revision"] <= previous_plan["revision"]
    ):
        raise DirectorError("Reconciliation requires a newer revision of the same project.")
    ledger = create_ledger(plan)
    old = _entries(previous)
    for entry in ledger["entries"]:
        found = old.get(entry["stage_id"])
        if found and found["definition_hash"] == entry["definition_hash"]:
            entry["attempts"] = found["attempts"]
            attempt = current(entry)
            if attempt and attempt["status"] == "running":
                attempt.update(
                    status="cancelled", error="Plan revised; start an explicit retry."
                )
    ledger["ledger_revision"] = previous["ledger_revision"] + 1
    return validate_ledger_for_plan(plan, ledger)


def _ticket(plan, stage_id, attempt):
    return {
        "plan_id": plan["plan_id"],
        "plan_revision": plan["revision"],
        "plan_signature": plan["plan_signature"],
        "stage_id": stage_id,
        "attempt": attempt["number"],
        "execution_hash": attempt["execution_hash"],
        "ticket_id": attempt["ticket_id"],
    }


def select_work_item(plan, ledger, *, stage_id="", mode="next_ready"):
    plan = validate_director_plan(plan)
    ledger = validate_ledger_for_plan(plan, ledger)
    if mode not in {"next_ready", "retry", "resume"}:
        raise DirectorError("Use next_ready, retry, or resume selection mode.")
    if mode != "next_ready" and not stage_id:
        raise DirectorError("Retry/resume requires an explicit stage_id.")
    entries = _entries(ledger)
    for stage in sorted(plan["stages"], key=lambda s: s["order"]):
        if stage_id and stage["stage_id"] != stage_id:
            continue
        entry = entries[stage["stage_id"]]
        latest = current(entry)
        status = latest["status"] if latest else "pending"
        eligible = (
            status == "pending"
            if mode == "next_ready"
            else (status in {"failed", "cancelled"} if mode == "retry" else status == "running")
        )
        execution_hash = _execution_hash(stage, entry, entries)
        if not eligible or execution_hash is None:
            continue
        if mode != "resume":
            number = len(entry["attempts"]) + 1
            ticket_id = fingerprint(
                {
                    "plan_id": plan["plan_id"],
                    "revision": plan["revision"],
                    "stage_id": stage["stage_id"],
                    "execution_hash": execution_hash,
                    "attempt": number,
                }
            )
            latest = {
                "number": number,
                "ticket_id": ticket_id,
                "status": "running",
                "execution_hash": execution_hash,
                "result": None,
                "error": None,
            }
            entry["attempts"].append(latest)
            ledger["ledger_revision"] += 1
        return (
            stage,
            _ticket(plan, stage["stage_id"], latest),
            validate_ledger_for_plan(plan, ledger),
        )
    raise DirectorError(
        "No eligible stage. Check stage_id, dependency completion, and explicit retry/resume mode."
    )


def checked_ticket(plan, ledger, ticket):
    plan = validate_director_plan(plan)
    ledger = validate_ledger_for_plan(plan, ledger)
    ticket = validate(Ticket, ticket).model_dump()
    if (ticket["plan_id"], ticket["plan_revision"], ticket["plan_signature"]) != (
        plan["plan_id"],
        plan["revision"],
        plan["plan_signature"],
    ):
        raise DirectorError("Ticket belongs to a different plan revision.")
    entry = _entries(ledger).get(ticket["stage_id"])
    number = ticket["attempt"]
    if entry is None or number > len(entry["attempts"]):
        raise DirectorError("Attempt was not started by Select Work Item.")
    attempt = entry["attempts"][number - 1]
    if ticket != _ticket(plan, ticket["stage_id"], attempt):
        raise DirectorError("Attempt ticket mismatch.")
    return plan, ledger, ticket, entry, attempt


def record_result(plan, ledger, ticket, *, status, result=None, error=None):
    plan, ledger, ticket, entry, attempt = checked_ticket(plan, ledger, ticket)
    if status not in {"succeeded", "failed", "cancelled"}:
        raise DirectorError("Record only succeeded, failed or cancelled terminal results.")
    result = validate_artifact(result) if result is not None else None
    event = {"status": status, "result": result, "error": error}
    # Compare against this exact attempt before starting any retry or changing state.
    if all(attempt[key] == value for key, value in event.items()):
        return ledger
    if attempt["status"] != "running" or attempt is not current(entry):
        raise DirectorError(
            "This attempt is already finished. Retry explicitly; conflicting results are not overwrites."
        )
    attempt.update(event)
    ledger["ledger_revision"] += 1
    return validate_ledger_for_plan(plan, ledger)


def resolve_binding(plan, ledger, ticket, slot):
    plan, ledger, ticket, entry, attempt = checked_ticket(plan, ledger, ticket)
    if attempt is not current(entry) or attempt["status"] != "running":
        raise DirectorError("Resolve inputs for the current running attempt only.")
    stage = next(s for s in plan["stages"] if s["stage_id"] == ticket["stage_id"])
    binding = next((b for b in stage["input_bindings"] if b["slot"] == slot), None)
    if binding is None:
        raise DirectorError("The selected stage has no such input slot.")
    if binding["source_type"] == "input":
        asset = next(a for a in plan["assets"] if a["input_name"] == binding["source_id"])
        return {**binding, **asset}
    source = current(_entries(ledger)[binding["source_id"]])
    if source is None or source["status"] != "succeeded":
        raise DirectorError("Dependency result is not available.")
    return {**binding, "result": source["result"]}
