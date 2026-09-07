"""Convert ComfyUI IMAGE values into bounded RGB PNG data URLs."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from typing import Sequence

import numpy as np
from PIL import Image

from .config import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE, MAX_IMAGES
from .errors import ImageEncodingError


@dataclass(frozen=True)
class EncodedImage:
    """A safe, API-ready image plus non-sensitive normalization warnings."""

    input_name: str
    data_url: str
    width: int
    height: int
    byte_length: int
    warnings: tuple[str, ...] = ()


def _shape_of(image: object, input_name: str) -> tuple[int, ...]:
    shape = getattr(image, "shape", None)
    if shape is None:
        raise ImageEncodingError(
            f"{input_name}: expected a ComfyUI IMAGE with shape [B,H,W,3]."
        )
    try:
        return tuple(int(dimension) for dimension in shape)
    except (TypeError, ValueError) as exc:
        raise ImageEncodingError(
            f"{input_name}: could not read the IMAGE tensor shape."
        ) from exc


def _first_image_as_float32(image: object, input_name: str) -> tuple[np.ndarray, int]:
    shape = _shape_of(image, input_name)
    if len(shape) != 4:
        raise ImageEncodingError(
            f"{input_name}: expected IMAGE rank 4 [B,H,W,3], got rank {len(shape)}."
        )

    batch_size, height, width, channels = shape
    if batch_size < 1 or height < 1 or width < 1:
        raise ImageEncodingError(
            f"{input_name}: IMAGE batch, height, and width must all be positive."
        )
    if channels != 3:
        raise ImageEncodingError(f"{input_name}: expected 3 RGB channels, got {channels}.")

    try:
        value = image[0]  # Transfer and encode only the first batch item.
        detach = getattr(value, "detach", None)
        if callable(detach):
            value = detach()
        cpu = getattr(value, "cpu", None)
        if callable(cpu):
            value = cpu()
        as_float = getattr(value, "float", None)
        if callable(as_float):
            value = as_float()
        as_numpy = getattr(value, "numpy", None)
        if callable(as_numpy):
            value = as_numpy()
        array = np.asarray(value, dtype=np.float32)
    except Exception as exc:
        raise ImageEncodingError(
            f"{input_name}: could not move the first IMAGE batch item to CPU memory."
        ) from exc

    expected_shape = (height, width, 3)
    if array.shape != expected_shape:
        raise ImageEncodingError(
            f"{input_name}: first IMAGE item has shape {array.shape}, expected {expected_shape}."
        )
    return array, batch_size


def encode_comfy_image(
    image: object,
    input_name: str = "image",
    *,
    max_edge: int = MAX_IMAGE_EDGE,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> EncodedImage:
    """Encode the first image in a BHWC RGB batch as a bounded PNG data URL."""

    if max_edge < 1 or max_bytes < 1:
        raise ImageEncodingError("Image encoding limits must be positive integers.")
    if not isinstance(input_name, str) or not input_name.strip():
        raise ImageEncodingError("Image input names must be non-empty strings.")

    frame, batch_size = _first_image_as_float32(image, input_name)
    warnings: list[str] = []
    if batch_size > 1:
        warnings.append(
            f"{input_name}: batch size {batch_size}; only the first image was used."
        )

    if not np.isfinite(frame).all():
        frame = np.nan_to_num(frame, nan=0.0, posinf=1.0, neginf=0.0)
        warnings.append(f"{input_name}: non-finite pixel values were replaced before encoding.")

    if np.any((frame < 0.0) | (frame > 1.0)):
        warnings.append(f"{input_name}: pixel values outside 0..1 were clamped.")
    frame = np.clip(frame, 0.0, 1.0)
    pixels = np.rint(frame * 255.0).astype(np.uint8)
    pil_image = Image.fromarray(pixels)

    original_width, original_height = pil_image.size
    if max(original_width, original_height) > max_edge:
        scale = max_edge / float(max(original_width, original_height))
        resized_width = max(1, int(round(original_width * scale)))
        resized_height = max(1, int(round(original_height * scale)))
        resampling = getattr(Image, "Resampling", Image)
        pil_image = pil_image.resize(
            (resized_width, resized_height), resample=resampling.LANCZOS
        )
        warnings.append(
            f"{input_name}: resized from {original_width}x{original_height} to "
            f"{resized_width}x{resized_height} to satisfy the {max_edge}px edge limit."
        )

    output = BytesIO()
    try:
        pil_image.save(output, format="PNG")
    except Exception as exc:
        raise ImageEncodingError(f"{input_name}: PNG encoding failed.") from exc
    png_bytes = output.getvalue()
    if len(png_bytes) > max_bytes:
        raise ImageEncodingError(
            f"{input_name}: encoded PNG is {len(png_bytes)} bytes, exceeding the "
            f"{max_bytes}-byte V1 limit."
        )

    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    width, height = pil_image.size
    return EncodedImage(
        input_name=input_name,
        data_url=data_url,
        width=width,
        height=height,
        byte_length=len(png_bytes),
        warnings=tuple(warnings),
    )


def encode_optional_images(
    images: Sequence[tuple[str, object | None]],
    *,
    max_edge: int = MAX_IMAGE_EDGE,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> tuple[list[EncodedImage], list[str]]:
    """Encode connected named IMAGE inputs, skipping unconnected ``None`` values."""

    if len(images) > MAX_IMAGES:
        raise ImageEncodingError(f"At most {MAX_IMAGES} IMAGE inputs are supported in V1.")

    encoded: list[EncodedImage] = []
    warnings: list[str] = []
    for input_name, image in images:
        if image is None:
            continue
        result = encode_comfy_image(
            image,
            input_name=input_name,
            max_edge=max_edge,
            max_bytes=max_bytes,
        )
        encoded.append(result)
        warnings.extend(result.warnings)
    return encoded, warnings


__all__ = ["EncodedImage", "encode_comfy_image", "encode_optional_images"]
