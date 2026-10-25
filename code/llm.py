"""
LLM Client and Central Agent Brain Module.

Centralizes API calls to Google Gemini (gemini-3.8-flash), system prompts,
JSON formatting configuration, and token/call usage telemetry for auditing reports.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None


# ---------------------------------------------------------------------------
# Central System Prompts
# ---------------------------------------------------------------------------

INTENT_SYSTEM_PROMPT = """You are an expert privacy compliance auditor evaluating data requests under GDPR, CCPA, and global privacy laws.
Your task is to classify the intent of a customer message submitted to an online platform.

The text may be written in various languages (English, Spanish, French, German, Japanese, Portuguese, etc.).

Analyze whether the customer is requesting:
1. "DELETION": Explicitly requesting account closure, permanent cancellation, personal data deletion, account erasure, or removal of profile data.
2. "DATA_EXPORT": Requesting a copy, download, or portable export of their personal data (Subject Access / Portability) while keeping the account active.
3. "ACCESS_REQUEST": Requesting copies of past invoices, order history, receipts, or inspection of records without deleting the account.
4. "PROFILE_UPDATE": Requesting correction or updating of contact details (e.g. phone number, email address) while explicitly leaving the account active.
5. "MARKETING_OPT_OUT": Requesting to unsubscribe from marketing emails or communications without deleting their account.
6. "OTHER": Inquiries, bug reports, or messages that do not authorize deletion.

CRITICAL POLICY RULE:
If the customer asks to "leave the account active", "keep the account", "keep my profile", or is merely asking for an export, update, or invoice copy, it is NOT an authorized deletion request.

Respond ONLY with a valid JSON object matching this schema:
{
  "is_deletion_request": true or false,
  "intent_category": "DELETION" | "DATA_EXPORT" | "ACCESS_REQUEST" | "PROFILE_UPDATE" | "MARKETING_OPT_OUT" | "OTHER",
  "confidence": float between 0.0 and 1.0,
  "reasoning": "short 1-sentence explanation"
}"""


AUDIT_AGENT_SYSTEM_PROMPT = """You are an autonomous AI Deletion-Audit Agent for Northstar Shop.
Your mission is to rigorously investigate whether a customer's personal data was truly deleted or if ghost data still exists across databases, replicas, search indexes, backups, and logs.
Evaluate all evidence strictly at the audit_at cutoff. Workflow statuses and operation claims cannot be accepted without proof."""


# ---------------------------------------------------------------------------
# Telemetry and Usage Tracking (for usage_report.md)
# ---------------------------------------------------------------------------

class LLMUsageTracker:
    """Tracks token counts, calls, and estimated cost across all agent invocations."""

    def __init__(self) -> None:
        self.call_count: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.model_counts: Dict[str, int] = {}

    def record_usage(self, model: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        self.call_count += 1
        self.input_tokens += prompt_tokens
        self.output_tokens += completion_tokens
        self.model_counts[model] = self.model_counts.get(model, 0) + 1

    def get_summary(self) -> Dict[str, Any]:
        return {
            "total_calls": self.call_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "model_breakdown": self.model_counts,
        }

    def reset(self) -> None:
        self.call_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.model_counts.clear()


# Global tracker instance
USAGE_TRACKER = LLMUsageTracker()


# ---------------------------------------------------------------------------
# Gemini API Invocation
# ---------------------------------------------------------------------------

def get_api_key(explicit_key: Optional[str] = None) -> Optional[str]:
    """Retrieve API key from argument or environment variables."""
    return explicit_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def call_gemini_json(
    prompt_text: str,
    system_prompt: str,
    model_name: str = "gemini-3.8-flash",
    api_key: Optional[str] = None,
    temperature: float = 0.0,
    timeout: int = 15,
) -> Optional[Dict[str, Any]]:
    """
    Execute a structured JSON prompt against the Gemini API.
    Returns parsed JSON dictionary or None if unavailable/errored.
    """
    key = get_api_key(api_key)
    if not key or requests is None:
        return None

    effective_model = os.environ.get("GEMINI_MODEL", model_name)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{effective_model}:generateContent?key={key}"

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt_text}]
            }
        ],
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": temperature
        }
    }

    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Extract usage metadata if returned by API
            usage = data.get("usageMetadata", {})
            p_tokens = usage.get("promptTokenCount", 0)
            c_tokens = usage.get("candidatesTokenCount", 0)
            USAGE_TRACKER.record_usage(effective_model, p_tokens, c_tokens)

            candidates = data.get("candidates", [])
            if candidates:
                raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                parsed = json.loads(raw_text)
                parsed["source"] = f"gemini:{effective_model}"
                return parsed
        return None
    except Exception:
        return None


def call_gemini_text(
    prompt_text: str,
    system_prompt: str,
    model_name: str = "gemini-3.8-flash",
    api_key: Optional[str] = None,
    temperature: float = 0.0,
    timeout: int = 15,
) -> Optional[str]:
    """
    Execute a general text prompt against the Gemini API.
    Returns response string or None if unavailable.
    """
    key = get_api_key(api_key)
    if not key or requests is None:
        return None

    effective_model = os.environ.get("GEMINI_MODEL", model_name)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{effective_model}:generateContent?key={key}"

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt_text}]
            }
        ],
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "generationConfig": {
            "temperature": temperature
        }
    }

    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            usage = data.get("usageMetadata", {})
            USAGE_TRACKER.record_usage(
                effective_model,
                usage.get("promptTokenCount", 0),
                usage.get("candidatesTokenCount", 0)
            )
            candidates = data.get("candidates", [])
            if candidates:
                return candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        return None
    except Exception:
        return None


if __name__ == "__main__":
    print("Testing LLM brain configuration:")
    print("Model:", os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"))
    print("API Key Configured:", bool(get_api_key()))
    print("Usage Tracker Initialized:", USAGE_TRACKER.get_summary())

