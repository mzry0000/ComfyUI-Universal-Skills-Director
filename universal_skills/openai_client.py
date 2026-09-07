"""Lazy OpenAI Responses client with safe domain-level failures."""

from __future__ import annotations

import importlib
import math
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from . import local_config
from .config import API_KEY_ENV, DEFAULT_TIMEOUT_SECONDS, TIMEOUT_ENV
from .errors import (
    MissingApiKeyError,
    OpenAIAPIError,
    OpenAIAuthenticationError,
    OpenAIBadRequestError,
    OpenAIClientConfigurationError,
    OpenAIConnectionError,
    OpenAIPermissionError,
    OpenAIRateLimitError,
    OpenAIResponseFormatError,
    OpenAITimeoutError,
    UniversalSkillHostError,
)


_MISSING = object()

# Only echo API enum-like tokens (status, reason, error code) into public
# errors; anything else could carry request or prompt content.
_SAFE_ENUM_TOKEN = re.compile(r"^[a-z0-9_.-]{1,64}$")

# Error code/param values such as "model_not_found" or "input[0].content" —
# still enum-like, never free text.
_SAFE_DETAIL_TOKEN = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,120}$")


def _safe_enum_token(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_ENUM_TOKEN.fullmatch(value):
        return value
    return None


def _safe_error_details(error: Exception) -> str:
    """Render sanitized SDK error identifiers, e.g. " (code: model_not_found)"."""

    parts: list[str] = []
    for name in ("code", "param"):
        value = getattr(error, name, None)
        if isinstance(value, str) and _SAFE_DETAIL_TOKEN.fullmatch(value):
            parts.append(f"{name}: {value}")
    return f" ({', '.join(parts)})" if parts else ""


def _member(value: Any, name: str, default: Any = None) -> Any:
    """Read an SDK model or mapping field without depending on SDK classes."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _items(value: Any) -> Sequence[Any]:
    """Return an SDK list-like field while excluding strings and mappings."""

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _contains_refusal(response: Any) -> bool:
    output = _member(response, "output", ())
    for item in _items(output):
        if _member(item, "type") == "refusal":
            return True
        for content in _items(_member(item, "content", ())):
            if _member(content, "type") == "refusal":
                return True
    return False


def extract_completed_output_text(response: Any) -> str:
    """Return text from a completed, non-refusal Responses API result.

    JSON decoding and schema validation deliberately remain in the planner layer.
    This helper only enforces the transport-level response contract.
    """

    status = _member(response, "status", _MISSING)
    if status != "completed":
        status_name = _safe_enum_token(status) or "unknown"
        reason = _safe_enum_token(
            _member(_member(response, "incomplete_details") or (), "reason")
        )
        error_code = _safe_enum_token(_member(_member(response, "error") or (), "code"))
        if status_name == "incomplete" and reason == "max_output_tokens":
            raise OpenAIResponseFormatError(
                "OpenAI stopped at the output token limit before completing the "
                "plan (status: incomplete, reason: max_output_tokens). Reduce the "
                "request size or increase max_output_tokens. Reasoning tokens "
                "also consume this budget."
            )
        details = [f"status: {status_name}"]
        if reason:
            details.append(f"reason: {reason}")
        if error_code:
            details.append(f"error: {error_code}")
        raise OpenAIResponseFormatError(
            "OpenAI did not return a completed response ("
            + ", ".join(details)
            + "). Retry the planner request."
        )

    if _contains_refusal(response):
        raise OpenAIResponseFormatError(
            "OpenAI refused the planner request. Revise the request or input images."
        )

    output_text = _member(response, "output_text", _MISSING)
    if not isinstance(output_text, str) or not output_text.strip():
        raise OpenAIResponseFormatError(
            "OpenAI returned no usable output text. Retry the planner request."
        )
    return output_text


def _sdk_exception_type(sdk_module: Any, name: str) -> type[BaseException] | None:
    candidate = getattr(sdk_module, name, None) if sdk_module is not None else None
    if isinstance(candidate, type) and issubclass(candidate, BaseException):
        return candidate
    return None


def _matches_sdk_exception(error: Exception, sdk_module: Any, *names: str) -> bool:
    classes = tuple(
        candidate
        for name in names
        if (candidate := _sdk_exception_type(sdk_module, name)) is not None
    )
    return bool(classes) and isinstance(error, classes)


def _translated_error(error: Exception, sdk_module: Any) -> UniversalSkillHostError:
    """Map SDK exceptions without copying potentially sensitive SDK messages."""

    # APITimeoutError is an APIConnectionError subclass in the official SDK.
    if _matches_sdk_exception(error, sdk_module, "APITimeoutError"):
        return OpenAITimeoutError(
            "The OpenAI request timed out. Retry or increase the configured timeout."
        )
    if _matches_sdk_exception(error, sdk_module, "APIConnectionError"):
        return OpenAIConnectionError(
            "Could not connect to OpenAI. Check the network and retry."
        )
    if _matches_sdk_exception(error, sdk_module, "AuthenticationError"):
        return OpenAIAuthenticationError(f"OpenAI authentication failed. Check {API_KEY_ENV}.")
    if _matches_sdk_exception(error, sdk_module, "PermissionDeniedError"):
        return OpenAIPermissionError(
            "The OpenAI project lacks permission for the requested model or input."
            + _safe_error_details(error)
        )
    if _matches_sdk_exception(error, sdk_module, "RateLimitError"):
        return OpenAIRateLimitError(
            "OpenAI rate limits or quota prevented the request. Retry later or check quota."
        )
    if _matches_sdk_exception(
        error,
        sdk_module,
        "BadRequestError",
        "NotFoundError",
        "UnprocessableEntityError",
    ):
        return OpenAIBadRequestError(
            "OpenAI rejected the request. Check the model and inputs."
            + _safe_error_details(error)
        )

    if _matches_sdk_exception(error, sdk_module, "APIStatusError"):
        status_code = getattr(error, "status_code", None)
        if status_code == 401:
            return OpenAIAuthenticationError(
                f"OpenAI authentication failed. Check {API_KEY_ENV}."
            )
        if status_code == 403:
            return OpenAIPermissionError(
                "The OpenAI project lacks permission for the requested model or input."
            )
        if status_code == 429:
            return OpenAIRateLimitError(
                "OpenAI rate limits or quota prevented the request. Retry later or check quota."
            )
        if status_code in (400, 404, 422):
            return OpenAIBadRequestError(
                "OpenAI rejected the request. Check the model and inputs."
                + _safe_error_details(error)
            )

    return OpenAIAPIError(
        "The OpenAI request failed. Retry and check the configured model and project access."
    )


def _resolve_default_timeout_seconds() -> float:
    """Resolve the timeout: env var, then ush_config.json, then the default."""

    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if not math.isfinite(value) or value <= 0:
            raise OpenAIClientConfigurationError(
                f"{TIMEOUT_ENV} must be a positive number of seconds."
            )
        return value

    file_timeout = local_config.load_local_config().timeout_seconds
    if file_timeout is not None:
        return file_timeout
    return DEFAULT_TIMEOUT_SECONDS


def _resolve_api_key() -> str:
    """Resolve the API key: env var first, then ush_config.json."""

    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if api_key:
        return api_key
    file_key = local_config.load_local_config().api_key
    if file_key:
        return file_key
    raise MissingApiKeyError(
        f"Set {API_KEY_ENV} in the ComfyUI process environment, or put "
        f'"openai_api_key" into {local_config.CONFIG_FILENAME} next to this '
        "node package, then restart ComfyUI."
    )


def _import_openai_sdk() -> Any:
    try:
        return importlib.import_module("openai")
    except (ImportError, ModuleNotFoundError):
        raise OpenAIClientConfigurationError(
            "The OpenAI Python SDK is not installed. Install this node's requirements."
        ) from None


def _responses_create(client: Any) -> Any:
    responses = getattr(client, "responses", None)
    create = getattr(responses, "create", None)
    if not callable(create):
        raise OpenAIClientConfigurationError(
            "The installed OpenAI SDK does not provide client.responses.create. "
            "Install a current OpenAI Python SDK."
        )
    return create


class OpenAIResponsesClient:
    """Thin synchronous wrapper around ``client.responses.create``.

    Supplying ``client`` and ``sdk_module`` keeps unit tests fully offline. When
    no client is supplied, SDK import, environment lookup, and client creation
    are deferred until the first request.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        sdk_module: Any | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise OpenAIClientConfigurationError(
                "The OpenAI timeout must be a positive number of seconds."
            )
        self._client = client
        self._sdk_module = sdk_module
        # None defers to USH_TIMEOUT_SECONDS or the package default at the
        # moment the default client is created.
        self._timeout_seconds = None if timeout_seconds is None else float(timeout_seconds)

    def _default_client(self) -> Any:
        api_key = _resolve_api_key()

        try:
            sdk_module = self._sdk_module or _import_openai_sdk()
            client_type = getattr(sdk_module, "OpenAI", None)
            if not callable(client_type):
                raise OpenAIClientConfigurationError(
                    "The installed OpenAI SDK does not provide openai.OpenAI. "
                    "Install a current OpenAI Python SDK."
                )

            timeout = (
                self._timeout_seconds
                if self._timeout_seconds is not None
                else _resolve_default_timeout_seconds()
            )
            try:
                client = client_type(
                    api_key=api_key,
                    timeout=timeout,
                )
            except Exception as error:
                translated = _translated_error(error, sdk_module)
            else:
                self._sdk_module = sdk_module
                self._client = client
                return client
        finally:
            # Do not retain the credential in a traceback frame if setup fails.
            api_key = ""

        # Raise outside the handler so the original SDK exception, which may
        # contain request data, is not retained as context on the public error.
        raise translated

    def _get_client(self) -> Any:
        if self._client is None:
            return self._default_client()
        return self._client

    def _sdk_for_error_mapping(self) -> Any | None:
        if self._sdk_module is not None:
            return self._sdk_module
        try:
            self._sdk_module = _import_openai_sdk()
        except OpenAIClientConfigurationError:
            return None
        return self._sdk_module

    def create_response(self, payload: Mapping[str, Any]) -> Any:
        """Call ``client.responses.create(**payload)`` and translate failures."""

        if not isinstance(payload, Mapping):
            raise TypeError("OpenAI response payload must be a mapping.")

        request_payload = dict(payload)
        del payload
        try:
            client = self._get_client()
            create = _responses_create(client)
            try:
                response = create(**request_payload)
            except Exception as error:
                translated = _translated_error(error, self._sdk_for_error_mapping())
            else:
                return response
        finally:
            # The copy can contain prompts and Base64 images; never retain it
            # in the traceback frame of a translated public exception.
            request_payload.clear()

        # Deliberately outside the exception handler; see _default_client().
        raise translated

    def create_text_response(self, payload: Mapping[str, Any]) -> str:
        """Create a response and return checked aggregate output text."""

        return extract_completed_output_text(self.create_response(payload))


__all__ = ["OpenAIResponsesClient", "extract_completed_output_text"]
