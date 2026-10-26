"""Environment-backed settings for the shared model transport."""

from __future__ import annotations

import os


DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_INTENT_MAX_RETRIES = 2
MAX_INTENT_RETRIES = 5
DEFAULT_DOCUMENT_TEXT_TIMEOUT_SECONDS = 60
DEFAULT_DOCUMENT_MAX_RETRIES = 2


def get_api_key(explicit_key: str | None = None) -> str | None:
    """Read credentials from an explicit argument or the process environment."""
    return explicit_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def resolve_model_name(model_name: str | None = None) -> str:
    """Resolve the shared text model override, then the application default."""
    return os.environ.get("GEMINI_MODEL") or model_name or DEFAULT_MODEL


def get_document_text_timeout_seconds() -> int:
    """Resolve the OCR-text extraction request timeout from the environment."""
    try:
        configured = int(os.environ.get("GEMINI_DOCUMENT_TEXT_TIMEOUT_SECONDS", ""))
    except ValueError:
        return DEFAULT_DOCUMENT_TEXT_TIMEOUT_SECONDS
    return max(1, configured)


def get_intent_max_retries() -> int:
    """Resolve bounded additional retries for intent classification."""
    try:
        configured = int(os.environ.get("GEMINI_INTENT_MAX_RETRIES", ""))
    except ValueError:
        return DEFAULT_INTENT_MAX_RETRIES
    return min(MAX_INTENT_RETRIES, max(0, configured))


def get_document_max_retries() -> int:
    """Resolve bounded retries for OCR-text interpretation calls."""
    try:
        configured = int(os.environ.get("GEMINI_DOCUMENT_MAX_RETRIES", ""))
    except ValueError:
        return DEFAULT_DOCUMENT_MAX_RETRIES
    return min(MAX_INTENT_RETRIES, max(0, configured))
