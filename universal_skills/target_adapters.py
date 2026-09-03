"""Deterministic conversion from a model-neutral plan to downstream prompt text."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .config import TARGET_PROFILES
from .errors import OpenAIResponseFormatError, UnsupportedTargetProfileError


_IMAGE_PROFILES = {
    "gpt_image_2",
    "generic_image_generation",
    "generic_image_edit",
    "anime_diffusion",
    "photoreal_diffusion",
}
_VIDEO_PROFILES = {"video_generation", "video_image_to_video"}


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _uses_compact_image_contract(target_profile: str, task_type: str) -> bool:
    """Return whether the downstream target consumes a still-image prompt."""

    if target_profile in _IMAGE_PROFILES:
        return True
    if target_profile != "custom":
        return False
    if task_type.endswith("_to_image"):
        return True
    if task_type.endswith("_to_video"):
        return False
    return task_type.startswith("image")


def _unique_clean(values: Iterable[Any]) -> list[str]:
    """Return non-empty strings once, preserving their original order."""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _bullet_section(title: str, values: Iterable[Any]) -> str:
    lines = _unique_clean(values)
    if not lines:
        return ""
    return f"{title}:\n" + "\n".join(f"- {line}" for line in lines)


def _mapping_lines(
    value: Mapping[str, Any], *, exclude_values: Iterable[Any] = ()
) -> list[str]:
    seen = set(_unique_clean(exclude_values))
    lines: list[str] = []
    for key, item in value.items():
        if isinstance(item, list):
            parts: list[str] = []
            for part in _unique_clean(item):
                if part not in seen:
                    seen.add(part)
                    parts.append(part)
            rendered = "; ".join(parts)
        else:
            rendered = _clean(item)
            if rendered in seen:
                rendered = ""
            elif rendered:
                seen.add(rendered)
        if rendered:
            label = key.replace("_", " ").capitalize()
            lines.append(f"{label}: {rendered}")
    return lines


def _mapping_values(value: Mapping[str, Any]) -> list[str]:
    """Return clean source values from a structured mapping without labels."""

    values: list[Any] = []
    for item in value.values():
        if isinstance(item, list):
            values.extend(item)
        else:
            values.append(item)
    return _unique_clean(values)


def _input_role_lines(plan: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for item in plan.get("input_roles", []):
        if not isinstance(item, Mapping):
            continue
        input_name = _clean(item.get("input"))
        role = _clean(item.get("role"))
        if input_name or role:
            if input_name and role:
                line = f"{input_name}: {role}"
            else:
                line = input_name or role
            lines.append(line)
    return lines


def _input_preservation_lines(plan: Mapping[str, Any]) -> tuple[list[str], set[str]]:
    """Return input-scoped preservation lines and their exact source values."""

    lines: list[str] = []
    values: set[str] = set()
    for item in plan.get("input_roles", []):
        if not isinstance(item, Mapping):
            continue
        preserve = item.get("preserve", [])
        if not isinstance(preserve, list):
            continue
        kept = _unique_clean(preserve)
        if not kept:
            continue
        values.update(kept)
        input_name = _clean(item.get("input")) or "Input image"
        lines.append(f"{input_name}: {'; '.join(kept)}")
    return lines, values


def _text_element_lines(plan: Mapping[str, Any]) -> list[str]:
    elements = plan.get("text_elements", [])
    if not isinstance(elements, list):
        return []
    sortable = [item for item in elements if isinstance(item, Mapping)]
    sortable.sort(key=lambda item: int(item.get("priority", 999_999)))
    lines: list[str] = []
    for item in sortable:
        text = _clean(item.get("text"))
        placement = _clean(item.get("placement"))
        priority = item.get("priority")
        if text:
            lines.append(f'"{text}" - priority {priority}, placement: {placement}')
    return lines


def build_final_prompt(plan: Mapping[str, Any]) -> str:
    """Return a compact standard STRING for a downstream generation/edit node.

    The exhaustive model draft remains available in plan_json.  The downstream
    prompt is rebuilt from authoritative structured fields so the same request is
    not repeated as a draft, an override rule, and a second set of requirements.
    """

    target_profile = _clean(plan.get("target_profile"))
    if target_profile not in TARGET_PROFILES:
        raise UnsupportedTargetProfileError(
            f"Unsupported target_profile: {target_profile or '(empty)'}."
        )

    task_type = _clean(plan.get("task_type")).lower()
    is_image_output = _uses_compact_image_contract(target_profile, task_type)
    draft = _clean(plan.get("downstream_prompt"))
    goal = _clean(plan.get("goal"))
    if not draft and not goal:
        raise OpenAIResponseFormatError(
            "The plan did not contain a usable downstream_prompt or goal."
        )

    primary_instruction = goal or draft
    sections: list[str] = [f"Goal:\n{primary_instruction}"]

    input_roles = _bullet_section("Input roles", _input_role_lines(plan))
    composition_value = plan.get("composition", {})
    composition_values = (
        _mapping_values(composition_value)
        if isinstance(composition_value, Mapping)
        else []
    )
    input_preservation, input_preserve_values = _input_preservation_lines(plan)
    # Top-level preserve has global/cross-output scope.  Keep its provenance
    # separate from input_roles[*].preserve even when the wording is identical.
    global_preserve_values = _unique_clean(plan.get("preserve", []))
    raw_constraint_values = _unique_clean(plan.get("constraints", []))
    constraint_values = [
        value
        for value in raw_constraint_values
        if value != primary_instruction
        and value not in composition_values
        and value not in global_preserve_values
    ]
    reserved_values = {
        primary_instruction,
        *composition_values,
        *global_preserve_values,
        *raw_constraint_values,
    }
    required_change_values = [
        value
        for value in _unique_clean(plan.get("required_changes", []))
        if value not in reserved_values
    ]
    direction_lines = [
        value
        for value in required_change_values
        if value != primary_instruction
    ]
    if isinstance(composition_value, Mapping):
        direction_lines.extend(
            _mapping_lines(
                composition_value,
                exclude_values=(primary_instruction, *global_preserve_values),
            )
        )
    if task_type.startswith("audio"):
        direction_title = "Audio direction"
    elif task_type.startswith("text"):
        direction_title = "Text direction"
    else:
        direction_title = "Visual direction"
    direction = _bullet_section(direction_title, direction_lines)
    text_elements = _bullet_section("Exact text", _text_element_lines(plan))
    if is_image_output:
        # The reference images already carry their visible appearance, so keep
        # their exhaustive per-input inventory in plan_json.  Global continuity
        # requirements can carry information that is not self-evident from one
        # image, however, and therefore remain as compact generation constraints.
        guardrails = _bullet_section(
            "Constraints",
            [
                (
                    "Continuity: " + "; ".join(global_preserve_values)
                    if global_preserve_values
                    else ""
                ),
                *constraint_values,
            ],
        )
    else:
        guardrail_title = (
            "Preserve exactly and constraints"
            if input_preservation or global_preserve_values
            else "Constraints"
        )
        guardrails = _bullet_section(
            guardrail_title,
            [
                *input_preservation,
                *(f"Global: {value}" for value in global_preserve_values),
                *(f"Constraint: {value}" for value in constraint_values),
            ],
        )

    for section in (input_roles, direction, text_elements, guardrails):
        if section:
            sections.append(section)

    motion_value = plan.get("motion", {})
    if is_image_output:
        include_motion = False
    elif target_profile in _VIDEO_PROFILES:
        include_motion = True
    elif "video" in task_type:
        include_motion = True
    elif task_type.startswith("image"):
        include_motion = False
    else:
        include_motion = isinstance(motion_value, Mapping) and bool(
            _mapping_lines(motion_value)
        )
    if include_motion:
        motion_exclusions = {
            primary_instruction,
            *required_change_values,
            *composition_values,
            *input_preserve_values,
            *global_preserve_values,
            *constraint_values,
        }
        for constraint in constraint_values:
            if constraint.startswith("Avoid: "):
                motion_exclusions.add(constraint.removeprefix("Avoid: ").strip())
        motion_lines = (
            _mapping_lines(motion_value, exclude_values=motion_exclusions)
            if isinstance(motion_value, Mapping)
            else []
        )
        motion_title = (
            "Timing" if task_type.startswith("audio") else "Motion and timing"
        )
        motion = _bullet_section(motion_title, motion_lines)
        if motion:
            sections.append(motion)

    return "\n\n".join(section.strip() for section in sections if section.strip())


def _director_item_from_plan(
    director_plan: Mapping[str, Any], work_item: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return canonical plan/item copies and reject detached or stale tickets."""

    # Local import keeps the original one-shot adapter lightweight and avoids a
    # hard Director dependency for existing saved workflows.
    from .director_core import flatten_work_items, validate_director_plan

    plan = validate_director_plan(director_plan)
    item_id = work_item.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        raise OpenAIResponseFormatError("Director work item has no item_id.")
    canonical = next(
        (item for item in flatten_work_items(plan) if item["item_id"] == item_id),
        None,
    )
    if canonical is None:
        raise OpenAIResponseFormatError(
            "Director work item does not belong to the supplied plan."
        )
    for field in ("plan_id", "revision", "plan_signature", "shot_signature"):
        if work_item.get(field) != canonical[field]:
            raise OpenAIResponseFormatError(
                f"Director work item {field} is stale or invalid."
            )
    return plan, canonical


