"""
Intent Classification Node & Tool for Deletion Auditing.

Analyzes multilingual deletion request texts through the shared model transport to determine if the customer actually
authorized account closure and personal data deletion, or if the request was an access request,
data export, profile rectification, or opt-out.
"""

from __future__ import annotations

import re
import sys
import time
import os
from pathlib import Path
from typing import Any, Dict, Optional

if __package__ in {None, ""}:
    repository_root = str(Path(__file__).resolve().parent.parent)
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

from code.llm.client import call_gemini_json
from code.llm.config import get_api_key, get_intent_max_retries


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
Keeping an account active does not cancel an explicit request to delete personal data. Classify "delete my personal data but keep my account active" as DELETION. Classify a request to keep the account active as non-deletion only when no personal-data deletion is requested.

Respond ONLY with a valid JSON object matching this schema:
{
  "is_deletion_request": true or false,
  "intent_category": "DELETION" | "DATA_EXPORT" | "ACCESS_REQUEST" | "PROFILE_UPDATE" | "MARKETING_OPT_OUT" | "OTHER",
  "confidence": float between 0.0 and 1.0,
  "reasoning": "short 1-sentence explanation"
}"""


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

DELETION_PATTERNS = [
    # English
    r"\b(delete|erase|remove|wipe|close|terminate)\b.{0,70}\b(account|profile|personal data|my data|all my data|my information|records|everything)\b",
    r"\b(account|profile|personal data|my data|all my data|my information|records|everything)\b.{0,70}\b(delete|erase|remove|wipe)\b",
    # Spanish, French, German, Portuguese
    r"\b(elimina|borrar|suprime|supprimer|effacer|l[oö]schen|entfernen|excluir|apagar)\b",
]

NEGATED_DELETION_PATTERNS = [
    r"\b(do not|don't|dont|never|not)\b.{0,40}\b(delete|erase|remove|close)\b",
    r"\b(no quiero|no solicito|ne pas|nicht|não|nao)\b.{0,40}\b(eliminar|borrar|supprimer|löschen|excluir|apagar)\b",
]


def _rule_based_fallback(text: str) -> Dict[str, Any]:
    """Fallback classifier when external LLM API is unavailable."""
    lower_text = text.lower().strip()
    if any(re.search(pattern, lower_text, flags=re.IGNORECASE) for pattern in NEGATED_DELETION_PATTERNS):
        return {
            "is_deletion_request": False,
            "intent_category": "OTHER",
            "confidence": 0.9,
            "reasoning": "The message explicitly says not to perform deletion.",
            "source": "heuristic_fallback",
        }

    if any(re.search(pattern, lower_text, flags=re.IGNORECASE) for pattern in DELETION_PATTERNS):
        return {
            "is_deletion_request": True,
            "intent_category": "DELETION",
            "confidence": 0.8,
            "reasoning": "The message explicitly requests deletion or account closure.",
            "source": "heuristic_fallback",
        }

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

    # Ambiguous or unrelated text must not authorize deletion by default.
    return {
        "is_deletion_request": False,
        "intent_category": "OTHER",
        "confidence": 0.5,
        "reasoning": "The message does not clearly authorize deletion.",
        "source": "heuristic_fallback",
    }


def classify_request_intent(
    request_text: str,
    model_name: str | None = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Core function to classify the intent of a deletion request message.
    Calls the generic model client with this module's intent prompt. The classifier's
    deterministic fallback remains local to this feature.
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

    if os.environ.get("GEMINI_OFFLINE_INTENT", "").strip().lower() in {"1", "true", "yes"}:
        return _rule_based_fallback(text_clean)

    # The feature owns its prompt; the shared client only sends it.
    user_prompt = f'Customer Request Text:\n"""{text_clean}"""'

    # Retries are owned by this feature. Other model-backed nodes keep their
    # own policy and the shared transport remains a single-attempt client.
    max_retries = get_intent_max_retries() if get_api_key(api_key) else 0
    for attempt in range(max_retries + 1):
        llm_result = call_gemini_json(
            prompt_text=user_prompt,
            system_prompt=INTENT_SYSTEM_PROMPT,
            model_name=model_name,
            api_key=api_key,
        )
        if (
            isinstance(llm_result, dict)
            and isinstance(llm_result.get("is_deletion_request"), bool)
            and llm_result.get("intent_category") in {
                "DELETION", "DATA_EXPORT", "ACCESS_REQUEST", "PROFILE_UPDATE", "MARKETING_OPT_OUT", "OTHER"
            }
        ):
            return llm_result
        if attempt < max_retries:
            time.sleep(min(2.0, 0.5 * (2 ** attempt)))

    # Fallback if API key is not present or offline
    return _rule_based_fallback(text_clean)


def classify_intent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: classify intent using the prompt owned by this feature."""
    request_text = state.get("request_text", "")
    result = classify_request_intent(
        request_text=request_text,
    )
    is_deletion = bool(result.get("is_deletion_request", False))
    return {
        **state,
        "is_deletion_request": is_deletion,
        "intent_category": result.get("intent_category", "OTHER"),
        "intent_confidence": result.get("confidence", 1.0),
        "intent_reasoning": result.get("reasoning", ""),
        "intent_source": result.get("source", ""),
        "deletion_status": state.get("deletion_status") if is_deletion else "NOT_APPLICABLE",
        "steps_json": state.get("steps_json") if is_deletion else [],
    }


if __name__ == "__main__":
    test_samples = [
        ("case_01", "Please close my account and process the associated personal-data deletion."),
        ("case_533", "The mobile number on my profile is out of date. Please replace it with the new one and leave the account active."),
        ("case_535", "I'd like to download my account information in a portable format. Please keep the account as it is."),
        ("case_540", "I need a copy of last year's invoices for my records. Please leave my profile and purchase history in place."),
    ]

    print("Running intent classification examples via the modular LLM client:")
    for cid, txt in test_samples:
        node_output = classify_intent_node({"case_id": cid, "request_text": txt})
        print(f"\n[{cid}] Request: \"{txt}\"")
        print(f"  is_deletion_request: {node_output['is_deletion_request']}")
        print(f"  intent_category:     {node_output['intent_category']}")
        print(f"  deletion_status:     {node_output.get('deletion_status', 'PENDING')}")
        print(f"  source:              {node_output.get('intent_source')}")
        print(f"  reasoning:           {node_output.get('intent_reasoning')}")
