"""Preview or explicitly submit an external Director queue manifest.

The default mode is read-only and prints a redacted preview. No request is
sent unless ``--run`` and ``--confirm`` are both present. Execution also
requires at least one exact ``--selector`` from the manifest allowlist.

Examples::

    python tools/director_queue.py director-queue.json
    python tools/director_queue.py director-queue.json --selector scene-01/shot-01/keyframe
    python tools/director_queue.py director-queue.json \
        --selector scene-01/shot-01/keyframe --run --confirm
    python tools/director_queue.py director-queue.json \
        --selector scene-01/shot-01/keyframe --run --confirm --wait
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT))

from universal_skills.director_controller import (  # noqa: E402
    DirectorQueueItem,
    DirectorQueueManifestError,
    build_prompt_payload,
    build_queue_preview,
    load_queue_manifest,
    select_queue_items,
)


DEFAULT_COMFY_BASE_URL = "http://127.0.0.1:8188"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_WAIT_TIMEOUT_SECONDS = 600.0
MIN_POLL_INTERVAL_SECONDS = 0.1
MAX_POLL_INTERVAL_SECONDS = 60.0
MIN_WAIT_TIMEOUT_SECONDS = 1.0
MAX_WAIT_TIMEOUT_SECONDS = 3600.0
MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024


class DirectorQueueTransportError(RuntimeError):
    """Raised when an explicit external queue submission fails safely."""


def _reject_non_json_constant(_value: str) -> None:
    raise ValueError


def normalize_comfy_base_url(value: str, *, allow_remote: bool = False) -> str:
    """Validate a CLI-owned ComfyUI base URL and return it without a slash."""

    if not isinstance(value, str) or not value.strip():
        raise DirectorQueueTransportError("ComfyUI base URL must not be empty.")
    parsed = urlparse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"}:
        raise DirectorQueueTransportError("ComfyUI base URL must use http or https.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise DirectorQueueTransportError(
            "ComfyUI base URL must contain a host and must not embed credentials."
        )
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise DirectorQueueTransportError(
            "ComfyUI base URL cannot contain a path, query, or fragment."
        )

    if not allow_remote and not _is_loopback_host(parsed.hostname):
        raise DirectorQueueTransportError(
            "Remote ComfyUI hosts require the explicit --allow-remote flag."
        )

    try:
        port = parsed.port
    except ValueError:
        raise DirectorQueueTransportError("ComfyUI base URL contains an invalid port.") from None
    host = parsed.hostname
    display_host = f"[{host}]" if ":" in host else host
    authority = f"{display_host}:{port}" if port is not None else display_host
    return f"{parsed.scheme}://{authority}"


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_timeout(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise DirectorQueueTransportError("HTTP timeout must be a number.")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0 or normalized > 300:
        raise DirectorQueueTransportError(
            "HTTP timeout must be greater than 0 and at most 300 seconds."
        )
    return normalized


def _validated_poll_interval(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise DirectorQueueTransportError("Poll interval must be a number.")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized < MIN_POLL_INTERVAL_SECONDS
        or normalized > MAX_POLL_INTERVAL_SECONDS
    ):
        raise DirectorQueueTransportError(
            f"Poll interval must be between {MIN_POLL_INTERVAL_SECONDS} and "
            f"{MAX_POLL_INTERVAL_SECONDS} seconds."
        )
    return normalized


def _validated_wait_timeout(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise DirectorQueueTransportError("Wait timeout must be a number.")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized < MIN_WAIT_TIMEOUT_SECONDS
        or normalized > MAX_WAIT_TIMEOUT_SECONDS
    ):
        raise DirectorQueueTransportError(
            f"Wait timeout must be between {MIN_WAIT_TIMEOUT_SECONDS} and "
            f"{MAX_WAIT_TIMEOUT_SECONDS} seconds."
        )
    return normalized


def _clock_value(clock: Callable[[], float]) -> float:
    try:
        value = float(clock())
    except (TypeError, ValueError, OverflowError):
        raise DirectorQueueTransportError("Polling clock returned an invalid value.") from None
    if not math.isfinite(value):
        raise DirectorQueueTransportError("Polling clock returned an invalid value.")
    return value


def _safe_response_identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 240
        or not value.isascii()
        or not value[0].isalnum()
        or any(
            not (character.isalnum() or character in "._:-")
            for character in value
        )
    ):
        raise DirectorQueueTransportError(
            f"ComfyUI response did not contain a safe {label}."
        )
    return value


def submit_prompt(
    base_url: str,
    item: DirectorQueueItem,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Submit one validated item; callers must enforce confirmation policy."""

    normalized_url = normalize_comfy_base_url(base_url, allow_remote=True)
    normalized_timeout = _validated_timeout(timeout)
    payload = build_prompt_payload(item)
    body = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urlrequest.Request(
        f"{normalized_url}/prompt",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    open_request = opener or urlrequest.urlopen
    try:
        response = open_request(request, timeout=normalized_timeout)
        with response:
            raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    except urlerror.HTTPError as error:
        raise DirectorQueueTransportError(
            f"ComfyUI rejected the queue request with HTTP {error.code}."
        ) from None
    except (urlerror.URLError, TimeoutError, OSError):
        raise DirectorQueueTransportError(
            "Could not connect to ComfyUI or the queue request timed out."
        ) from None

    if len(raw) > MAX_HTTP_RESPONSE_BYTES:
        raise DirectorQueueTransportError("ComfyUI response exceeded the safe size limit.")
    try:
        decoded = raw.decode("utf-8")
        result = json.loads(
            decoded,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise DirectorQueueTransportError(
            "ComfyUI returned an invalid JSON response."
        ) from None
    if not isinstance(result, dict):
        raise DirectorQueueTransportError("ComfyUI response must be a JSON object.")

    prompt_id = _safe_response_identifier(result.get("prompt_id"), "prompt_id")
    number = result.get("number")
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float, type(None)))
        or (isinstance(number, float) and not math.isfinite(number))
    ):
        number = None
    return {
        "selector": item.selector,
        "work_item_id": item.work_item_id,
        "status": "queued",
        "prompt_id": prompt_id,
        "number": number,
    }


