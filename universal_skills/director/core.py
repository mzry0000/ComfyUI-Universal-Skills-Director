"""Pure plan validation, revision and execution fingerprints."""

from __future__ import annotations

from uuid import uuid4

from ..config import IMAGE_TARGETS, TEXT_TARGETS
from ..contracts import fingerprint, validate
from ..errors import UniversalSkillHostError
from .models import DirectorDraft, DirectorPlan


class DirectorError(UniversalSkillHostError):
    pass


def validate_draft(value: object) -> dict:
    draft = validate(DirectorDraft, value).model_dump()
    stages = draft["stages"]
    by_id = {stage["stage_id"]: stage for stage in stages}
    if len(by_id) != len(stages) or len({s["order"] for s in stages}) != len(stages):
        raise DirectorError("Stage IDs and display orders must be unique.")
    for stage in stages:
        allowed = IMAGE_TARGETS if stage["media_kind"] == "image" else TEXT_TARGETS
        if stage["target_profile"] not in allowed:
            raise DirectorError("Stage media_kind and target_profile do not match.")
        bindings = stage["input_bindings"]
        if len({b["slot"] for b in bindings}) != len(bindings):
            raise DirectorError("Each stage input slot can be bound only once.")
        if len({p["name"] for p in stage["parameters"]}) != len(stage["parameters"]):
            raise DirectorError("Stage parameter names must be unique.")
        for binding in bindings:
            if binding["source_type"] == "input":
                if binding["source_id"] not in {
                    "image1",
                    "image2",
                    "image3",
                    "image4",
                } or not binding["slot"].startswith("image"):
                    raise DirectorError(
                        "External bindings must refer to a supplied IMAGE input."
                    )
            else:
                source = by_id.get(binding["source_id"])
                if source is None or not binding["slot"].startswith(source["media_kind"]):
                    raise DirectorError(
                        "Stage binding has a missing source or wrong media kind."
                    )

    visiting, visited = set(), set()

    def visit(stage_id):
        if stage_id in visiting:
            raise DirectorError("Stage dependencies contain a cycle.")
        if stage_id in visited:
            return
        visiting.add(stage_id)
        for binding in by_id[stage_id]["input_bindings"]:
            if binding["source_type"] == "stage":
                visit(binding["source_id"])
        visiting.remove(stage_id)
        visited.add(stage_id)

    for stage_id in by_id:
        visit(stage_id)
    draft["stages"] = sorted(stages, key=lambda s: s["order"])
    return draft


def plan_signature(plan: dict) -> str:
    return fingerprint({key: value for key, value in plan.items() if key != "plan_signature"})


def validate_director_plan(value: object) -> dict:
    if not isinstance(value, dict) or value.get("schema_version") != "2.0":
        raise DirectorError(
            "Director v2 requires a v2 Plan. Keep v1 sessions with the old release; create a new v2 plan."
        )
    plan = validate(DirectorPlan, value).model_dump()
    validate_draft({key: plan[key] for key in DirectorDraft.model_fields})
    if plan["plan_signature"] != plan_signature(plan):
        raise DirectorError("Plan signature mismatch; replan instead of editing a signed Plan.")
    assets = {item["input_name"] for item in plan["assets"]}
    if len(assets) != len(plan["assets"]):
        raise DirectorError("Duplicate input assets.")
    for stage in plan["stages"]:
        if any(
            b["source_type"] == "input" and b["source_id"] not in assets
            for b in stage["input_bindings"]
        ):
            raise DirectorError("Plan references an image that was not supplied.")
    return plan


def hydrate_plan(
    draft: object,
    *,
    assets: list[dict],
    specification_fingerprint: str,
    brief_fingerprint: str,
    previous_plan: object | None = None,
) -> dict:
    normalized = validate_draft(draft)
    previous = validate_director_plan(previous_plan) if previous_plan is not None else None
    existing = {stage["stage_id"] for stage in previous["stages"]} if previous else set()
    replacements = {}
    for stage in normalized["stages"]:
        stage_id = stage["stage_id"]
        if stage_id in existing:
            continue
        if not stage_id.startswith("new-"):
            raise DirectorError(
                "New stages must use unique new-* IDs; revisions must preserve existing stage IDs."
            )
        replacements[stage_id] = "stage-" + uuid4().hex
    for stage in normalized["stages"]:
        stage["stage_id"] = replacements.get(stage["stage_id"], stage["stage_id"])
        for binding in stage["input_bindings"]:
            if binding["source_type"] == "stage":
                binding["source_id"] = replacements.get(
                    binding["source_id"], binding["source_id"]
                )
    plan = {
        **normalized,
        "schema_version": "2.0",
        "plan_id": previous["plan_id"] if previous else "project-" + uuid4().hex,
        "revision": previous["revision"] + 1 if previous else 1,
        "specification_fingerprint": specification_fingerprint,
        "brief_fingerprint": brief_fingerprint,
        "assets": assets,
    }
    plan["plan_signature"] = plan_signature(plan)
    return validate_director_plan(plan)


def definition_hashes(plan: dict) -> dict[str, str]:
    """Hash only execution inputs. Metadata, list ordering and planning brief are excluded."""
    by_id = {stage["stage_id"]: stage for stage in plan["stages"]}
    assets = {a["input_name"]: a["fingerprint"] for a in plan["assets"]}
    result = {}

    def compute(stage_id):
        if stage_id not in result:
            stage = by_id[stage_id]
            bindings = [
                {
                    **b,
                    "fingerprint": assets[b["source_id"]]
                    if b["source_type"] == "input"
                    else compute(b["source_id"]),
                }
                for b in sorted(stage["input_bindings"], key=lambda b: b["slot"])
            ]
            result[stage_id] = fingerprint(
                {
                    "contract": "director-stage-v2",
                    "prompt": stage["prompt"],
                    "media_kind": stage["media_kind"],
                    "target_profile": stage["target_profile"],
                    "parameters": sorted(stage["parameters"], key=lambda p: p["name"]),
                    "bindings": bindings,
                }
            )
        return result[stage_id]

    for stage_id in by_id:
        compute(stage_id)
    return result
