"""Build a deterministic, editor-neutral timeline manifest from Director state."""

from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, cast

from .director_core import validate_director_plan
from .director_state import (
    DirectorStateError,
    canonical_json,
    parse_strict_json,
    validate_ledger_for_plan,
)
from .errors import OpenAIResponseFormatError
from .schemas import validate_schema_instance


TIMELINE_MANIFEST_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "timeline_manifest.schema.json"
)
TIMELINE_SCHEMA_VERSION = "1.0"
TIMELINE_SELECTION_POLICIES = (
    "selected_else_latest_succeeded",
    "all_succeeded",
)
TIMELINE_GAP_POLICIES = ("preserve", "compact")
TIMELINE_TRACK_POLICIES = ("single_track", "coverage")

_OUTPUT_STATUSES = frozenset({"succeeded", "selected", "assembled"})
_SELECTED_STATUSES = frozenset({"selected", "assembled"})
_MEDIA_STAGE_KINDS = frozenset({"video", "audio"})
_STATUS_RANK = {"succeeded": 1, "selected": 2, "assembled": 3}


class TimelineManifestError(DirectorStateError):
    """Raised when a timeline cannot be derived from current Director state."""


@lru_cache(maxsize=1)
def _load_timeline_manifest_schema_template() -> dict[str, Any]:
    try:
        raw = TIMELINE_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - broken installation
        raise RuntimeError(
            f"Timeline manifest schema could not be read: {TIMELINE_MANIFEST_SCHEMA_PATH}"
        ) from exc
    value = parse_strict_json(raw, label="Timeline manifest schema")
    if not isinstance(value, dict):  # pragma: no cover - checked-in schema invariant
        raise RuntimeError("Timeline manifest schema root must be an object.")
    return cast(dict[str, Any], value)


def load_timeline_manifest_schema() -> dict[str, Any]:
    """Return a fresh copy of the checked-in timeline schema."""

    return copy.deepcopy(_load_timeline_manifest_schema_template())


def validate_timeline_manifest(value: object) -> dict[str, Any]:
    """Validate a decoded timeline manifest."""

    try:
        validate_schema_instance(value, _load_timeline_manifest_schema_template())
    except (OpenAIResponseFormatError, ValueError) as exc:
        raise TimelineManifestError(f"Timeline manifest is invalid: {exc}") from exc
    manifest = cast(dict[str, Any], value)
    track_ids = [track["track_id"] for track in manifest["tracks"]]
    if len(track_ids) != len(set(track_ids)):
        raise TimelineManifestError("Timeline manifest contains duplicate track IDs.")
    clip_ids = [
        clip["clip_id"] for track in manifest["tracks"] for clip in track["clips"]
    ]
    if len(clip_ids) != len(set(clip_ids)):
        raise TimelineManifestError("Timeline manifest contains duplicate clip IDs.")
    return manifest


