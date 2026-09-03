"""Central V1 limits and supported UI choices."""

from __future__ import annotations

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
TARGET_PROFILES = (
    "gpt_image_2",
    "generic_image_generation",
    "generic_image_edit",
    "anime_diffusion",
    "photoreal_diffusion",
    "video_generation",
    "video_image_to_video",
    "text_generation",
    "structured_json",
    "custom",
)
