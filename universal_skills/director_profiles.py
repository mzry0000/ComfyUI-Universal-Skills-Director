"""Declarative vertical profiles layered over the generic Director plan."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import UniversalSkillHostError


DIRECTOR_PROFILES = (
    "generic",
    "music_video",
    "business_ad",
    "ugc_ad",
    "short_film",
)


@dataclass(frozen=True)
class DirectorProfileAdapter:
    """Small, model-independent policy bundle for one production vertical."""

    name: str
    display_name: str
    planning_guidance: tuple[str, ...]


_PROFILE_ADAPTERS = {
    "generic": DirectorProfileAdapter(
        name="generic",
        display_name="Generic Director",
        planning_guidance=(
            "Break the request into the smallest useful ordered scenes and shots.",
            "Make every shot executable on its own and declare its stages and dependencies.",
            "Use variants for intentional angle or take alternatives, not unrelated concepts.",
        ),
    ),
    "music_video": DirectorProfileAdapter(
        name="music_video",
        display_name="Music Video",
        planning_guidance=(
            "Align shot start times and durations to the supplied song or timing information.",
            "Use lyric_moment for vocal or lyrical sync and tags for coverage intent.",
            "Plan identity-continuous keyframes before dependent video stages.",
            "Balance performance, narrative, detail, and environmental coverage.",
        ),
    ),
    "business_ad": DirectorProfileAdapter(
        name="business_ad",
        display_name="Business Advertisement",
        planning_guidance=(
            "Preserve product and brand facts from references without inventing claims.",
            "Create a clear hook, problem or context, product demonstration, proof, and CTA.",
            "Reserve a readable final shot for approved end-card text when requested.",
        ),
    ),
    "ugc_ad": DirectorProfileAdapter(
        name="ugc_ad",
        display_name="UGC Advertisement",
        planning_guidance=(
            "Favor credible creator-led framing, natural dialogue, and simple product handling.",
            "Keep product appearance and factual claims anchored to supplied references.",
            "Use concise beats that can be generated or recorded as separate takes.",
        ),
    ),
    "short_film": DirectorProfileAdapter(
        name="short_film",
        display_name="Short Film",
        planning_guidance=(
            "Preserve cast, wardrobe, props, geography, lighting, and screen direction.",
            "Build coverage that supports an editable sequence rather than isolated hero images.",
            "Keep dialogue, action, camera movement, and duration achievable in each shot.",
        ),
    ),
}


def get_director_profile(value: object) -> DirectorProfileAdapter:
    """Resolve a supported Director vertical or raise an actionable error."""

    if not isinstance(value, str) or value not in _PROFILE_ADAPTERS:
        choices = ", ".join(DIRECTOR_PROFILES)
        raise UniversalSkillHostError(f"director_profile must be one of: {choices}.")
    return _PROFILE_ADAPTERS[value]


def profile_planning_guidance(value: object) -> str:
    """Render one profile's rules for the OpenAI planner instructions."""

    adapter = get_director_profile(value)
    return "\n".join(f"- {line}" for line in adapter.planning_guidance)


def _iter_shots(plan: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    scenes = plan.get("scenes", [])
    if not isinstance(scenes, list):
        return
    for scene in scenes:
        if not isinstance(scene, Mapping):
            continue
        shots = scene.get("shots", [])
        if not isinstance(shots, list):
            continue
        for shot in shots:
            if isinstance(shot, Mapping):
                yield shot


def _asset_matches(plan: Mapping[str, Any], *tokens: str) -> bool:
    assets = plan.get("assets", [])
    for asset in assets if isinstance(assets, list) else ():
        if not isinstance(asset, Mapping):
            continue
        searchable = " ".join(
            str(asset.get(name, "")) for name in ("asset_id", "kind", "role")
        ).lower()
        if any(token in searchable for token in tokens):
            return True
    return False


def profile_warnings(plan: Mapping[str, Any]) -> tuple[str, ...]:
    """Return non-blocking vertical review warnings for a valid generic plan."""

    profile = get_director_profile(plan.get("director_profile"))
    warnings: list[str] = []
    shots = tuple(_iter_shots(plan))

    if profile.name == "music_video":
        if not _asset_matches(plan, "audio", "song", "music"):
            warnings.append(
                "Music-video plan has no audio/song asset; timing must be verified externally."
            )
        if any(
            isinstance(shot.get("timing"), Mapping)
            and shot["timing"].get("start_seconds") is None
            for shot in shots
        ):
            warnings.append(
                "Music-video plan contains shots without explicit start_seconds."
            )
        has_lyric_or_instrumental = any(
            (
                isinstance(shot.get("content"), Mapping)
                and bool(str(shot["content"].get("lyric_moment", "")).strip())
            )
            or any(
                str(tag).strip().lower() == "instrumental"
                for tag in shot.get("tags", [])
            )
            for shot in shots
        )
        if not has_lyric_or_instrumental:
            warnings.append(
                "Music-video plan has neither lyric moments nor an instrumental tag."
            )

    if profile.name in {"business_ad", "ugc_ad"} and not _asset_matches(
        plan, "product"
    ):
        warnings.append(
            "Advertisement plan has no asset identified with a product role."
        )

    if profile.name == "business_ad" and shots:
        last = shots[-1]
        content = last.get("content", {})
        text_overlay = (
            str(content.get("text_overlay", "")).strip()
            if isinstance(content, Mapping)
            else ""
        )
        tags = {str(tag).strip().lower() for tag in last.get("tags", [])}
        if not text_overlay and not ({"end_card", "cta"} & tags):
            warnings.append(
                "Business-ad plan has no explicit end-card/CTA in its final shot."
            )

    if profile.name == "ugc_ad":
        if not any(
            isinstance(shot.get("content"), Mapping)
            and bool(str(shot["content"].get("dialogue", "")).strip())
            for shot in shots
        ):
            warnings.append("UGC-ad plan has no creator dialogue.")

    if profile.name == "short_film":
        globals_value = plan.get("globals", {})
        continuity = (
            globals_value.get("continuity", [])
            if isinstance(globals_value, Mapping)
            else []
        )
        if not continuity:
            warnings.append("Short-film plan has no explicit continuity rules.")

    # Stable order and exact-value deduplication make these safe to hash or test.
    return tuple(dict.fromkeys(warnings))


__all__ = [
    "DIRECTOR_PROFILES",
    "DirectorProfileAdapter",
    "get_director_profile",
    "profile_planning_guidance",
    "profile_warnings",
]
