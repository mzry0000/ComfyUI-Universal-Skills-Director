"""Pure normalization, validation, signatures, and selection for Director plans."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, cast

from .director_profiles import profile_warnings
from .director_schemas import (
    DIRECTOR_SCHEMA_VERSION,
    validate_director_draft,
    validate_director_plan_schema,
)
from .errors import OpenAIResponseFormatError, UniversalSkillHostError
from .specification import (
    normalize_specification,
    specification_signature_payload,
)


_SIGNATURE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DONE_STATUSES = frozenset({"succeeded", "completed", "selected", "assembled"})
_ACTIVE_STATUSES = frozenset({"queued", "running"})
_DEPENDENCY_DONE_STATUSES = frozenset(
    {"succeeded", "completed", "selected", "assembled"}
)
_SELECTION_MODES = frozenset({"next_ready", "exact", "retry_failed"})
_MAX_TOTAL_SHOTS = 256


def canonical_json(value: object) -> str:
    """Serialize normalized JSON data identically across supported Python versions."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise UniversalSkillHostError(
            "Director data must contain only finite JSON-compatible values."
        ) from exc


def stable_signature(namespace: str, value: object) -> str:
    """Return a namespaced deterministic SHA-256 signature."""

    if not isinstance(namespace, str) or not namespace.strip():
        raise UniversalSkillHostError("Signature namespace must not be empty.")
    payload = (namespace.strip() + "\0" + canonical_json(value)).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _normalize_json(value: object) -> object:
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_json(child) for key, child in value.items()}
    if isinstance(value, list):
        normalized = [_normalize_json(child) for child in value]
        if all(isinstance(child, str) for child in normalized):
            return list(dict.fromkeys(child for child in normalized if child))
        return normalized
    return value


