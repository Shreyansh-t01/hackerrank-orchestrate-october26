"""Append small JSON-line run events without logging request contents or PII."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "audit_runtime.jsonl"
SAFE_FIELDS = {
    "case_id", "stage", "status", "file_id", "file_type", "record_count",
    "error_type", "resource_count", "coverage_complete", "tool_name",
    "result", "model", "input_tokens", "output_tokens", "failed_calls",
    "step_count", "validation_error_count", "unresolved_count", "duration_ms",
}


def record_event(event: str, **fields) -> None:
    """Write only allow-listed operational metadata; never write prompts or identifiers."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    entry.update({key: value for key, value in fields.items() if key in SAFE_FIELDS})
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # A runtime log must never turn an otherwise recoverable audit into success.
        return
