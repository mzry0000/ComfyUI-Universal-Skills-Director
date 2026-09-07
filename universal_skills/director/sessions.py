"""Safe, revision-checked persistence for Director plan and ledger sessions."""

from __future__ import annotations

import copy
import errno
import hashlib
import math
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, cast

from ..contracts import canonical_json, parse_strict_json
from .core import validate_director_plan
from .state import (
    DIRECTOR_STATE_SCHEMA_VERSION,
    DirectorStateError,
    utc_timestamp,
    validate_ledger_for_plan,
)


MAX_DIRECTOR_SESSION_BYTES = 8 * 1024 * 1024
MAX_DIRECTOR_SESSION_NAME_CHARS = 96
DEFAULT_DIRECTOR_SESSION_LOCK_TIMEOUT_SECONDS = 10.0
MAX_DIRECTOR_SESSION_LOCK_TIMEOUT_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.05
_LOCK_FILENAME = ".director-session.lock"

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.RLock] = {}


class DirectorSessionError(DirectorStateError):
    """Raised when a Director session cannot be safely loaded or saved."""


class DirectorSessionConflictError(DirectorSessionError):
    """Raised when optimistic session revision checking rejects an overwrite."""


class DirectorSessionStore:
    """Persist sessions below one injected, fixed root directory.

    Workflow inputs select only a single JSON filename; they can never supply a
    directory.  Writes use a same-directory temporary file plus ``os.replace``
    and compare the caller's expected storage revision while holding a
    process-wide lock and an OS-level lock for the root.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        trusted_base: str | os.PathLike[str] | None = None,
        lock_timeout_seconds: float = DEFAULT_DIRECTOR_SESSION_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        """Create a store, optionally bounding ``root`` below ``trusted_base``.

        ``trusted_base`` is intended for host-configured locations such as the
        ComfyUI output directory.  Existing symlink or junction ancestors below
        that base are resolved and rejected before any missing directories are
        created when they would escape the base.
        """

        self._lock_timeout_seconds = _normalize_lock_timeout(lock_timeout_seconds)
        raw_root = _absolute_path(root)
        if trusted_base is None:
            # Preserve the original one-argument API: callers may supply a root
            # whose parent does not exist yet.  Host integrations should pass a
            # known, existing trusted_base to get the stronger boundary.
            try:
                raw_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DirectorSessionError(
                    "Director session directory is unavailable."
                ) from exc
            raw_base = raw_root.parent
        else:
            raw_base = _absolute_path(trusted_base)

        resolved_base, resolved_root = _prepare_bounded_root(raw_base, raw_root)
        self._trusted_base_path = raw_base
        self._trusted_base_identity = resolved_base
        self._trusted_base_stat = _directory_identity(resolved_base)
        self._requested_root = raw_root
        self.root = resolved_root
        self._root_stat = _directory_identity(resolved_root)
        self._lock_path = self.root / _LOCK_FILENAME
        self._lock = _lock_for_root(resolved_root)

    def list_files(self) -> tuple[str, ...]:
        """List direct, regular, non-symlink JSON session files."""

        with self._lock:
            self._assert_root_boundary()
            try:
                files = [
                    path.name
                    for path in self.root.iterdir()
                    if path.suffix.lower() == ".json"
                    and path.is_file()
                    and not path.is_symlink()
                    and path.resolve(strict=True).parent == self.root
                ]
            except OSError as exc:
                raise DirectorSessionError("Director sessions could not be listed.") from exc
        return tuple(sorted(files, key=lambda name: (name.casefold(), name)))

    def fingerprint(self, session_file: str) -> str:
        """Hash one bounded session file for ComfyUI ``IS_CHANGED``."""

        with self._lock:
            self._assert_root_boundary()
            data = self._read_bytes(self._resolve_filename(session_file, must_exist=True))
        return f"sha256:{hashlib.sha256(data).hexdigest()}"

    def load(self, session_file: str) -> dict[str, Any]:
        """Load and validate one session snapshot."""

        with self._lock:
            self._assert_root_boundary()
            path = self._resolve_filename(session_file, must_exist=True)
            data = self._read_bytes(path)
            snapshot = self._decode_snapshot(data, expected_file=path.name)
        return snapshot

    def save(
        self,
        session_name: str,
        plan: Mapping[str, Any],
        ledger: object,
        *,
        expected_session_revision: int = -1,
        saved_at: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create or compare-and-swap a session snapshot.

        ``expected_session_revision=-1`` means create-only.  Updating an
        existing file requires the exact revision returned by ``load`` or the
        previous ``save`` call.
        """

        if (
            isinstance(expected_session_revision, bool)
            or not isinstance(expected_session_revision, int)
            or expected_session_revision < -1
        ):
            raise DirectorSessionError(
                "expected_session_revision must be -1 or a non-negative integer."
            )
        # Detach workflow-owned dicts before validation.  A caller may retain
        # and mutate the original objects while this save waits for a process
        # lock; persisted state must only come from this isolated snapshot.
        try:
            plan_snapshot = copy.deepcopy(plan)
            ledger_snapshot = copy.deepcopy(ledger)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise DirectorSessionError(
                "Director session inputs could not be snapshotted safely."
            ) from exc
        checked_plan = validate_director_plan(plan_snapshot)
        checked_ledger = validate_ledger_for_plan(checked_plan, ledger_snapshot)
        timestamp = _bounded_required_text(saved_at or utc_timestamp(), "saved_at", 128)

        with self._lock:
            self._assert_root_boundary()
            # The revision read, comparison, and replace are one cross-process
            # critical section.  The lock file is intentionally persistent:
            # deleting it after release could split waiters across two inodes.
            with self._interprocess_lock():
                self._assert_root_boundary()
                path = self._resolve_filename(session_name, must_exist=False)
                exists = path.exists()
                if exists:
                    if path.is_symlink() or not path.is_file():
                        raise DirectorSessionError(
                            "Director session target must be a regular file."
                        )
                    current = self._decode_snapshot(
                        self._read_bytes(path), expected_file=path.name
                    )
                    current_revision = current["session_revision"]
                    if expected_session_revision == -1:
                        raise DirectorSessionConflictError(
                            f"Director session {path.name!r} already exists; load it before saving."
                        )
                    if current_revision != expected_session_revision:
                        raise DirectorSessionConflictError(
                            f"Director session {path.name!r} changed from revision "
                            f"{expected_session_revision} to {current_revision}; reload it first."
                        )
                    next_revision = current_revision + 1
                else:
                    if expected_session_revision != -1:
                        raise DirectorSessionConflictError(
                            f"Director session {path.name!r} does not exist; "
                            "save it as a new session."
                        )
                    next_revision = 1

                snapshot: dict[str, Any] = {
                    "schema_version": DIRECTOR_STATE_SCHEMA_VERSION,
                    "session_file": path.name,
                    "session_revision": next_revision,
                    "saved_at": timestamp,
                    "plan": checked_plan,
                    "ledger": checked_ledger,
                }
                encoded = (canonical_json(snapshot) + "\n").encode("utf-8")
                if len(encoded) > MAX_DIRECTOR_SESSION_BYTES:
                    raise DirectorSessionError(
                        f"Director session exceeds {MAX_DIRECTOR_SESSION_BYTES} bytes."
                    )
                # Reparse and validate the exact bytes before they can replace a
                # valid snapshot.  This also gives the caller a fully detached
                # result without a post-write validation failure window.
                validated_snapshot = self._decode_snapshot(encoded, expected_file=path.name)
                self._atomic_replace(path, encoded)
                return validated_snapshot

    def _assert_root_boundary(self) -> None:
        """Reject a root/base that was replaced or redirected after setup."""

        try:
            current_base = self._trusted_base_path.resolve(strict=True)
            current_root = self._requested_root.resolve(strict=True)
        except OSError as exc:
            raise DirectorSessionError(
                "Director session root boundary is unavailable."
            ) from exc
        if (
            current_base != self._trusted_base_identity
            or _directory_identity(current_base) != self._trusted_base_stat
        ):
            raise DirectorSessionError("Director trusted base changed after initialization.")
        if not _is_within(current_root, current_base):
            raise DirectorSessionError("Director session root escaped its trusted base.")
        if current_root != self.root or _directory_identity(current_root) != self._root_stat:
            raise DirectorSessionError("Director session root changed after initialization.")

    @contextmanager
    def _interprocess_lock(self) -> Iterator[None]:
        descriptor = _open_lock_file(self._lock_path, self.root)
        acquired = False
        deadline = time.monotonic() + self._lock_timeout_seconds
        try:
            while True:
                try:
                    acquired = _try_lock_descriptor(descriptor)
                except OSError as exc:
                    raise DirectorSessionError(
                        "Director session process lock could not be acquired."
                    ) from exc
                if acquired:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DirectorSessionError(
                        "Timed out waiting for the Director session process lock."
                    )
                time.sleep(min(_LOCK_POLL_SECONDS, remaining))
            yield
        finally:
            if acquired:
                _unlock_descriptor(descriptor)
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _resolve_filename(self, value: str, *, must_exist: bool) -> Path:
        filename = _normalize_session_filename(value)
        candidate = self.root / filename
        if candidate.parent != self.root:
            raise DirectorSessionError("Director session must stay inside the session root.")
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink():
                raise DirectorSessionError("Director session symlinks are not allowed.")
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                raise DirectorSessionError(
                    "Director session path could not be resolved."
                ) from exc
            if resolved.parent != self.root:
                raise DirectorSessionError(
                    "Director session resolves outside the session root."
                )
            candidate = resolved
        elif must_exist:
            raise DirectorSessionError(f"Director session {filename!r} was not found.")
        return candidate

    def _read_bytes(self, path: Path) -> bytes:
        try:
            size = path.stat().st_size
            if size <= 0:
                raise DirectorSessionError("Director session is empty.")
            if size > MAX_DIRECTOR_SESSION_BYTES:
                raise DirectorSessionError(
                    f"Director session exceeds {MAX_DIRECTOR_SESSION_BYTES} bytes."
                )
            data = path.read_bytes()
        except DirectorSessionError:
            raise
        except OSError as exc:
            raise DirectorSessionError("Director session could not be read.") from exc
        if len(data) != size:
            raise DirectorSessionError("Director session changed while it was being read.")
        return data

    def _decode_snapshot(self, data: bytes, *, expected_file: str) -> dict[str, Any]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DirectorSessionError("Director session must be UTF-8 JSON.") from exc
        try:
            value = parse_strict_json(text)
        except DirectorStateError as exc:
            raise DirectorSessionError(str(exc)) from exc
        if not isinstance(value, dict):
            raise DirectorSessionError("Director session root must be an object.")
        required = {
            "schema_version",
            "session_file",
            "session_revision",
            "saved_at",
            "plan",
            "ledger",
        }
        if set(value) != required:
            raise DirectorSessionError(
                "Director session has missing or unsupported top-level fields."
            )
        if value["schema_version"] != DIRECTOR_STATE_SCHEMA_VERSION:
            raise DirectorSessionError(
                "Director v2 requires a 2.0 session. v1 files are not modified; use the old release to open them."
            )
        if value["session_file"] != expected_file:
            raise DirectorSessionError(
                "Director session filename metadata does not match the file."
            )
        revision = value["session_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise DirectorSessionError("Director session_revision must be a positive integer.")
        _bounded_required_text(value["saved_at"], "saved_at", 128)
        raw_plan = value["plan"]
        if not isinstance(raw_plan, dict):
            raise DirectorSessionError("Director session plan must be an object.")
        try:
            value["plan"] = validate_director_plan(raw_plan)
            value["ledger"] = validate_ledger_for_plan(value["plan"], value["ledger"])
            canonical_json(value)
        except DirectorStateError as exc:
            raise DirectorSessionError(f"Director session state is invalid: {exc}") from exc
        return cast(dict[str, Any], value)

    def _atomic_replace(self, target: Path, data: bytes) -> None:
        temporary: Path | None = None
        try:
            self._assert_root_boundary()
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target.stem}.",
                suffix=".tmp",
                dir=str(self.root),
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Recheck immediately before replace while the root lock is held.
            self._assert_root_boundary()
            if target.is_symlink():
                raise DirectorSessionError("Director session symlinks are not allowed.")
            os.replace(temporary, target)
            temporary = None
        except DirectorSessionError:
            raise
        except OSError as exc:
            raise DirectorSessionError(
                "Director session could not be saved atomically."
            ) from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _normalize_session_filename(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DirectorSessionError("Director session name must be a non-empty string.")
    name = value.strip()
    if (
        "\x00" in name
        or "/" in name
        or "\\" in name
        or any(character in name for character in '<>:"|?*')
        or name in {".", ".."}
    ):
        raise DirectorSessionError("Director session name must be one filename, not a path.")
    if not name.lower().endswith(".json"):
        name += ".json"
    if len(name) > MAX_DIRECTOR_SESSION_NAME_CHARS:
        raise DirectorSessionError(
            f"Director session filename exceeds {MAX_DIRECTOR_SESSION_NAME_CHARS} characters."
        )
    if Path(name).name != name:
        raise DirectorSessionError("Director session name must be one filename, not a path.")
    stem = Path(name).stem.rstrip(" .")
    if not stem or stem.upper() in _WINDOWS_RESERVED_NAMES:
        raise DirectorSessionError("Director session filename is reserved or invalid.")
    if name[-1] in {" ", "."}:
        raise DirectorSessionError("Director session filename cannot end with a space or dot.")
    return name


def _lock_for_root(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root))
    with _LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_LOCKS[key] = lock
        return lock


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(value)))
    except (TypeError, ValueError, OSError) as exc:
        raise DirectorSessionError("Director session path is invalid.") from exc


