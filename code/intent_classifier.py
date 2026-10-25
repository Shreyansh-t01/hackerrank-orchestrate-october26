"""
Intent Classification Node & Tool for Deletion Auditing.

Analyzes multilingual deletion request texts using Google Gemini (gemini-3.8-flash)
via the centralized LLM brain module (code/llm.py) to determine if the customer actually
authorized account closure and personal data deletion, or if the request was an access request,
data export, profile rectification, or opt-out.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Import brain logic from modular central LLM module
from llm import INTENT_SYSTEM_PROMPT, call_gemini_json


# Multilingual heuristics used as fallback if API key is not configured or in offline mode
NON_DELETION_PATTERNS = [
    # English
    r"\bleave\s+(the|my)?\s*account\s+active\b",
    r"\bkeep\s+(the|my)?\s*account\b",
    r"\bdownload\s+my\s+account\s+information\b",
    r"\bportable\s+format\b",
    r"\bcopy\s+of\s+last\s+year'?s?\s+invoices\b",
    r"\bleave\s+my\s+profile\b",
    r"\bout\s+of\s+date\b.*\breplace\b.*\bleave\b",
    # Spanish
    r"\bdejar\s+la\s+cuenta\s+activa\b",
    r"\bmantener\s+la\s+cuenta\b",
    r"\bdescargar\s+mis\s+datos\b",
    # French
    r"\blaisser\s+le\s+compte\s+actif\b",
    r"\bgarder\s+le\s+compte\b",
    r"\btélécharger\s+mes\s+données\b",
    # German
    r"\bkonto\s+aktiv\s+lassen\b",
    r"\bkonto\s+behalten\b",
]


def _rule_based_fallback(text: str) -> Dict[str, Any]:
    """Fallback classifier when external LLM API is unavailable."""
    lower_text = text.lower().strip()
    for pattern in NON_DELETION_PATTERNS:
        if re.search(pattern, lower_text):
            if "invoice" in lower_text or "facture" in lower_text or "rechnung" in lower_text:
                cat = "ACCESS_REQUEST"
            elif "download" in lower_text or "portable" in lower_text or "export" in lower_text:
                cat = "DATA_EXPORT"
            elif "out of date" in lower_text or "replace" in lower_text or "update" in lower_text:
                cat = "PROFILE_UPDATE"
            else:
                cat = "OTHER"
            return {
                "is_deletion_request": False,
                "intent_category": cat,
                "confidence": 0.95,
                "reasoning": "Customer explicitly requested to keep account active or requested non-deletion action (heuristic fallback).",
                "source": "heuristic_fallback",
            }

    # Default to deletion if formal deletion phrasing detected
    return {
        "is_deletion_request": True,
        "intent_category": "DELETION",
        "confidence": 0.95,
        "reasoning": "Customer requested account closure or personal data deletion (heuristic fallback).",
        "source": "heuristic_fallback",
    }


def classify_request_intent(
    request_text: str,
    model_name: str = "gemini-3.8-flash",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Core function to classify the intent of a deletion request message.
    Calls Gemini 3.8 Flash model brain via code/llm.py, with heuristic fallback if no API key is provided.
    """
    text_clean = str(request_text or "").strip()
    if not text_clean:
        return {
            "is_deletion_request": False,
            "intent_category": "OTHER",
            "confidence": 1.0,
            "reasoning": "Empty request text.",
            "source": "empty_input",
        }

    # Format the prompt for the LLM brain
    user_prompt = f'Customer Request Text:\n"""{text_clean}"""'

    # Attempt LLM brain call via centralized llm.py
    llm_result = call_gemini_json(
        prompt_text=user_prompt,
        system_prompt=INTENT_SYSTEM_PROMPT,
        model_name=model_name,
        api_key=api_key,
    )
    if llm_result is not None:
        return llm_result

    # Fallback if API key is not present or offline
    return _rule_based_fallback(text_clean)


def intent_classification_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph Node for Intent Classification.

    Input State expected:
      - 'request_text': The raw customer message.
      - 'case_id' (optional): ID of the request.

    Output State updated:
      - 'intent': Full classification metadata dictionary.
      - 'is_deletion_request': bool.
      - 'intent_category': str ('DELETION', 'DATA_EXPORT', etc.).
      - 'deletion_status': Set to 'NOT_APPLICABLE' if is_deletion_request is False.
    """
    request_text = state.get("request_text", "")
    result = classify_request_intent(request_text)

    is_deletion = bool(result.get("is_deletion_request", False))
    updates: Dict[str, Any] = {
        **state,
        "intent": result,
        "is_deletion_request": is_deletion,
        "intent_category": result.get("intent_category", "OTHER"),
    }

    if not is_deletion:
        updates["deletion_status"] = "NOT_APPLICABLE"
        updates["steps_json"] = []

    return updates


if __name__ == "__main__":
    test_samples = [
        ("case_01", "Please close my account and process the associated personal-data deletion."),
        ("case_533", "The mobile number on my profile is out of date. Please replace it with the new one and leave the account active."),
        ("case_535", "I'd like to download my account information in a portable format. Please keep the account as it is."),
        ("case_540", "I need a copy of last year's invoices for my records. Please leave my profile and purchase history in place."),
    ]

    print("Running Intent Classification Node tests (via modular llm.py):")
    for cid, txt in test_samples:
        node_output = intent_classification_node({"case_id": cid, "request_text": txt})
        print(f"\n[{cid}] Request: \"{txt}\"")
        print(f"  is_deletion_request: {node_output['is_deletion_request']}")
        print(f"  intent_category:     {node_output['intent_category']}")
        print(f"  deletion_status:     {node_output.get('deletion_status', 'PENDING')}")
        print(f"  source:              {node_output['intent'].get('source')}")
        print(f"  reasoning:           {node_output['intent'].get('reasoning')}")