def _history_outcome(value: Any, prompt_id: str) -> tuple[str, str] | None:
    """Reduce a full history document to a redacted terminal outcome."""

    if not isinstance(value, Mapping):
        return ("error", "history_invalid_response")
    entry = value.get(prompt_id)
    if entry is None:
        return None
    if not isinstance(entry, Mapping):
        return ("error", "history_invalid_response")

    status = entry.get("status")
    status_text = ""
    completed = False
    if isinstance(status, Mapping):
        raw_status = status.get("status_str")
        if isinstance(raw_status, str):
            normalized = raw_status.strip().casefold()
            if normalized in {"success", "completed", "complete"}:
                status_text = "completed"
            elif normalized in {"error", "failed", "failure", "cancelled", "canceled"}:
                status_text = "error"
        completed = status.get("completed") is True

    if status_text == "error":
        return ("error", "execution_failed")
    if completed or status_text == "completed" or isinstance(entry.get("outputs"), Mapping):
        return ("completed", "")
    return None


def wait_for_prompt_history(
    base_url: str,
    prompt_id: str,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Boundedly poll one ComfyUI history entry and return a redacted receipt."""

    normalized_url = normalize_comfy_base_url(base_url, allow_remote=True)
    safe_prompt_id = _safe_response_identifier(prompt_id, "prompt_id")
    interval = _validated_poll_interval(poll_interval)
    timeout = _validated_wait_timeout(wait_timeout)
    open_request = opener or urlrequest.urlopen
    deadline = _clock_value(clock) + timeout
    max_polls = int(math.ceil(timeout / interval)) + 2
    history_url = (
        f"{normalized_url}/history/"
        f"{urlparse.quote(safe_prompt_id, safe='')}"
    )

    for _poll_number in range(max_polls):
        remaining = deadline - _clock_value(clock)
        if remaining <= 0:
            return {"status": "timeout", "prompt_id": safe_prompt_id}
        request = urlrequest.Request(
            history_url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            response = open_request(
                request,
                timeout=max(0.001, min(DEFAULT_TIMEOUT_SECONDS, remaining)),
            )
            with response:
                raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
        except urlerror.HTTPError:
            return {
                "status": "error",
                "prompt_id": safe_prompt_id,
                "error_code": "history_http_error",
            }
        except (urlerror.URLError, TimeoutError, OSError):
            return {
                "status": "error",
                "prompt_id": safe_prompt_id,
                "error_code": "history_connection_error",
            }

        if len(raw) > MAX_HTTP_RESPONSE_BYTES:
            return {
                "status": "error",
                "prompt_id": safe_prompt_id,
                "error_code": "history_response_too_large",
            }
        try:
            history = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_non_json_constant,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
        ):
            return {
                "status": "error",
                "prompt_id": safe_prompt_id,
                "error_code": "history_invalid_response",
            }

        outcome = _history_outcome(history, safe_prompt_id)
        if outcome is not None:
            status, error_code = outcome
            receipt = {"status": status, "prompt_id": safe_prompt_id}
            if error_code:
                receipt["error_code"] = error_code
            return receipt

        remaining = deadline - _clock_value(clock)
        if remaining <= 0:
            return {"status": "timeout", "prompt_id": safe_prompt_id}
        try:
            sleeper(min(interval, remaining))
        except Exception:
            return {
                "status": "error",
                "prompt_id": safe_prompt_id,
                "error_code": "poll_sleep_error",
            }

    return {"status": "timeout", "prompt_id": safe_prompt_id}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Path to a Director queue JSON file.")
    parser.add_argument(
        "--selector",
        action="append",
        default=[],
        help="Exact selector from allowed_selectors; repeat to choose multiple items.",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_COMFY_BASE_URL,
        help=f"ComfyUI base URL (default: {DEFAULT_COMFY_BASE_URL}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout in seconds (greater than 0, maximum 300).",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="After confirmed submission, poll each selected prompt's history.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=(
            f"History poll interval in seconds ({MIN_POLL_INTERVAL_SECONDS} to "
            f"{MAX_POLL_INTERVAL_SECONDS})."
        ),
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help=(
            f"Maximum wait per prompt in seconds ({MIN_WAIT_TIMEOUT_SECONDS} to "
            f"{MAX_WAIT_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Enable HTTP submission; --confirm must be supplied at the same time.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Acknowledge that selected prompts may consume GPU time or paid credits.",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Permit a non-loopback ComfyUI base URL.",
    )
    return parser


def run_cli(
    argv: list[str] | None = None,
    *,
    submitter: Callable[..., dict[str, Any]] = submit_prompt,
    waiter: Callable[..., dict[str, Any]] = wait_for_prompt_history,
    sleeper: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the CLI with injectable I/O and time functions for offline tests."""

    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.run != args.confirm:
        parser.error("--run and --confirm must be supplied together.")
    if args.run and not args.selector:
        parser.error("Execution requires at least one explicit --selector.")
    if args.wait and not args.run:
        parser.error("--wait requires --run and --confirm.")

    try:
        base_url = normalize_comfy_base_url(
            args.base_url,
            allow_remote=args.allow_remote,
        )
        timeout = _validated_timeout(args.timeout)
        poll_interval = _validated_poll_interval(args.poll_interval)
        wait_timeout = _validated_wait_timeout(args.wait_timeout)
        manifest = load_queue_manifest(args.manifest)
        selected = select_queue_items(manifest, args.selector)
        preview = build_queue_preview(manifest, args.selector)
    except (DirectorQueueManifestError, DirectorQueueTransportError) as error:
        print(f"Director queue error: {error}", file=sys.stderr)
        return 1

    print(json.dumps(preview, ensure_ascii=False, indent=2))
    if not args.run:
        print(
            "Preview only. No HTTP request was sent. Use explicit --selector, --run, and --confirm to submit.",
            file=sys.stderr,
        )
        return 0

    receipts: list[dict[str, Any]] = []
    try:
        for item in selected:
            receipts.append(
                submitter(
                    base_url,
                    item,
                    timeout=timeout,
                )
            )
    except DirectorQueueTransportError as error:
        if receipts:
            print(
                json.dumps(
                    {"mode": "partial", "receipts": receipts},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        print(f"Director queue error: {error}", file=sys.stderr)
        return 1

    if args.wait:
        completed_receipts: list[dict[str, Any]] = []
        for receipt in receipts:
            try:
                history_receipt = waiter(
                    base_url,
                    receipt["prompt_id"],
                    poll_interval=poll_interval,
                    wait_timeout=wait_timeout,
                    sleeper=sleeper,
                    clock=clock,
                )
            except DirectorQueueTransportError:
                history_receipt = {
                    "status": "error",
                    "prompt_id": receipt["prompt_id"],
                    "error_code": "history_controller_error",
                }
            if not isinstance(history_receipt, Mapping):
                history_receipt = {
                    "status": "error",
                    "prompt_id": receipt["prompt_id"],
                    "error_code": "history_controller_error",
                }
            status = history_receipt.get("status")
            if status not in {"completed", "timeout", "error"}:
                status = "error"
                history_receipt = {
                    "status": status,
                    "prompt_id": receipt["prompt_id"],
                    "error_code": "history_controller_error",
                }
            merged = {
                "selector": receipt["selector"],
                "work_item_id": receipt["work_item_id"],
                "status": status,
                "prompt_id": receipt["prompt_id"],
                "number": receipt.get("number"),
            }
            error_code = history_receipt.get("error_code")
            if error_code in {
                "execution_failed",
                "history_http_error",
                "history_connection_error",
                "history_response_too_large",
                "history_invalid_response",
                "poll_sleep_error",
                "history_controller_error",
            }:
                merged["error_code"] = error_code
            completed_receipts.append(merged)
        receipts = completed_receipts

    mode = "run_wait" if args.wait else "run"
    print(json.dumps({"mode": mode, "receipts": receipts}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    sys.exit(main())
