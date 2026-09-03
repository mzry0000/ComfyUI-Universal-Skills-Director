"""Thin ComfyUI V1 wrappers for the Universal Skills Director."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from .universal_skills.config import (
    DEFAULT_MODEL,
    IMAGE_DETAIL_LEVELS,
    REASONING_EFFORTS,
    TARGET_PROFILES,
)
from .universal_skills.director_core import (
    select_work_item,
    validate_director_plan,
)
from .universal_skills.director_planner import DirectorPlanner
from .universal_skills.director_profiles import DIRECTOR_PROFILES
from .universal_skills.director_sessions import DirectorSessionStore
from .universal_skills.director_state import (
    DIRECTOR_STATUSES,
    create_ledger,
    is_ledger_stale,
    reconcile_ledger,
    record_result,
    validate_ledger,
    validate_ledger_for_plan,
)
from .universal_skills.director_timeline import (
    TIMELINE_GAP_POLICIES,
    TIMELINE_SELECTION_POLICIES,
    TIMELINE_TRACK_POLICIES,
    build_timeline_manifest,
    validate_timeline_manifest,
)
from .universal_skills.errors import UniversalSkillHostError
from .universal_skills.openai_client import OpenAIResponsesClient
from .universal_skills.target_adapters import build_director_final_prompt


DIRECTOR_CATEGORY = "Universal Skills/Director"
DIRECTOR_SELECTION_MODES = ("next_ready", "exact", "retry_failed")
_NO_SESSIONS = "(no Director sessions saved)"
_SESSION_NAMESPACE = "universal_skills_director"

# Reuse the same lazily initialized Responses client as queue executions repeat.
# API key lookup and SDK import still happen only when a plan request is made.
_SHARED_CLIENT = OpenAIResponsesClient()


def _json_text(value: object) -> str:
    """Serialize one already validated boundary value for STRING outputs."""

    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _session_location() -> tuple[Path, Path]:
    """Return ``(runtime base, trusted base)`` without workflow-supplied paths."""

    try:
        import folder_paths  # type: ignore[import-not-found]
    except ImportError:
        trusted_base = Path(__file__).resolve().parent
        base = trusted_base / "runtime"
    else:
        getter = getattr(folder_paths, "get_output_directory", None)
        if not callable(getter):
            trusted_base = Path(__file__).resolve().parent
            base = trusted_base / "runtime"
        else:
            output_directory = getter()
            if not isinstance(output_directory, (str, Path)) or not str(
                output_directory
            ).strip():
                raise UniversalSkillHostError(
                    "ComfyUI did not provide a usable output directory for Director sessions."
                )
            base = Path(output_directory)
            trusted_base = base
    return base, trusted_base


def _session_base() -> Path:
    """Return the host-configured runtime base used for Director sessions."""

    return _session_location()[0]


def _session_root() -> Path:
    """Return the fixed runtime session root without accepting workflow paths."""

    return _session_base() / _SESSION_NAMESPACE / "sessions"


def _get_session_store() -> DirectorSessionStore:
    """Construct a store below the fixed ComfyUI or package-local runtime root."""

    base, trusted_base = _session_location()
    return DirectorSessionStore(
        base / _SESSION_NAMESPACE / "sessions",
        trusted_base=trusted_base,
    )


def _session_choices() -> list[str]:
    try:
        choices = list(_get_session_store().list_files())
    except UniversalSkillHostError:
        choices = []
    return choices or [_NO_SESSIONS]


class DirectorPlanProjectNode:
    """Create one signed Director plan and its separate empty ledger."""

    CATEGORY = DIRECTOR_CATEGORY
    FUNCTION = "plan_project"
    RETURN_TYPES = (
        "USH_DIRECTOR_PLAN",
        "USH_DIRECTOR_LEDGER",
        "STRING",
        "STRING",
        "STRING",
        "STRING",
    )
    RETURN_NAMES = (
        "plan",
        "ledger",
        "plan_json",
        "ledger_json",
        "warnings",
        "summary",
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple]]:
        return {
            "required": {
                "request": ("STRING", {"default": "", "multiline": True}),
                "specification": ("USH_SPEC", {"forceInput": True}),
                "director_profile": (
                    list(DIRECTOR_PROFILES),
                    {"default": "generic"},
                ),
                "target_profile": (
                    list(TARGET_PROFILES),
                    {"default": "gpt_image_2"},
                ),
                "target_notes": ("STRING", {"default": "", "multiline": True}),
                "model": ("STRING", {"default": DEFAULT_MODEL}),
                "reasoning_effort": (
                    list(REASONING_EFFORTS),
                    {"default": "medium"},
                ),
                "image_detail": (
                    list(IMAGE_DETAIL_LEVELS),
                    {"default": "auto"},
                ),
                "additional_context": (
                    "STRING",
                    {"default": "", "multiline": True},
                ),
            },
            "optional": {
                "previous_plan": ("USH_DIRECTOR_PLAN", {"forceInput": True}),
                "previous_ledger": ("USH_DIRECTOR_LEDGER", {"forceInput": True}),
                "image1": ("IMAGE",),
                "image2": ("IMAGE",),
                "image3": ("IMAGE",),
                "image4": ("IMAGE",),
            },
        }

    def plan_project(
        self,
        request: str,
        specification: dict[str, object],
        director_profile: str,
        target_profile: str,
        target_notes: str = "",
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "medium",
        image_detail: str = "auto",
        additional_context: str = "",
        image1: object | None = None,
        image2: object | None = None,
        image3: object | None = None,
        image4: object | None = None,
        previous_plan: Mapping[str, Any] | None = None,
        previous_ledger: object | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], str, str, str, str]:
        if (previous_plan is None) != (previous_ledger is None):
            raise UniversalSkillHostError(
                "previous_plan and previous_ledger must be connected together."
            )
        checked_previous_plan: dict[str, Any] | None = None
        checked_previous_ledger: dict[str, Any] | None = None
        if previous_plan is not None:
            checked_previous_plan = validate_director_plan(previous_plan)
            checked_previous_ledger = validate_ledger_for_plan(
                checked_previous_plan, previous_ledger
            )
        result = DirectorPlanner(client=_SHARED_CLIENT).plan(
            request=request,
            specification=specification,
            director_profile=director_profile,
            target_profile=target_profile,
            model=model,
            reasoning_effort=reasoning_effort,
            image_detail=image_detail,
            target_notes=target_notes,
            additional_context=additional_context,
            images=(
                ("image1", image1),
                ("image2", image2),
                ("image3", image3),
                ("image4", image4),
            ),
            previous_plan=checked_previous_plan,
        )
        plan = validate_director_plan(result.plan)
        if checked_previous_plan is None:
            ledger = create_ledger(plan)
        else:
            ledger = reconcile_ledger(
                checked_previous_plan, checked_previous_ledger, plan
            )
        ledger = validate_ledger_for_plan(plan, ledger)
        return (
            plan,
            ledger,
            _json_text(plan),
            _json_text(ledger),
            _json_text(plan["warnings"]),
            str(plan["summary"]),
        )


class DirectorValidatePlanNode:
    """Validate plan/ledger boundaries and report whether their identities differ."""

    CATEGORY = DIRECTOR_CATEGORY
    FUNCTION = "validate"
    RETURN_TYPES = (
        "USH_DIRECTOR_PLAN",
        "USH_DIRECTOR_LEDGER",
        "BOOLEAN",
        "STRING",
        "STRING",
    )
    RETURN_NAMES = (
        "plan",
        "ledger",
        "is_stale",
        "validation_json",
        "warnings",
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple]]:
        return {
            "required": {
                "plan": ("USH_DIRECTOR_PLAN", {"forceInput": True}),
                "ledger": ("USH_DIRECTOR_LEDGER", {"forceInput": True}),
            }
        }

    def validate(
        self, plan: Mapping[str, Any], ledger: object
    ) -> tuple[dict[str, Any], dict[str, Any], bool, str, str]:
        checked_plan = validate_director_plan(plan)
        checked_ledger = validate_ledger(ledger)
        stale = is_ledger_stale(checked_plan, checked_ledger)
        if not stale:
            checked_ledger = validate_ledger_for_plan(checked_plan, checked_ledger)
        warnings = list(checked_plan["warnings"])
        if stale:
            warnings.append(
                "Director ledger is stale for this plan; rebuild or reload it before selection."
            )
        diagnostics = {
            "schema_version": "1.0",
            "plan_valid": True,
            "ledger_valid": True,
            "ledger_stale": stale,
            "plan_id": checked_plan["plan_id"],
            "plan_revision": checked_plan["revision"],
            "plan_signature": checked_plan["plan_signature"],
            "ledger_revision": checked_ledger["ledger_revision"],
            "work_item_count": len(checked_ledger["items"]),
        }
        return (
            checked_plan,
            copy.deepcopy(checked_ledger),
            stale,
            _json_text(diagnostics),
            _json_text(warnings),
        )


class DirectorSelectWorkItemNode:
    """Select one dependency-ready ticket and expose its final prompt first."""

    CATEGORY = DIRECTOR_CATEGORY
    FUNCTION = "select"
    RETURN_TYPES = (
        "STRING",
        "STRING",
        "USH_DIRECTOR_WORK_ITEM",
        "STRING",
        "STRING",
        "STRING",
    )
    RETURN_NAMES = (
        "final_prompt",
        "negative_prompt",
        "work_item",
        "work_item_json",
        "timing_json",
        "warnings",
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple]]:
        return {
            "required": {
                "plan": ("USH_DIRECTOR_PLAN", {"forceInput": True}),
                "ledger": ("USH_DIRECTOR_LEDGER", {"forceInput": True}),
                "selection_mode": (
                    list(DIRECTOR_SELECTION_MODES),
                    {"default": "next_ready"},
                ),
                "scene_id": ("STRING", {"default": ""}),
                "shot_id": ("STRING", {"default": ""}),
                "variant_id": ("STRING", {"default": ""}),
                "stage_id": ("STRING", {"default": ""}),
            }
        }

    def select(
        self,
        plan: Mapping[str, Any],
        ledger: object,
        selection_mode: str = "next_ready",
        scene_id: str = "",
        shot_id: str = "",
        variant_id: str = "",
        stage_id: str = "",
    ) -> tuple[str, str, dict[str, Any], str, str, str]:
        checked_plan = validate_director_plan(plan)
        checked_ledger = validate_ledger_for_plan(checked_plan, ledger)
        item = select_work_item(
            checked_plan,
            ledger=checked_ledger,
            scene_id=scene_id,
            shot_id=shot_id,
            variant_id=variant_id,
            stage_id=stage_id,
            selection_mode=selection_mode,
        )
        final_prompt = build_director_final_prompt(checked_plan, item)
        if not isinstance(final_prompt, str) or not final_prompt.strip():
            raise UniversalSkillHostError(
                "Director Target Adapter returned an empty final_prompt STRING."
            )
        negative_prompt = str(item.get("negative_prompt", ""))
        return (
            final_prompt,
            negative_prompt,
            item,
            _json_text(item),
            _json_text(item["timing"]),
            _json_text(checked_plan["warnings"]),
        )


class DirectorRecordResultNode:
    """Apply one validated result event to the separate Director ledger."""

    CATEGORY = DIRECTOR_CATEGORY
    FUNCTION = "record"
    RETURN_TYPES = (
        "USH_DIRECTOR_LEDGER",
        "STRING",
        "STRING",
        "BOOLEAN",
        "STRING",
    )
    RETURN_NAMES = (
        "ledger",
        "ledger_json",
        "receipt_json",
        "changed",
        "warnings",
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple]]:
        return {
            "required": {
                "plan": ("USH_DIRECTOR_PLAN", {"forceInput": True}),
                "ledger": ("USH_DIRECTOR_LEDGER", {"forceInput": True}),
                "work_item": ("USH_DIRECTOR_WORK_ITEM", {"forceInput": True}),
                "status": (list(DIRECTOR_STATUSES), {"default": "succeeded"}),
                "output_ref": ("STRING", {"default": "", "multiline": False}),
                "prompt_id": ("STRING", {"default": "", "multiline": False}),
                "workflow_id": ("STRING", {"default": "", "multiline": False}),
                "error_code": ("STRING", {"default": "", "multiline": False}),
                "error_message": ("STRING", {"default": "", "multiline": True}),
            }
        }

    def record(
        self,
        plan: Mapping[str, Any],
        ledger: object,
        work_item: Mapping[str, Any],
        status: str,
        output_ref: str = "",
        prompt_id: str = "",
        workflow_id: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> tuple[dict[str, Any], str, str, bool, str]:
        checked_plan = validate_director_plan(plan)
        checked_ledger = validate_ledger_for_plan(checked_plan, ledger)
        # The adapter lookup rejects detached, unknown, or stale work-item dicts.
        build_director_final_prompt(checked_plan, work_item)
        update = record_result(
            checked_plan,
            checked_ledger,
            work_item,
            status=status,
            output_ref=output_ref,
            prompt_id=prompt_id,
            workflow_id=workflow_id,
            error_code=error_code,
            error_message=error_message,
        )
        updated_ledger = validate_ledger_for_plan(checked_plan, update.ledger)
        if not any(
            receipt.get("receipt_id") == update.receipt.get("receipt_id")
            for receipt in updated_ledger["receipts"]
        ):
            raise UniversalSkillHostError(
                "Director result receipt was not present in the validated ledger."
            )
        return (
            updated_ledger,
            _json_text(updated_ledger),
            _json_text(update.receipt),
            bool(update.changed),
            _json_text(list(update.warnings)),
        )


class DirectorLoadSessionNode:
    """Load a validated Director snapshot from the fixed session root."""

    CATEGORY = DIRECTOR_CATEGORY
    FUNCTION = "load_session"
    RETURN_TYPES = (
        "USH_DIRECTOR_PLAN",
        "USH_DIRECTOR_LEDGER",
        "INT",
        "STRING",
        "STRING",
        "STRING",
        "STRING",
    )
    RETURN_NAMES = (
        "plan",
        "ledger",
        "session_revision",
        "session_file",
        "saved_at",
        "plan_json",
        "ledger_json",
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple]]:
        choices = _session_choices()
        return {
            "required": {
                "session_file": (choices, {"default": choices[0]}),
            }
        }

    @classmethod
    def IS_CHANGED(cls, session_file: str) -> object:
        try:
            return _get_session_store().fingerprint(session_file)
        except UniversalSkillHostError:
            return float("nan")

    def load_session(
        self, session_file: str
    ) -> tuple[dict[str, Any], dict[str, Any], int, str, str, str, str]:
        snapshot = _get_session_store().load(session_file)
        plan = validate_director_plan(snapshot["plan"])
        ledger = validate_ledger_for_plan(plan, snapshot["ledger"])
        return (
            plan,
            copy.deepcopy(ledger),
            int(snapshot["session_revision"]),
            str(snapshot["session_file"]),
            str(snapshot["saved_at"]),
            _json_text(plan),
            _json_text(ledger),
        )


class DirectorSaveSessionNode:
    """Atomically save one Director snapshot with optimistic revision checking."""

    CATEGORY = DIRECTOR_CATEGORY
    FUNCTION = "save_session"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "STRING", "INT", "STRING")
    RETURN_NAMES = (
        "snapshot_json",
        "session_file",
        "session_revision",
        "saved_at",
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple]]:
        return {
            "required": {
                "plan": ("USH_DIRECTOR_PLAN", {"forceInput": True}),
                "ledger": ("USH_DIRECTOR_LEDGER", {"forceInput": True}),
                "session_name": ("STRING", {"default": "director-session"}),
                "expected_session_revision": (
                    "INT",
                    {"default": -1, "min": -1, "max": 2147483647, "step": 1},
                ),
            }
        }

    def save_session(
        self,
        plan: Mapping[str, Any],
        ledger: object,
        session_name: str,
        expected_session_revision: int = -1,
    ) -> tuple[str, str, int, str]:
        checked_plan = validate_director_plan(plan)
        checked_ledger = validate_ledger_for_plan(checked_plan, ledger)
        snapshot = _get_session_store().save(
            session_name,
            checked_plan,
            checked_ledger,
            expected_session_revision=expected_session_revision,
        )
        # Revalidate the persisted boundary instead of trusting a custom socket.
        persisted_plan = validate_director_plan(snapshot["plan"])
        validate_ledger_for_plan(persisted_plan, snapshot["ledger"])
        return (
            _json_text(snapshot),
            str(snapshot["session_file"]),
            int(snapshot["session_revision"]),
            str(snapshot["saved_at"]),
        )


class DirectorTimelineManifestNode:
    """Build and validate an editor-neutral manifest from current state."""

    CATEGORY = DIRECTOR_CATEGORY
    FUNCTION = "build_manifest"
    RETURN_TYPES = ("USH_TIMELINE_MANIFEST", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("manifest", "manifest_json", "manifest_id", "warnings")

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple]]:
        return {
            "required": {
                "plan": ("USH_DIRECTOR_PLAN", {"forceInput": True}),
                "ledger": ("USH_DIRECTOR_LEDGER", {"forceInput": True}),
                "selection_policy": (
                    list(TIMELINE_SELECTION_POLICIES),
                    {"default": "selected_else_latest_succeeded"},
                ),
                "gap_policy": (
                    list(TIMELINE_GAP_POLICIES),
                    {"default": "preserve"},
                ),
                "track_policy": (
                    list(TIMELINE_TRACK_POLICIES),
                    {"default": "single_track"},
                ),
            }
        }

    def build_manifest(
        self,
        plan: Mapping[str, Any],
        ledger: object,
        selection_policy: str = "selected_else_latest_succeeded",
        gap_policy: str = "preserve",
        track_policy: str = "single_track",
    ) -> tuple[dict[str, Any], str, str, str]:
        checked_plan = validate_director_plan(plan)
        checked_ledger = validate_ledger_for_plan(checked_plan, ledger)
        manifest = validate_timeline_manifest(
            build_timeline_manifest(
                checked_plan,
                checked_ledger,
                selection_policy=selection_policy,
                gap_policy=gap_policy,
                track_policy=track_policy,
            )
        )
        return (
            copy.deepcopy(manifest),
            _json_text(manifest),
            str(manifest["manifest_id"]),
            _json_text(manifest["warnings"]),
        )


__all__ = [
    "DIRECTOR_CATEGORY",
    "DIRECTOR_SELECTION_MODES",
    "DirectorLoadSessionNode",
    "DirectorPlanProjectNode",
    "DirectorRecordResultNode",
    "DirectorSaveSessionNode",
    "DirectorSelectWorkItemNode",
    "DirectorTimelineManifestNode",
    "DirectorValidatePlanNode",
]