def _is_within(candidate: Path, base: Path) -> bool:
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return True


def _prepare_bounded_root(raw_base: Path, raw_root: Path) -> tuple[Path, Path]:
    """Create ``raw_root`` only after checking existing ancestors against base."""

    try:
        common = Path(os.path.commonpath((str(raw_base), str(raw_root))))
    except (OSError, ValueError) as exc:
        raise DirectorSessionError(
            "Director session root must be inside its trusted base."
        ) from exc
    if os.path.normcase(str(common)) != os.path.normcase(str(raw_base)):
        raise DirectorSessionError("Director session root must be inside its trusted base.")

    try:
        resolved_base = raw_base.resolve(strict=True)
    except OSError as exc:
        raise DirectorSessionError("Director trusted base is unavailable.") from exc
    if not resolved_base.is_dir():
        raise DirectorSessionError("Director trusted base must be a directory.")

    relative = Path(os.path.relpath(raw_root, raw_base))
    current = raw_base
    for part in () if str(relative) == "." else relative.parts:
        current = current / part
        if not (current.exists() or current.is_symlink()):
            break
        try:
            resolved_ancestor = current.resolve(strict=True)
        except OSError as exc:
            raise DirectorSessionError(
                "Director session root contains an unavailable redirected ancestor."
            ) from exc
        if not _is_within(resolved_ancestor, resolved_base):
            raise DirectorSessionError(
                "Director session root escapes its trusted base through a symlink or junction."
            )
        if not resolved_ancestor.is_dir():
            raise DirectorSessionError(
                "Director session root contains a non-directory ancestor."
            )

    try:
        raw_root.mkdir(parents=True, exist_ok=True)
        resolved_root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise DirectorSessionError("Director session directory is unavailable.") from exc
    if not resolved_root.is_dir():
        raise DirectorSessionError("Director session root must be a directory.")
    if not _is_within(resolved_root, resolved_base):
        raise DirectorSessionError("Director session root escaped its trusted base.")
    return resolved_base, resolved_root


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        details = path.stat()
    except OSError as exc:
        raise DirectorSessionError(
            "Director session directory identity is unavailable."
        ) from exc
    if not stat.S_ISDIR(details.st_mode):
        raise DirectorSessionError("Director session boundary must be a directory.")
    return details.st_dev, details.st_ino