def _normalized_number(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    numeric = round(float(value), 6)
    return 0.0 if numeric == 0 else numeric


def _normalize_plan_collections(value: dict[str, Any], *, runtime: bool) -> None:
    assets = value.get("assets")
    if isinstance(assets, list):
        assets.sort(key=lambda item: str(item.get("asset_id", "")))

    scenes = value.get("scenes")
    if not isinstance(scenes, list):
        return
    scenes.sort(key=lambda item: (int(item.get("order", 0)), str(item.get("title", ""))))
    for scene in scenes:
        scene["start_seconds"] = _normalized_number(scene.get("start_seconds"))
        scene["duration_seconds"] = _normalized_number(scene.get("duration_seconds"))
        shots = scene.get("shots")
        if not isinstance(shots, list):
            continue
        shots.sort(key=lambda item: (int(item.get("order", 0)), str(item.get("title", ""))))
        for shot in shots:
            timing = shot.get("timing")
            if isinstance(timing, dict):
                for name in ("start_seconds", "duration_seconds", "fps"):
                    timing[name] = _normalized_number(timing.get(name))
            stages = shot.get("stages")
            if isinstance(stages, list):
                stages.sort(
                    key=lambda item: (int(item.get("order", 0)), str(item.get("stage_id", "")))
                )
            variants = shot.get("variants")
            if isinstance(variants, list):
                variants.sort(
                    key=lambda item: (int(item.get("order", 0)), int(item.get("take", 0)))
                )
            if runtime:
                # Runtime IDs are checked against their order-derived canonical
                # values by semantic validation; do not rewrite supplied IDs here.
                continue


def normalize_director_draft(value: object) -> dict[str, Any]:
    """Return a canonical deep copy of one structurally valid model draft."""

    candidate = copy.deepcopy(validate_director_draft(value))
    normalized = cast(dict[str, Any], _normalize_json(candidate))
    _normalize_plan_collections(normalized, runtime=False)
    validate_director_draft(normalized)
    _validate_director_semantics(normalized, runtime=False)
    return normalized


def normalize_director_plan(value: object) -> dict[str, Any]:
    """Return a canonical deep copy without trusting a custom-type dictionary."""

    candidate = copy.deepcopy(validate_director_plan_schema(value))
    normalized = cast(dict[str, Any], _normalize_json(candidate))
    _normalize_plan_collections(normalized, runtime=True)
    validate_director_plan_schema(normalized)
    return normalized


def _ensure_unique(items: Sequence[Mapping[str, Any]], field: str, location: str) -> None:
    seen: set[object] = set()
    for item in items:
        value = item.get(field)
        if value in seen:
            raise OpenAIResponseFormatError(
                f"Director plan contains duplicate {field} {value!r} in {location}."
            )
        seen.add(value)


def _validate_stage_graph(stages: Sequence[Mapping[str, Any]], shot_label: str) -> None:
    stage_ids = {str(stage.get("stage_id", "")) for stage in stages}
    dependencies: dict[str, tuple[str, ...]] = {}
    for stage in stages:
        stage_id = str(stage.get("stage_id", ""))
        raw_dependencies = stage.get("depends_on", [])
        deps = tuple(str(value) for value in raw_dependencies)
        for dependency in deps:
            if dependency not in stage_ids:
                raise OpenAIResponseFormatError(
                    f"Director stage {stage_id!r} in {shot_label} depends on unknown "
                    f"stage {dependency!r}."
                )
            if dependency == stage_id:
                raise OpenAIResponseFormatError(
                    f"Director stage {stage_id!r} in {shot_label} depends on itself."
                )
        dependencies[stage_id] = deps

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(stage_id: str) -> None:
        if stage_id in visiting:
            raise OpenAIResponseFormatError(
                f"Director stage dependencies contain a cycle in {shot_label}."
            )
        if stage_id in visited:
            return
        visiting.add(stage_id)
        for dependency in dependencies[stage_id]:
            visit(dependency)
        visiting.remove(stage_id)
        visited.add(stage_id)

    for stage_id in dependencies:
        visit(stage_id)


def _validate_stage_prompts(shot: Mapping[str, Any], shot_label: str) -> None:
    prompt = cast(Mapping[str, Any], shot.get("prompt", {}))
    keyframe = str(prompt.get("keyframe", "")).strip()
    motion = str(prompt.get("motion", "")).strip()
    content = cast(Mapping[str, Any], shot.get("content", {}))
    dialogue = str(content.get("dialogue", "")).strip()
    for stage in cast(Sequence[Mapping[str, Any]], shot.get("stages", [])):
        kind = stage.get("kind")
        stage_id = stage.get("stage_id")
        if kind in {"image", "text"} and not keyframe:
            raise OpenAIResponseFormatError(
                f"Director {kind} stage {stage_id!r} in {shot_label} needs a keyframe prompt."
            )
        if kind == "video" and not (motion or keyframe):
            raise OpenAIResponseFormatError(
                f"Director video stage {stage_id!r} in {shot_label} needs a motion or "
                "keyframe prompt."
            )
        if kind == "audio" and not (motion or dialogue or keyframe):
            raise OpenAIResponseFormatError(
                f"Director audio stage {stage_id!r} in {shot_label} needs prompt or dialogue."
            )


def _validate_director_semantics(plan: Mapping[str, Any], *, runtime: bool) -> None:
    assets = cast(Sequence[Mapping[str, Any]], plan.get("assets", []))
    _ensure_unique(assets, "asset_id", "assets")
    asset_ids = {str(asset.get("asset_id")) for asset in assets}

    scenes = cast(Sequence[Mapping[str, Any]], plan.get("scenes", []))
    _ensure_unique(scenes, "order", "scenes")
    if runtime:
        _ensure_unique(scenes, "scene_id", "scenes")

    total_shots = 0
    for scene in scenes:
        scene_order = int(scene["order"])
        scene_id = str(scene.get("scene_id", f"scene-{scene_order:03d}"))
        if runtime and scene_id != f"scene-{scene_order:03d}":
            raise OpenAIResponseFormatError(
                f"Director scene_id {scene_id!r} does not match scene order {scene_order}."
            )
        shots = cast(Sequence[Mapping[str, Any]], scene.get("shots", []))
        total_shots += len(shots)
        _ensure_unique(shots, "order", scene_id)
        if runtime:
            _ensure_unique(shots, "shot_id", scene_id)

        scene_duration = float(scene["duration_seconds"])
        shot_cursor = 0.0
        for shot in shots:
            shot_order = int(shot["order"])
            shot_id = str(shot.get("shot_id", f"{scene_id}-shot-{shot_order:03d}"))
            if runtime and shot_id != f"{scene_id}-shot-{shot_order:03d}":
                raise OpenAIResponseFormatError(
                    f"Director shot_id {shot_id!r} does not match shot order {shot_order}."
                )

            references = cast(Sequence[Mapping[str, Any]], shot.get("references", []))
            _ensure_unique(references, "asset_id", shot_id)
            for reference in references:
                if str(reference.get("asset_id")) not in asset_ids:
                    raise OpenAIResponseFormatError(
                        f"Director shot {shot_id} references unknown asset "
                        f"{reference.get('asset_id')!r}."
                    )

            stages = cast(Sequence[Mapping[str, Any]], shot.get("stages", []))
            variants = cast(Sequence[Mapping[str, Any]], shot.get("variants", []))
            _ensure_unique(stages, "stage_id", shot_id)
            _ensure_unique(stages, "order", shot_id)
            _ensure_unique(variants, "order", shot_id)
            if runtime:
                _ensure_unique(variants, "variant_id", shot_id)
                for variant in variants:
                    expected = f"{shot_id}-variant-{int(variant['order']):03d}"
                    if variant.get("variant_id") != expected:
                        raise OpenAIResponseFormatError(
                            f"Director variant_id {variant.get('variant_id')!r} does not "
                            f"match variant order {variant['order']}."
                        )
            _validate_stage_graph(stages, shot_id)
            _validate_stage_prompts(shot, shot_id)

            timing = cast(Mapping[str, Any], shot.get("timing", {}))
            shot_start = timing.get("start_seconds")
            # A shot start is always relative to the containing scene.  A null
            # value uses the end of the latest preceding shot, matching the
            # timeline builder's sequential placement rule.
            relative_start = (
                shot_cursor
                if shot_start is None
                else float(cast(float, shot_start))
            )
            shot_end = relative_start + float(timing["duration_seconds"])
            if relative_start < 0 or shot_end > scene_duration + 0.000001:
                raise OpenAIResponseFormatError(
                    f"Director shot {shot_id} falls outside its scene timing range."
                )
            shot_cursor = max(shot_cursor, shot_end)

    if total_shots > _MAX_TOTAL_SHOTS:
        raise OpenAIResponseFormatError(
            f"Director plan contains {total_shots} shots; the limit is {_MAX_TOTAL_SHOTS}."
        )


def _specification_record(specification: object) -> dict[str, str]:
    normalized = normalize_specification(specification)
    fingerprint = stable_signature(
        "director-specification",
        specification_signature_payload(normalized),
    )
    return {
        "name": normalized["name"],
        "version": normalized["version"],
        "source_format": normalized["source_format"],
        "source_file": normalized["source_file"],
        "fingerprint": fingerprint,
    }


def normalize_authoritative_assets(value: object) -> list[dict[str, Any]]:
    """Normalize trusted asset handles without allowing embedded media payloads."""

    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise UniversalSkillHostError("Director assets must be an array of asset records.")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise UniversalSkillHostError(f"Director asset {index + 1} must be an object.")
        required = {"asset_id", "kind", "source_input", "role", "preserve", "fingerprint"}
        unknown = set(item) - required
        missing = required - set(item)
        if unknown or missing:
            raise UniversalSkillHostError(
                f"Director asset {index + 1} has invalid fields."
            )
        fingerprint = _normalize_text(str(item["fingerprint"]))
        if _SIGNATURE_RE.fullmatch(fingerprint) is None:
            raise UniversalSkillHostError(
                f"Director asset {index + 1} fingerprint must be sha256:<64 hex>."
            )
        preserve = item["preserve"]
        if not isinstance(preserve, list) or not all(isinstance(part, str) for part in preserve):
            raise UniversalSkillHostError(
                f"Director asset {index + 1} preserve must be a string array."
            )
        normalized = cast(
            dict[str, Any],
            _normalize_json(
                {
                    "asset_id": item["asset_id"],
                    "kind": item["kind"],
                    "source_input": item["source_input"],
                    "role": item["role"],
                    "preserve": preserve,
                    "fingerprint": fingerprint,
                }
            ),
        )
        result.append(normalized)
    result.sort(key=lambda asset: asset["asset_id"])
    _ensure_unique(result, "asset_id", "authoritative assets")
    return result


def _merge_authoritative_assets(
    draft_assets: Sequence[Mapping[str, Any]],
    authoritative_assets: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    authoritative = normalize_authoritative_assets(authoritative_assets)
    expected = {asset["asset_id"]: asset for asset in authoritative}
    draft_by_id = {str(asset["asset_id"]): asset for asset in draft_assets}
    if set(draft_by_id) != set(expected):
        raise OpenAIResponseFormatError(
            "Director draft assets did not exactly match the supplied asset IDs."
        )
    result: list[dict[str, Any]] = []
    for asset_id in sorted(expected):
        source = expected[asset_id]
        draft = draft_by_id[asset_id]
        if draft.get("kind") != source["kind"] or draft.get("source_input") != source["source_input"]:
            raise OpenAIResponseFormatError(
                f"Director draft changed the kind or source of asset {asset_id!r}."
            )
        result.append(
            {
                **source,
                "role": str(draft.get("role", "")).strip(),
                # Trusted preservation constraints are authoritative.  The model
                # may add constraints but can never remove operator-supplied ones.
                "preserve": _deduplicate_strings(
                    [*source["preserve"], *draft.get("preserve", [])]
                ),
            }
        )
    return result


def _hydrate_ids(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    for raw_scene in cast(Sequence[Mapping[str, Any]], draft["scenes"]):
        scene = copy.deepcopy(dict(raw_scene))
        scene_id = f"scene-{int(scene['order']):03d}"
        scene["scene_id"] = scene_id
        hydrated_shots: list[dict[str, Any]] = []
        for raw_shot in cast(Sequence[Mapping[str, Any]], scene["shots"]):
            shot = copy.deepcopy(dict(raw_shot))
            shot_id = f"{scene_id}-shot-{int(shot['order']):03d}"
            shot["shot_id"] = shot_id
            hydrated_variants: list[dict[str, Any]] = []
            for raw_variant in cast(Sequence[Mapping[str, Any]], shot["variants"]):
                variant = copy.deepcopy(dict(raw_variant))
                variant["variant_id"] = (
                    f"{shot_id}-variant-{int(variant['order']):03d}"
                )
                hydrated_variants.append(variant)
            shot["variants"] = hydrated_variants
            hydrated_shots.append(shot)
        scene["shots"] = hydrated_shots
        scenes.append(scene)
    return scenes


def _plan_execution_projection(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: copy.deepcopy(plan[name])
        for name in (
            "schema_version",
            "plan_id",
            "director_profile",
            "target_profile",
            "title",
            "goal",
            "specification",
            "brief_fingerprint",
            "assets",
            "globals",
            "scenes",
        )
    }


def compute_plan_signature(plan: Mapping[str, Any]) -> str:
    """Hash only execution-affecting plan content, excluding revision and review text."""

    return stable_signature("director-plan-v1", _plan_execution_projection(plan))


def compute_shot_signature(
    plan: Mapping[str, Any],
    scene: Mapping[str, Any],
    shot: Mapping[str, Any],
    variant: Mapping[str, Any],
) -> str:
    """Hash one variant and its dependencies without invalidating unrelated shots."""

    referenced_ids = {
        str(reference.get("asset_id"))
        for reference in cast(Sequence[Mapping[str, Any]], shot.get("references", []))
    }
    assets = [
        copy.deepcopy(asset)
        for asset in cast(Sequence[Mapping[str, Any]], plan.get("assets", []))
        if str(asset.get("asset_id")) in referenced_ids
    ]
    assets.sort(key=lambda asset: str(asset.get("asset_id", "")))
    shot_without_variants = copy.deepcopy(dict(shot))
    shot_without_variants.pop("variants", None)
    projection = {
        "schema_version": plan["schema_version"],
        "director_profile": plan["director_profile"],
        "target_profile": plan["target_profile"],
        "goal": plan["goal"],
        "specification": {"fingerprint": plan["specification"]["fingerprint"]},
        "globals": copy.deepcopy(plan["globals"]),
        "assets": assets,
        "scene": {
            name: copy.deepcopy(scene[name])
            for name in (
                "scene_id",
                "title",
                "summary",
                "start_seconds",
                "duration_seconds",
            )
        },
        "shot": shot_without_variants,
        "variant": copy.deepcopy(dict(variant)),
    }
    return stable_signature("director-shot-v1", projection)


def hydrate_director_plan(
    draft: object,
    *,
    specification: object,
    brief_fingerprint: str,
    authoritative_assets: Sequence[Mapping[str, Any]] = (),
    runtime_warnings: Sequence[str] = (),
    previous_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert one model draft into a deterministic, signed runtime plan."""

    normalized_draft = normalize_director_draft(draft)
    if not isinstance(brief_fingerprint, str) or _SIGNATURE_RE.fullmatch(
        brief_fingerprint
    ) is None:
        raise UniversalSkillHostError(
            "brief_fingerprint must use the sha256:<64 hex> format."
        )
    assets = _merge_authoritative_assets(
        cast(Sequence[Mapping[str, Any]], normalized_draft["assets"]),
        authoritative_assets,
    )
    plan_id = "dir_" + brief_fingerprint.removeprefix("sha256:")[:20]
    revision = 1
    if previous_plan is not None:
        checked_previous = validate_director_plan(previous_plan)
        if checked_previous["plan_id"] != plan_id:
            raise UniversalSkillHostError(
                "previous_plan was created from a different Director brief."
            )
        if checked_previous["brief_fingerprint"] != brief_fingerprint:
            raise UniversalSkillHostError(
                "previous_plan brief_fingerprint does not match the current request."
            )
        revision = int(checked_previous["revision"]) + 1
    warnings = _deduplicate_strings(
        [*normalized_draft["warnings"], *runtime_warnings]
    )
    plan: dict[str, Any] = {
        "schema_version": DIRECTOR_SCHEMA_VERSION,
        "plan_id": plan_id,
        "revision": revision,
        "plan_signature": "sha256:" + ("0" * 64),
        "director_profile": normalized_draft["director_profile"],
        "target_profile": normalized_draft["target_profile"],
        "title": normalized_draft["title"],
        "goal": normalized_draft["goal"],
        "specification": _specification_record(specification),
        "brief_fingerprint": brief_fingerprint,
        "assets": assets,
        "globals": copy.deepcopy(normalized_draft["globals"]),
        "scenes": _hydrate_ids(normalized_draft),
        "warnings": warnings,
        "summary": normalized_draft["summary"],
    }
    plan["warnings"] = _deduplicate_strings([*warnings, *profile_warnings(plan)])
    normalized_plan = normalize_director_plan(plan)
    _validate_director_semantics(normalized_plan, runtime=True)
    normalized_plan["plan_signature"] = compute_plan_signature(normalized_plan)
    return validate_director_plan(normalized_plan)


def validate_director_plan(value: object) -> dict[str, Any]:
    """Validate structure, references, dependency graph, IDs, and signature."""

    plan = normalize_director_plan(value)
    _validate_director_semantics(plan, runtime=True)
    brief_fingerprint = str(plan["brief_fingerprint"])
    expected_id = "dir_" + brief_fingerprint.removeprefix("sha256:")[:20]
    if plan["plan_id"] != expected_id:
        raise OpenAIResponseFormatError(
            "Director plan_id does not match its brief_fingerprint."
        )
    expected_signature = compute_plan_signature(plan)
    if plan["plan_signature"] != expected_signature:
        raise OpenAIResponseFormatError(
            "Director plan_signature does not match its execution content."
        )
    return plan


def _deduplicate_strings(values: Sequence[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = _normalize_text(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _expanded_references(
    plan: Mapping[str, Any], shot: Mapping[str, Any]
) -> list[dict[str, Any]]:
    assets = {
        str(asset["asset_id"]): asset
        for asset in cast(Sequence[Mapping[str, Any]], plan["assets"])
    }
    result: list[dict[str, Any]] = []
    for reference in cast(Sequence[Mapping[str, Any]], shot["references"]):
        asset = assets[str(reference["asset_id"])]
        result.append(
            {
                "asset_id": asset["asset_id"],
                "kind": asset["kind"],
                "source_input": asset["source_input"],
                "role": reference["role"] or asset["role"],
                "preserve": _deduplicate_strings(
                    [*asset["preserve"], *reference["preserve"]]
                ),
                "fingerprint": asset["fingerprint"],
            }
        )
    return result


def _stage_prompt(shot: Mapping[str, Any], stage_kind: str) -> str:
    prompt = cast(Mapping[str, Any], shot["prompt"])
    content = cast(Mapping[str, Any], shot["content"])
    if stage_kind == "video":
        return str(prompt["motion"] or prompt["keyframe"])
    if stage_kind == "audio":
        return str(content["dialogue"] or prompt["motion"] or prompt["keyframe"])
    return str(prompt["keyframe"])


def flatten_work_items(value: object) -> tuple[dict[str, Any], ...]:
    """Expand a valid plan into deterministic shot × variant × stage tickets."""

    plan = validate_director_plan(value)
    result: list[dict[str, Any]] = []
    for scene in plan["scenes"]:
        for shot in scene["shots"]:
            for variant in shot["variants"]:
                shot_signature = compute_shot_signature(plan, scene, shot, variant)
                for stage in shot["stages"]:
                    item_id = (
                        f"{shot['shot_id']}:{variant['variant_id']}:{stage['stage_id']}"
                    )
                    result.append(
                        {
                            "schema_version": DIRECTOR_SCHEMA_VERSION,
                            "item_id": item_id,
                            "plan_id": plan["plan_id"],
                            "revision": plan["revision"],
                            "plan_signature": plan["plan_signature"],
                            "shot_signature": shot_signature,
                            "scene_id": scene["scene_id"],
                            "scene_order": scene["order"],
                            "scene_title": scene["title"],
                            "scene_summary": scene["summary"],
                            "shot_id": shot["shot_id"],
                            "shot_order": shot["order"],
                            "shot_title": shot["title"],
                            "shot_type": shot["shot_type"],
                            "variant_id": variant["variant_id"],
                            "variant_order": variant["order"],
                            "variant_label": variant["label"],
                            "variant_angle": variant["angle"],
                            "take": variant["take"],
                            "stage_id": stage["stage_id"],
                            "stage_order": stage["order"],
                            "stage_kind": stage["kind"],
                            "target_profile": stage["target_profile"],
                            "workflow_hint": stage["workflow_hint"],
                            "prompt": _stage_prompt(shot, stage["kind"]),
                            "keyframe_prompt": shot["prompt"]["keyframe"],
                            "motion_prompt": shot["prompt"]["motion"],
                            "negative_prompt": shot["prompt"]["negative"],
                            "timing": copy.deepcopy(shot["timing"]),
                            "camera": copy.deepcopy(shot["camera"]),
                            "content": copy.deepcopy(shot["content"]),
                            "references": _expanded_references(plan, shot),
                            "tags": copy.deepcopy(shot["tags"]),
                            "shot_constraints": copy.deepcopy(shot["constraints"]),
                            "seed": variant["seed"],
                            "depends_on": [
                                f"{shot['shot_id']}:{variant['variant_id']}:{dependency}"
                                for dependency in stage["depends_on"]
                            ],
                        }
                    )
    return tuple(result)


def _ledger_records(ledger: object) -> dict[str, Mapping[str, Any]]:
    if ledger is None:
        return {}
    raw_items: object
    if isinstance(ledger, Mapping):
        raw_items = ledger.get("items", [])
    else:
        raw_items = ledger
    if not isinstance(raw_items, Sequence) or isinstance(
        raw_items, (str, bytes, bytearray)
    ):
        raise UniversalSkillHostError("Director ledger items must be an array.")
    records: dict[str, Mapping[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        item_id = raw.get("item_id")
        if isinstance(item_id, str) and item_id:
            records[item_id] = raw
    return records


def _record_is_current(record: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    recorded = record.get("dependency_signature", record.get("shot_signature"))
    return recorded in (None, "", item["shot_signature"])


def select_work_item(
    value: object,
    *,
    ledger: object = None,
    scene_id: str = "",
    shot_id: str = "",
    variant_id: str = "",
    stage_id: str = "",
    selection_mode: str = "next_ready",
    include_failed: bool | None = None,
) -> dict[str, Any]:
    """Select one dependency-ready ticket using an explicit deterministic mode.

    ``include_failed`` remains as a compatibility override for older callers:
    passing ``True`` lets ``next_ready`` consider failed items, while the new
    default excludes them.  New retry UIs should use ``retry_failed``.
    """

    if selection_mode not in _SELECTION_MODES:
        raise UniversalSkillHostError(
            "selection_mode must be one of: exact, next_ready, retry_failed."
        )

    filters = {
        "scene_id": _normalize_text(scene_id),
        "shot_id": _normalize_text(shot_id),
        "variant_id": _normalize_text(variant_id),
        "stage_id": _normalize_text(stage_id),
    }
    if selection_mode == "exact" and not all(filters.values()):
        raise UniversalSkillHostError(
            "exact selection_mode requires scene_id, shot_id, variant_id, and stage_id."
        )
    records = _ledger_records(ledger)
    candidates = []
    for item in flatten_work_items(value):
        if any(expected and item[name] != expected for name, expected in filters.items()):
            continue
        record = records.get(item["item_id"])
        if record is not None and not _record_is_current(record, item):
            record = None
        status = str(record.get("status", "pending")).lower() if record else "pending"
        if status in _DONE_STATUSES or status in _ACTIVE_STATUSES:
            continue
        if selection_mode == "retry_failed" and status != "failed":
            continue
        if (
            selection_mode == "next_ready"
            and status == "failed"
            and include_failed is not True
        ):
            continue

        dependency_ready = True
        for dependency_id in item["depends_on"]:
            dependency = records.get(dependency_id)
            if dependency is None or not _record_is_current(dependency, item):
                dependency_ready = False
                break
            if str(dependency.get("status", "")).lower() not in _DEPENDENCY_DONE_STATUSES:
                dependency_ready = False
                break
        if dependency_ready:
            candidates.append(item)

    if not candidates:
        requested = ", ".join(
            f"{name}={value!r}" for name, value in filters.items() if value
        )
        suffix = f" for {requested}" if requested else ""
        raise UniversalSkillHostError(
            f"No {selection_mode} dependency-ready Director work item was found"
            + suffix
            + "."
        )
    return copy.deepcopy(candidates[0])


__all__ = [
    "canonical_json",
    "compute_plan_signature",
    "compute_shot_signature",
    "flatten_work_items",
    "hydrate_director_plan",
    "normalize_authoritative_assets",
    "normalize_director_draft",
    "normalize_director_plan",
    "select_work_item",
    "stable_signature",
    "validate_director_plan",
]
