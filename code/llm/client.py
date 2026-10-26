"""Prompt-agnostic Gemini API transport."""

from __future__ import annotations

import json
from typing import Any

try:
    import requests
except ImportError:
    requests = None

from code.llm.config import DEFAULT_MODEL, get_api_key, resolve_model_name
from code.llm.usage import USAGE_TRACKER


API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _generate_content(
    *,
    parts: list[dict[str, Any]],
    system_prompt: str | None,
    model_name: str | None,
    api_key: str | None,
    temperature: float,
    timeout: int,
    json_response: bool = False,
    object_response: bool = False,
) -> Any:
    key = get_api_key(api_key)
    effective_model = resolve_model_name(model_name)
    if not key:
        USAGE_TRACKER.record_failure(effective_model, error_type="credentials_unavailable")
        return None
    if requests is None:
        USAGE_TRACKER.record_failure(effective_model, error_type="requests_unavailable")
        return None

    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature},
    }
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    if json_response:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    response_recorded = False
    try:
        response = requests.post(
            f"{API_URL.format(model=effective_model)}?key={key}",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if response.status_code != 200:
            USAGE_TRACKER.record_failure(
                effective_model,
                error_type=f"http_status_{response.status_code}",
            )
            return None

        data = response.json()
        usage = data.get("usageMetadata", {})
        USAGE_TRACKER.record_usage(
            effective_model,
            usage.get("promptTokenCount", 0),
            usage.get("candidatesTokenCount", 0),
        )
        response_recorded = True

        candidates = data.get("candidates", [])
        if not candidates:
            USAGE_TRACKER.record_failure(
                effective_model,
                count_call=False,
                error_type="empty_candidates",
            )
            return None

        content_parts = candidates[0].get("content", {}).get("parts", [])
        response_text = next((part.get("text", "") for part in content_parts if "text" in part), "")
        if not json_response:
            return response_text

        result = json.loads(response_text)
        if object_response and not isinstance(result, dict):
            raise ValueError("Gemini returned JSON that was not an object.")
        return result
    except Exception as exc:
        USAGE_TRACKER.record_failure(
            effective_model,
            count_call=not response_recorded,
            error_type=type(exc).__name__,
        )
        return None


def call_gemini_json(
    prompt_text: str,
    system_prompt: str,
    model_name: str | None = DEFAULT_MODEL,
    api_key: str | None = None,
    temperature: float = 0.0,
    timeout: int = 15,
) -> dict[str, Any] | None:
    """Send a feature-provided prompt and parse a JSON object response."""
    result = _generate_content(
        parts=[{"text": prompt_text}],
        system_prompt=system_prompt,
        model_name=model_name,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
        json_response=True,
        object_response=True,
    )
    if isinstance(result, dict):
        result["source"] = f"gemini:{resolve_model_name(model_name)}"
        return result
    return None


def call_gemini_text(
    prompt_text: str,
    system_prompt: str,
    model_name: str | None = DEFAULT_MODEL,
    api_key: str | None = None,
    temperature: float = 0.0,
    timeout: int = 15,
) -> str | None:
    """Send a feature-provided prompt and return the model's text response."""
    result = _generate_content(
        parts=[{"text": prompt_text}],
        system_prompt=system_prompt,
        model_name=model_name,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
    )
    return result if isinstance(result, str) else None


