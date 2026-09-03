"""Small shared types for the Specification planner boundary."""

from __future__ import annotations

from typing import Literal, TypedDict


class Specification(TypedDict):
    """Normalized path-backed or browser-embedded ``USH_SPEC`` value."""

    name: str
    version: str
    source_format: Literal["markdown", "json"]
    content: str
    source_file: str
    source_path: str | None

__all__ = [
    "Specification",
]