def _normalize_lock_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DirectorSessionError("Director session lock timeout must be a number.")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized <= 0
        or normalized > MAX_DIRECTOR_SESSION_LOCK_TIMEOUT_SECONDS
    ):
        raise DirectorSessionError(
            "Director session lock timeout must be greater than 0 and no more than "
            f"{MAX_DIRECTOR_SESSION_LOCK_TIMEOUT_SECONDS:g} seconds."
        )
    return normalized


def _open_lock_file(path: Path, root: Path) -> int:
    """Open a persistent regular lock file without following POSIX symlinks."""

    if path.is_symlink():
        raise DirectorSessionError("Director session process lock cannot be a symlink.")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise DirectorSessionError("Director session process lock must be a regular file.")
        resolved = path.resolve(strict=True)
        if resolved.parent != root:
            raise DirectorSessionError(
                "Director session process lock escaped the session root."
            )
        # Windows byte-range locks need a byte to lock.  Extending an empty,
        # validated in-root lock file is harmless and is done without truncation.
        if details.st_size < 1:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except DirectorSessionError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise DirectorSessionError(
            "Director session process lock file is unavailable."
        ) from exc


def _try_lock_descriptor(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


def _unlock_descriptor(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        # Closing the descriptor below also releases the OS lock.  A release
        # error must not hide the save result or the original exception.
        pass


def _bounded_required_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DirectorSessionError(f"Director session {name} must be a non-empty string.")
    clean = value.strip()
    if len(clean) > maximum:
        raise DirectorSessionError(f"Director session {name} exceeds {maximum} characters.")
    return clean


__all__ = [
    "DEFAULT_DIRECTOR_SESSION_LOCK_TIMEOUT_SECONDS",
    "MAX_DIRECTOR_SESSION_LOCK_TIMEOUT_SECONDS",
    "MAX_DIRECTOR_SESSION_BYTES",
    "MAX_DIRECTOR_SESSION_NAME_CHARS",
    "DirectorSessionConflictError",
    "DirectorSessionError",
    "DirectorSessionStore",
]