def build_timeline_manifest(
    plan: Mapping[str, Any],
    ledger: object,
    *,
    selection_policy: str = "selected_else_latest_succeeded",
    gap_policy: str = "preserve",
    track_policy: str = "single_track",
) -> dict[str, Any]:
    """Return an editor-neutral manifest without opening or probing media refs."""

    if selection_policy not in TIMELINE_SELECTION_POLICIES:
        raise TimelineManifestError(
            f"selection_policy must be one of: {', '.join(TIMELINE_SELECTION_POLICIES)}."
        )
    if gap_policy not in TIMELINE_GAP_POLICIES:
        raise TimelineManifestError(
            f"gap_policy must be one of: {', '.join(TIMELINE_GAP_POLICIES)}."
        )
    if track_policy not in TIMELINE_TRACK_POLICIES:
        raise TimelineManifestError(
            f"track_policy must be one of: {', '.join(TIMELINE_TRACK_POLICIES)}."
        )

    checked_plan = validate_director_plan(plan)
    checked_ledger = validate_ledger_for_plan(checked_plan, ledger)
    plan_index = _index_plan(checked_plan)
    ledger_items = {item["item_id"]: item for item in checked_ledger["items"]}
    receipt_order: dict[str, int] = {}
    for sequence, receipt in enumerate(checked_ledger["receipts"], start=1):
        receipt_order[receipt["item_id"]] = sequence

    candidates: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    expected_groups: list[tuple[str, str]] = []
    for item_id, entry in plan_index["items"].items():
        if entry["stage_kind"] not in _MEDIA_STAGE_KINDS:
            continue
        group = (entry["shot_id"], entry["stage_id"])
        if group not in expected_groups:
            expected_groups.append(group)
        state = ledger_items.get(item_id)
        if (
            state is not None
            and state["status"] in _OUTPUT_STATUSES
            and state["output_refs"]
        ):
            candidates[group].append(
                {
                    "entry": entry,
                    "state": state,
                    # Receipt append order is the monotonic event order within a
                    # ledger.  Timestamps are user-injectable and may collide.
                    "event_sequence": receipt_order.get(item_id, 0),
                }
            )

    warnings: list[str] = []
    chosen: list[dict[str, Any]] = []
    for group in expected_groups:
        group_candidates = candidates.get(group, [])
        if not group_candidates:
            warnings.append(
                f"No completed media result is available for shot {group[0]!r}, "
                f"stage {group[1]!r}."
            )
            continue
        if selection_policy == "all_succeeded":
            chosen.extend(sorted(group_candidates, key=_candidate_sort_key))
            continue
        preferred = [
            candidate
            for candidate in group_candidates
            if candidate["state"]["status"] in _SELECTED_STATUSES
        ]
        pool = preferred or group_candidates
        if len(preferred) > 1:
            warnings.append(
                f"Multiple selected results exist for shot {group[0]!r}, stage "
                f"{group[1]!r}; the latest deterministic candidate was used."
            )
        chosen.append(max(pool, key=_candidate_sort_key))

    chosen.sort(
        key=lambda candidate: (
            candidate["entry"]["scene_order"],
            candidate["entry"]["shot_order"],
            candidate["entry"]["stage_order"],
            candidate["entry"]["variant_order"],
            candidate["state"]["item_id"],
        )
    )
    compact_starts = _compact_start_map(chosen) if gap_policy == "compact" else {}

    grouped_clips: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in chosen:
        entry = candidate["entry"]
        state = candidate["state"]
        kind = entry["stage_kind"]
        coverage = _coverage_label(entry) if kind == "video" else "main"
        track_key = (kind, coverage if track_policy == "coverage" else "main")
        natural_start = entry["natural_start_seconds"]
        timeline_start = (
            compact_starts[entry["shot_id"]]
            if gap_policy == "compact"
            else natural_start
        )
        asset_ref = state["output_refs"][-1]
        clip_projection = {
            "item_id": state["item_id"],
            "asset_ref": asset_ref,
            "timeline_start_seconds": _rounded(timeline_start),
            "duration_seconds": entry["duration_seconds"],
            "speed": 1.0,
            "sync_group": entry["sync_group"],
        }
        clip = {
            "clip_id": _short_id("clip", clip_projection),
            "item_id": state["item_id"],
            "scene_id": entry["scene_id"],
            "shot_id": entry["shot_id"],
            "variant_id": entry["variant_id"],
            "stage_id": entry["stage_id"],
            "asset_ref": asset_ref,
            "timeline_start_seconds": _rounded(timeline_start),
            "source_start_seconds": 0.0,
            "duration_seconds": entry["duration_seconds"],
            "speed": 1.0,
            "sync_lock": entry["sync_lock"],
            "sync_group": entry["sync_group"],
        }
        grouped_clips[track_key].append(clip)

    tracks: list[dict[str, Any]] = []
    ordered_keys = sorted(
        grouped_clips,
        key=lambda key: (0 if key[0] == "video" else 1, key[1].casefold(), key[1]),
    )
    for index, key in enumerate(ordered_keys, start=1):
        kind, coverage = key
        clips = sorted(
            grouped_clips[key],
            key=lambda clip: (
                clip["timeline_start_seconds"],
                clip["shot_id"],
                clip["variant_id"],
                clip["stage_id"],
            ),
        )
        label = f"Director {kind.title()}"
        if track_policy == "coverage" and coverage != "main":
            label += f" - {coverage}"
        tracks.append(
            {
                "track_id": f"track-{index:03d}",
                "kind": kind,
                "label": label,
                "clips": clips,
            }
        )

    duration = max(
        (
            clip["timeline_start_seconds"] + clip["duration_seconds"]
            for track in tracks
            for clip in track["clips"]
        ),
        default=0.0,
    )
    projection = {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "plan_id": checked_plan["plan_id"],
        "plan_revision": checked_plan["revision"],
        "plan_signature": checked_plan["plan_signature"],
        "ledger_revision": checked_ledger["ledger_revision"],
        "duration_seconds": _rounded(duration),
        "tracks": tracks,
        "warnings": list(dict.fromkeys(warnings)),
    }
    manifest = {"manifest_id": _short_id("timeline", projection), **projection}
    return validate_timeline_manifest(manifest)


