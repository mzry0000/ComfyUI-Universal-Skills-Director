"""Domain errors with concise, actionable messages for ComfyUI users."""

from __future__ import annotations


class UniversalSkillHostError(RuntimeError):
    """Base class for expected, user-correctable host failures."""


class MissingApiKeyError(UniversalSkillHostError):
    """Raised when no OpenAI API key is configured via env or config file."""


class LocalConfigError(UniversalSkillHostError):
    """Raised when ush_config.json exists but cannot be used safely."""


class OpenAIClientConfigurationError(UniversalSkillHostError):
    """Raised when the installed OpenAI SDK lacks the required API surface."""


class InvalidSpecificationError(UniversalSkillHostError):
    """Raised when a local Markdown or JSON specification is unsafe or invalid."""


class UnsupportedTargetProfileError(UniversalSkillHostError):
    """Raised when no V1 target adapter exists for the requested profile."""


class ImageEncodingError(UniversalSkillHostError):
    """Raised when a ComfyUI IMAGE cannot be encoded safely."""


class OpenAIAuthenticationError(UniversalSkillHostError):
    """Raised for invalid, expired, or revoked API credentials."""


class OpenAIPermissionError(UniversalSkillHostError):
    """Raised when the API project lacks model or API capability permission."""


class OpenAIRateLimitError(UniversalSkillHostError):
    """Raised after the SDK cannot recover from a rate limit."""


class OpenAITimeoutError(UniversalSkillHostError):
    """Raised when the Responses request times out."""


class OpenAIConnectionError(UniversalSkillHostError):
    """Raised when the SDK cannot connect to the OpenAI API."""


class OpenAIBadRequestError(UniversalSkillHostError):
    """Raised when the API rejects the request payload."""


class OpenAIAPIError(UniversalSkillHostError):
    """Raised for other OpenAI API failures."""


class OpenAIResponseFormatError(UniversalSkillHostError):
    """Raised when a completed response cannot be parsed as a valid plan."""