def director_work_item_to_plan(
    director_plan: Mapping[str, Any], work_item: Mapping[str, Any]
) -> dict[str, Any]:
    """Map one Director ticket onto the established model-neutral prompt plan."""

    plan, item = _director_item_from_plan(director_plan, work_item)
    stage_kind = str(item["stage_kind"])
    visual_stage = stage_kind in {"image", "video"}
    reference_kinds = {
        "image": {"image"},
        "video": {"image", "video"},
        "audio": {"audio"},
        "text": {"text"},
    }.get(stage_kind, set())

    references = item["references"]
    input_roles: list[dict[str, Any]] = []
    for reference in references:
        if str(reference.get("kind", "")) not in reference_kinds:
            continue
        kept = [
            str(value)
            for value in reference.get("preserve", [])
            if str(value).strip()
        ]
        input_roles.append(
            {
                "input": str(reference.get("source_input", reference["asset_id"])),
                "role": str(reference.get("role", "")),
                "preserve": kept,
            }
        )

    globals_value = plan["globals"]
    continuity = (
        [str(value) for value in globals_value["continuity"]]
        if visual_stage
        else []
    )
    content = item["content"]
    camera = item["camera"]
    timing = item["timing"]
    constraints = [
        *[str(value) for value in globals_value["constraints"]],
        *(
            [str(value) for value in item["shot_constraints"]]
            if visual_stage
            else []
        ),
    ]
    negative = str(item.get("negative_prompt", "")).strip()
    if visual_stage and negative:
        constraints.append("Avoid: " + negative)

    task_types = {
        "image": "image_generation",
        "video": "video_generation",
        "audio": "audio_generation",
        "text": "text_generation",
    }
    text_overlay = str(content.get("text_overlay", "")).strip()
    text_elements = (
        [{"text": text_overlay, "priority": 1, "placement": "as directed in the shot"}]
        if text_overlay and stage_kind in {"image", "video", "text"}
        else []
    )
    stage_prompt = str(item["prompt"]).strip()
    required_changes = []
    if visual_stage:
        required_changes.append(
            f"Use variant {item['variant_label']} at {item['variant_angle']} "
            f"(take {item['take']})."
        )
    product_action = str(content.get("product_action", "")).strip()
    dialogue = str(content.get("dialogue", "")).strip()
    lyric_moment = str(content.get("lyric_moment", "")).strip()
    if stage_kind in {"image", "video"} and product_action:
        if product_action != stage_prompt:
            required_changes.append("Product action: " + product_action)
    if stage_kind in {"video", "audio", "text"} and dialogue:
        if dialogue != stage_prompt:
            required_changes.append("Dialogue: " + dialogue)
    if stage_kind in {"video", "audio", "text"} and lyric_moment:
        if lyric_moment != stage_prompt:
            required_changes.append("Lyric moment: " + lyric_moment)

    duration = timing.get("duration_seconds")
    timeline = (
        [f"Duration: {duration} seconds"]
        if stage_kind in {"video", "audio"} and duration is not None
        else []
    )
    fps = timing.get("fps")
    if stage_kind == "video" and fps is not None:
        timeline.append(f"Frame rate: {fps} fps")

    camera_angle_parts = [str(camera["angle"]), str(item["variant_angle"])]
    lens = str(camera.get("lens", "")).strip()
    if stage_kind in {"image", "video"} and lens:
        camera_angle_parts.append("Lens: " + lens)

    return {
        "task_type": task_types.get(str(stage_kind), "generation"),
        # A Work Item is sent to one downstream node.  Its own prompt therefore
        # owns the executable goal; project-wide progression stays in plan_json.
        "goal": stage_prompt,
        "target_profile": str(item["target_profile"]),
        "input_roles": input_roles,
        "required_changes": required_changes,
        "preserve": list(dict.fromkeys(continuity)),
        "constraints": list(
            dict.fromkeys(value for value in constraints if value.strip())
        ),
        "composition": {
            "aspect_ratio": str(globals_value["aspect_ratio"]) if visual_stage else "",
            "framing": str(camera["framing"]) if visual_stage else "",
            # Scene summaries describe multi-stage progression and do not own a
            # selected stage's spatial layout.
            "layout": "",
            "camera_angle": (
                " / ".join(value for value in camera_angle_parts if value.strip())
                if visual_stage
                else ""
            ),
            "lighting": "",
            "background": "",
            "style": str(globals_value["style"]) if visual_stage else "",
        },
        "text_elements": text_elements,
        "motion": {
            "start_state": (
                str(item["keyframe_prompt"]) if stage_kind == "video" else ""
            ),
            "timeline": timeline,
            "subject_motion": (
                str(item["motion_prompt"]) if stage_kind == "video" else ""
            ),
            "camera_motion": (
                str(camera["movement"]) if stage_kind == "video" else ""
            ),
            "end_state": "",
            "preserve": continuity,
            "forbidden_motion": (
                [negative] if stage_kind == "video" and negative else []
            ),
        },
        "downstream_prompt": str(item["prompt"]),
        "warnings": list(plan["warnings"]),
        "summary": (
            f"Director {item['scene_id']} / {item['shot_id']} / "
            f"{item['variant_id']} / {item['stage_id']}"
        ),
    }


def build_director_final_prompt(
    director_plan: Mapping[str, Any], work_item: Mapping[str, Any]
) -> str:
    """Return a plain STRING prompt for a selected Director work item."""

    final_prompt = build_final_prompt(
        director_work_item_to_plan(director_plan, work_item)
    )
    if not isinstance(final_prompt, str) or not final_prompt.strip():  # defensive contract
        raise OpenAIResponseFormatError(
            "Director target adapter did not produce a usable STRING prompt."
        )
    return final_prompt


__all__ = [
    "build_director_final_prompt",
    "build_final_prompt",
    "director_work_item_to_plan",
]
