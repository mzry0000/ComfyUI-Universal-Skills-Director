"""Shared limits and supported UI/profile contracts."""

from __future__ import annotations

from typing import Literal, get_args

API_KEY_ENV = "OPENAI_API_KEY"
TIMEOUT_ENV = "USH_TIMEOUT_SECONDS"
DEFAULT_MODEL = "gpt-5.6"
DEFAULT_TIMEOUT_SECONDS = 120.0

MAX_IMAGES = 4
MAX_IMAGE_EDGE = 2048
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REQUEST_CHARS = 20_000
MAX_CONTEXT_CHARS = 20_000
MAX_SPECIFICATION_BYTES = 256 * 1024
MAX_SPECIFICATION_NAME_CHARS = 200
MAX_SPECIFICATION_VERSION_CHARS = 100

REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
IMAGE_DETAIL_LEVELS = ("auto", "low", "high", "original")
ImageTargetProfile = Literal[
    "gpt_image_2",
    "generic_image_generation",
    "generic_image_edit",
    "anime_diffusion",
    "photoreal_diffusion",
    "custom",
]
TextTargetProfile = Literal["text_generation", "structured_json", "custom"]
DirectorTargetProfile = Literal[ImageTargetProfile, TextTargetProfile]
IMAGE_TARGETS = frozenset(get_args(ImageTargetProfile))
TEXT_TARGETS = frozenset(get_args(TextTargetProfile))
TARGET_PROFILES = (
    *get_args(ImageTargetProfile)[:-1],
    "video_generation",
    "video_image_to_video",
    *get_args(TextTargetProfile),
)