def _index_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    items: dict[str, dict[str, Any]] = {}
    global_cursor = 0.0
    scenes = sorted(plan["scenes"], key=lambda scene: (scene["order"], scene["scene_id"]))
    for scene in scenes:
        scene_start_raw = scene["start_seconds"]
        scene_start = global_cursor if scene_start_raw is None else float(scene_start_raw)
        local_cursor = 0.0
        shots = sorted(scene["shots"], key=lambda shot: (shot["order"], shot["shot_id"]))
        for shot in shots:
            duration = _rounded(float(shot["timing"]["duration_seconds"]))
            explicit_start = shot["timing"]["start_seconds"]
            natural_start = (
                scene_start + local_cursor
                if explicit_start is None
                else scene_start + float(explicit_start)
            )
            local_cursor = max(local_cursor, natural_start - scene_start + duration)
            stages = sorted(
                shot["stages"], key=lambda stage: (stage["order"], stage["stage_id"])
            )
            variants = sorted(
                shot["variants"],
                key=lambda variant: (variant["order"], variant["variant_id"]),
            )
            tags = [str(tag).strip().casefold() for tag in shot["tags"]]
            lyric_moment = str(shot["content"]["lyric_moment"]).strip()
            sync_lock = bool(lyric_moment) or any(
                tag in {"sync_lock", "vocal", "vocal_sync"} or tag.startswith("sync:")
                for tag in tags
            )
            explicit_sync_groups = [
                tag.split(":", 1)[1].strip()
                for tag in tags
                if tag.startswith("sync:") and tag.split(":", 1)[1].strip()
            ]
            if explicit_sync_groups:
                sync_group: str | None = explicit_sync_groups[0]
            elif lyric_moment:
                sync_group = "lyric_" + hashlib.sha256(
                    lyric_moment.encode("utf-8")
                ).hexdigest()[:16]
            elif sync_lock:
                sync_group = shot["shot_id"]
            else:
                sync_group = None
            for variant in variants:
                for stage in stages:
                    item_id = f"{shot['shot_id']}:{variant['variant_id']}:{stage['stage_id']}"
                    if item_id in items:
                        raise TimelineManifestError(
                            f"Director plan produces duplicate work item {item_id!r}."
                        )
                    items[item_id] = {
                        "item_id": item_id,
                        "scene_id": scene["scene_id"],
                        "scene_order": scene["order"],
                        "shot_id": shot["shot_id"],
                        "shot_order": shot["order"],
                        "variant_id": variant["variant_id"],
                        "variant_order": variant["order"],
                        "variant_label": variant["label"],
                        "variant_angle": variant["angle"],
                        "stage_id": stage["stage_id"],
                        "stage_order": stage["order"],
                        "stage_kind": stage["kind"],
                        "natural_start_seconds": _rounded(natural_start),
                        "duration_seconds": duration,
                        "sync_lock": sync_lock,
                        "sync_group": sync_group,
                    }
        scene_duration = max(local_cursor, float(scene["duration_seconds"]))
        global_cursor = max(global_cursor, scene_start + scene_duration)
    return {"items": items}


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    state = candidate["state"]
    entry = candidate["entry"]
    return (
        _STATUS_RANK[state["status"]],
        candidate["event_sequence"],
        state["attempt"],
        state["updated_at"],
        entry["variant_order"],
        state["item_id"],
    )


def _compact_start_map(candidates: list[dict[str, Any]]) -> dict[str, float]:
    starts: dict[str, float] = {}
    cursor = 0.0
    for candidate in candidates:
        entry = candidate["entry"]
        shot_id = entry["shot_id"]
        if shot_id in starts:
            continue
        starts[shot_id] = _rounded(cursor)
        cursor += entry["duration_seconds"]
    return starts


def _coverage_label(entry: Mapping[str, Any]) -> str:
    label = str(entry["variant_label"]).strip()
    angle = str(entry["variant_angle"]).strip()
    return label or angle or str(entry["variant_id"])


def _short_id(prefix: str, value: object) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:20]}"


def _rounded(value: float) -> float:
    return round(float(value), 6)


__all__ = [
    "TIMELINE_GAP_POLICIES",
    "TIMELINE_MANIFEST_SCHEMA_PATH",
    "TIMELINE_SCHEMA_VERSION",
    "TIMELINE_SELECTION_POLICIES",
    "TIMELINE_TRACK_POLICIES",
    "TimelineManifestError",
    "build_timeline_manifest",
    "load_timeline_manifest_schema",
    "validate_timeline_manifest",
]
