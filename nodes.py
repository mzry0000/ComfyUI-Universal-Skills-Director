"""Thin ComfyUI wrappers around the Specification planner."""

from __future__ import annotations

from .universal_skills.config import (
    DEFAULT_MODEL,
    IMAGE_DETAIL_LEVELS,
    REASONING_EFFORTS,
    TARGET_PROFILES,
)
from .universal_skills.errors import UniversalSkillHostError
from .universal_skills.openai_client import OpenAIResponsesClient
from .universal_skills.composer import PromptComposer
from .universal_skills.specification import (
    load_specification_selection,
    specification_selection_fingerprint,
)
from .universal_skills.types import Specification


# One process-wide client so the underlying OpenAI SDK client (and its HTTP
# connection pool) is created once and reused across queue executions instead
# of being rebuilt and leaked on every run. SDK import and API key lookup
# stay deferred until the first planner request.
_SHARED_CLIENT = OpenAIResponsesClient()


class LoadSpecificationNode:
    """Load one browser-embedded or trusted-path file as ``USH_SPEC``."""

    CATEGORY = "Universal Skills"
    FUNCTION = "load"
    RETURN_TYPES = ("USH_SPEC",)
    RETURN_NAMES = ("specification",)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple]]:
        return {
            "required": {
                "spec_file": (
                    "STRING",
                    {
                        "default": "",
                        "placeholder": ("Trusted server path (leave blank when using D&D)"),
                        "dynamicPrompts": False,
                        "advanced": True,
                    },
                ),
                "uploaded_filename": (
                    "STRING",
                    {
                        "default": "",
                        "placeholder": "Filled by browser D&D",
                        "dynamicPrompts": False,
                        "advanced": True,
                    },
                ),
                "uploaded_content": (
                    "STRING",
                    {
                        "default": "",
                        "placeholder": "Filled by browser D&D; embedded in workflow",
                        "multiline": True,
                        "dynamicPrompts": False,
                        "advanced": True,
                    },
                ),
            }
        }

    @classmethod
    def IS_CHANGED(
        cls,
        spec_file: str,
        uploaded_filename: str = "",
        uploaded_content: str = "",
    ) -> object:
        try:
            return specification_selection_fingerprint(
                spec_file,
                uploaded_filename,
                uploaded_content,
            )
        except UniversalSkillHostError:
            # Force execution so ``load`` can surface the actionable domain error.
            return float("nan")

    def load(
        self,
        spec_file: str,
        uploaded_filename: str = "",
        uploaded_content: str = "",
    ) -> tuple[Specification]:
        return (
            load_specification_selection(
                spec_file,
                uploaded_filename,
                uploaded_content,
            ),
        )


class PromptPlannerNode:
    """Legacy widget/output ordering; execution uses the v2 Composer."""

    DEPRECATED = True

    CATEGORY = "Universal Skills"
    FUNCTION = "plan"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = (
        "final_prompt",
        "plan_json",
        "applied_specification",
        "warnings",
        "summary",
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple]]:
        return {
            "required": {
                "request": ("STRING", {"default": "", "multiline": True}),
                "specification": ("USH_SPEC", {"forceInput": True}),
                "target_profile": (list(TARGET_PROFILES), {"default": "gpt_image_2"}),
                "target_notes": ("STRING", {"default": "", "multiline": True}),
                "model": ("STRING", {"default": DEFAULT_MODEL}),
                "reasoning_effort": (
                    list(REASONING_EFFORTS),
                    {"default": "medium"},
                ),
                "image_detail": (list(IMAGE_DETAIL_LEVELS), {"default": "auto"}),
                "additional_context": (
                    "STRING",
                    {"default": "", "multiline": True},
                ),
            },
            "optional": {
                "image1": ("IMAGE",),
                "image2": ("IMAGE",),
                "image3": ("IMAGE",),
                "image4": ("IMAGE",),
            },
        }

    def plan(
        self,
        request: str,
        specification: dict[str, object],
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
    ) -> tuple[str, str, str, str, str]:
        result = PromptComposer(client=_SHARED_CLIENT).compose(
            request=request,
            specification=specification,
            target_profile=target_profile,
            model=model,
            reasoning_effort=reasoning_effort,
            image_detail=image_detail,
            context="\n\n".join(value for value in (target_notes, additional_context) if value),
            images=(
                ("image1", image1),
                ("image2", image2),
                ("image3", image3),
                ("image4", image4),
            ),
        )
        return result.legacy_tuple()


class PromptComposerNode:
    CATEGORY = "Universal Skills"
    FUNCTION = "compose"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("final_prompt", "warnings")
    DESCRIPTION = "Compose a concise final prompt from a Skill, request and reference images. Connect the images to the generator too."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "specification": ("USH_SPEC", {"forceInput": True}),
                "request": (
                    "STRING",
                    {"default": "", "multiline": True, "dynamicPrompts": False},
                ),
                "target_profile": (list(TARGET_PROFILES), {"default": "gpt_image_2"}),
                "generation_id": ("INT", {"default": 0, "min": 0, "max": 2147483647}),
                "context": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "dynamicPrompts": False,
                        "advanced": True,
                    },
                ),
                "model": ("STRING", {"default": DEFAULT_MODEL, "advanced": True}),
                "reasoning_effort": (
                    list(REASONING_EFFORTS),
                    {"default": "medium", "advanced": True},
                ),
                "image_detail": (
                    list(IMAGE_DETAIL_LEVELS),
                    {"default": "auto", "advanced": True},
                ),
                "max_output_tokens": (
                    "INT",
                    {"default": 8192, "min": 1024, "max": 65536, "advanced": True},
                ),
            },
            "optional": {f"image{i}": ("IMAGE",) for i in range(1, 5)},
        }

    def compose(
        self,
        specification,
        request,
        target_profile="gpt_image_2",
        generation_id=0,
        context="",
        model=DEFAULT_MODEL,
        reasoning_effort="medium",
        image_detail="auto",
        max_output_tokens=8192,
        image1=None,
        image2=None,
        image3=None,
        image4=None,
    ):
        result = PromptComposer(client=_SHARED_CLIENT).compose(
            specification=specification,
            request=request,
            target_profile=target_profile,
            context=context,
            model=model,
            reasoning_effort=reasoning_effort,
            image_detail=image_detail,
            max_output_tokens=max_output_tokens,
            generation_id=generation_id,
            images=list(
                zip(("image1", "image2", "image3", "image4"), (image1, image2, image3, image4))
            ),
        )
        return result.final_prompt, "\n".join(result.warnings)


__all__ = [
    "LoadSpecificationNode",
    "PromptPlannerNode",
    "PromptComposerNode",
]
