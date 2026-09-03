"""Resolve the trusted directories from which Specification files may be read."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

from . import local_config
from .errors import LocalConfigError


PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _comfy_input_root() -> Path | None:
    """Return ComfyUI's input directory when running inside a ComfyUI host."""

    try:
        folder_paths = importlib.import_module("folder_paths")
    except (ImportError, ModuleNotFoundError):
        return None
    getter = getattr(folder_paths, "get_input_directory", None)
    if not callable(getter):
        return None
    try:
        value = getter()
        root = Path(value).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return None
    return root if root.is_dir() else None


def _configured_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for index, value in enumerate(
        local_config.load_local_config().specification_roots,
        start=1,
    ):
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = PACKAGE_ROOT / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError):
            raise LocalConfigError(
                f"specification_roots item {index} in "
                f"{local_config.CONFIG_FILENAME} does not name a readable directory."
            ) from None
        if not resolved.is_dir():
            raise LocalConfigError(
                f"specification_roots item {index} in "
                f"{local_config.CONFIG_FILENAME} must name a directory."
            )
        roots.append(resolved)
    return tuple(roots)


def allowed_specification_roots() -> tuple[Path, ...]:
    """Return deduplicated host-authorized roots in stable priority order.

    ComfyUI's input directory is accepted automatically so uploaded or staged
    files work in ComfyUI and compatible Floyo workers. Operators can add
    mounted directories through ``ush_config.json``.
    """

    candidates: list[Path] = []
    comfy_root = _comfy_input_root()
    if comfy_root is not None:
        candidates.append(comfy_root)
    candidates.extend(_configured_roots())

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        identity = os.path.normcase(str(candidate))
        if identity in seen:
            continue
        seen.add(identity)
        roots.append(candidate)
    return tuple(roots)


__all__ = [
    "PACKAGE_ROOT",
    "allowed_specification_roots",
]
