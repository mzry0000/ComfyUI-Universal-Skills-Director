"""Actual result adapters. Filenames alone can never mark a stage complete."""

from __future__ import annotations

import hashlib
import os
import tempfile
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from ..contracts import fingerprint
from ..image_codec import _first_image_as_float32
from .core import DirectorError
from .sessions import _prepare_bounded_root
from .state import validate_artifact

MAX_RESULT_BYTES = 128 * 1024 * 1024
MAX_RESULT_PIXELS = 64 * 1024 * 1024


def image_fingerprint(image):
    frame, batch = _first_image_as_float32(image, "image")
    if batch != 1:
        raise DirectorError(
            "Director references require exactly one image per input. Split image batches before planning."
        )
    # Use the full input, not the resized preview sent to the planner.
    digest = hashlib.sha256(str(frame.shape).encode("ascii"))
    digest.update(np.ascontiguousarray(frame, dtype="<f4").tobytes())
    return digest.hexdigest()


def text_artifact(text):
    return validate_artifact({"kind": "text", "value": text, "fingerprint": fingerprint(text)})


def _pixel_fingerprint(pixels):
    """Versioned RGB8 identity, independent of PNG encoder/metadata/strides."""
    digest = hashlib.sha256(b"usd-result-rgb8-v1\0")
    digest.update(f"{pixels.shape[1]}x{pixels.shape[0]}\0".encode("ascii"))
    digest.update(np.ascontiguousarray(pixels, dtype=np.uint8).tobytes())
    return digest.hexdigest()


class ArtifactStore:
    def __init__(self, root, *, trusted_base):
        self.raw_base = Path(trusted_base).absolute()
        self.raw_root = Path(root).absolute()
        self.base, self.root = _prepare_bounded_root(self.raw_base, self.raw_root)

    def _check_root(self):
        base, root = _prepare_bounded_root(self.raw_base, self.raw_root)
        if base != self.base or root != self.root:
            raise DirectorError("Result storage location changed.")

    def _path(self, artifact):
        self._check_root()
        artifact = validate_artifact(artifact)
        if artifact["kind"] != "image":
            raise DirectorError("Expected an image result.")
        path = self.root / artifact["value"]
        if path.is_symlink() or (
            path.exists() and (path.resolve().parent != self.root or path.stat().st_nlink != 1)
        ):
            raise DirectorError("Unsafe result path.")
        return path

    def save_image(self, image, *, expected_artifact=None):
        frame, batch = _first_image_as_float32(image, "generated_image")
        if batch != 1 or frame.shape[0] * frame.shape[1] > MAX_RESULT_PIXELS:
            raise DirectorError(
                "Record Image requires exactly one RGB image, at most 64 megapixels."
            )
        if not np.isfinite(frame).all() or np.any((frame < 0) | (frame > 1)):
            raise DirectorError("Generated IMAGE pixels must be finite values in 0..1.")
        pixels = np.rint(frame * 255).astype(np.uint8)
        pixel_hash = _pixel_fingerprint(pixels)
        if expected_artifact is not None:
            # Keep the exact old record (including legacy byte-hash identities).
            # Do not re-encode, migrate the ledger or create an orphan on retries.
            expected_artifact = validate_artifact(expected_artifact)
            stored_pixels = self._load_pixels(expected_artifact)
            if pixel_hash != _pixel_fingerprint(stored_pixels):
                raise DirectorError(
                    "This attempt already has a different image result; it cannot be overwritten."
                )
            return expected_artifact
        stream = BytesIO()
        Image.fromarray(pixels).save(stream, format="PNG")
        data = stream.getvalue()
        if len(data) > MAX_RESULT_BYTES:
            raise DirectorError("Generated PNG exceeds the 128 MiB result limit.")
        digest = hashlib.sha256(data).hexdigest()
        artifact = {
            "kind": "image",
            "fingerprint": pixel_hash,
            "file_sha256": digest,
            "value": digest + ".png",
        }
        path = self._path(artifact)
        if path.exists():
            self.load_image(artifact)
            return artifact
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root, prefix=".result-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._path(artifact)
            os.replace(temporary, path)
            temporary = None
        except OSError:
            raise DirectorError("Could not save the generated image result.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return artifact

    def _load_pixels(self, artifact):
        artifact = validate_artifact(artifact)
        path = self._path(artifact)
        try:
            with path.open("rb") as handle:
                if os.fstat(handle.fileno()).st_nlink != 1:
                    raise DirectorError("Result hard links are not permitted.")
                data = handle.read(MAX_RESULT_BYTES + 1)
            if len(data) > MAX_RESULT_BYTES or hashlib.sha256(data).hexdigest() != artifact.get(
                "file_sha256", artifact["fingerprint"]
            ):
                raise DirectorError("Stored result content changed or exceeds the size limit.")
            with Image.open(BytesIO(data)) as picture:
                if (
                    picture.width * picture.height > MAX_RESULT_PIXELS
                    or picture.format != "PNG"
                ):
                    raise DirectorError("Invalid stored image result.")
                pixels = np.asarray(picture.convert("RGB"), dtype=np.uint8)
            if (
                "file_sha256" in artifact
                and _pixel_fingerprint(pixels) != artifact["fingerprint"]
            ):
                raise DirectorError("Stored result pixel fingerprint mismatch.")
            return pixels
        except (OSError, ValueError, Image.DecompressionBombError):
            raise DirectorError(
                "Stored image result is missing or unreadable. Restore the result before continuing."
            ) from None

    def load_image(self, artifact):
        return self._load_pixels(artifact).astype(np.float32)[None, ...] / 255.0
