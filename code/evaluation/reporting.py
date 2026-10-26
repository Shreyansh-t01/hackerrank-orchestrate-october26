"""Write honest evaluation and model-usage reports for completed runs."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def _read_steps(row: dict) -> list[dict]:
    try:
        value = json.loads(row.get("steps_json", "[]"))
        return value if isinstance(value, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def write_evaluation_report(
    predictions: list[dict],
    reference_rows: list[dict],
    report_path: str | Path,
) -> None:
    """Compare predictions only with the public sample answer file."""
    pred = {row["case_id"]: row for row in predictions}
    reference = {row["case_id"]: row for row in reference_rows}
    status_correct = sum(
        case_id in pred and pred[case_id].get("deletion_status") == row.get("deletion_status")
        for case_id, row in reference.items()
    )
    exact_sequences = 0
    step_correct = step_total = field_correct = field_total = 0
    fields = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")

    for case_id, expected_row in reference.items():
        predicted_row = pred.get(case_id, {})
        expected_steps = _read_steps(expected_row)
        predicted_steps = _read_steps(predicted_row)
        if predicted_steps == expected_steps:
            exact_sequences += 1
        total_positions = max(len(expected_steps), len(predicted_steps))
        step_total += total_positions
        field_total += total_positions * len(fields)
        for index in range(total_positions):
            expected_step = expected_steps[index] if index < len(expected_steps) else None
            predicted_step = predicted_steps[index] if index < len(predicted_steps) else None
            if expected_step is not None and predicted_step is not None and expected_step == predicted_step:
                step_correct += 1
            for field in fields:
                if expected_step is not None and predicted_step is not None and expected_step.get(field) == predicted_step.get(field):
                    field_correct += 1

    sample_count = len(reference)
    status_accuracy = status_correct / sample_count if sample_count else 0
    sequence_accuracy = exact_sequences / sample_count if sample_count else 0
    step_accuracy = step_correct / step_total if step_total else 0
    field_accuracy = field_correct / field_total if field_total else 0
    generated = datetime.now(timezone.utc).isoformat()

    content = f"""# Evaluation Report — Ghost Data Still Exists

Generated: {generated}

## Public sample comparison

The reference is `dataset/sample_output.csv` (public cases only). Hidden evaluation labels are not available to this report.

- Cases compared: {sample_count}
- Case-level deletion-status accuracy: {status_correct}/{sample_count} ({status_accuracy:.1%})
- Step-level exact-position accuracy: {step_correct}/{step_total} ({step_accuracy:.1%})
- Field-level accuracy across the seven required fields: {field_correct}/{field_total} ({field_accuracy:.1%})
- Exact ordered-sequence accuracy: {exact_sequences}/{sample_count} ({sequence_accuracy:.1%})

## Additional designed cases

1. **Historical identifier with reassignment:** Resolve an old email using its ownership interval at `requested_at`; do not connect the new owner to the prior user's data. Expected: audit only the original user's evidence and mark unresolved identity as incomplete.
2. **Deletion requested while the account stays active:** “Delete my personal data, but leave my account active.” Expected: deletion is authorized; preserve an account tombstone only when policy requires it and redact direct identifiers.
3. **Restore claim newer than a snapshot:** A workflow reports a successful restore after the last source snapshot. Expected: the old snapshot is stale, the current state is unverified, and the case cannot be marked complete until a later source snapshot resolves it.

These cases are design checks; this report does not claim they were executed unless they are included in a separately recorded test run.

## Failures found and improvements made

- Activity `SUCCESS` results no longer overwrite a source snapshot as proof of absence or presence. A newer state-changing claim makes the snapshot stale and the resource state unknown.
- An empty resource search no longer produces `COMPLETE`; source coverage and identity resolution are checked separately.
- Historical identifiers are gathered only from the resolved user's own temporal edges, preventing traversal through a recycled identifier to another user.
- PDF and image evidence is read with local OCR and interpreted by the configured text model; extracted records are cached by source hash, prompt, OCR engine version, and model.
- Output rows are validated independently for schema, allowed operations, phase order, step numbering, and evidence references before writing.

## Limitations

- Case-level and step-level metrics against hidden canonical answers require the organizer's reference labels and are not inferred from workflow status claims.
- Costs are reported only when token usage and model rates are available from the run configuration.
"""
    run_notes = []
    if os.environ.get("GEMINI_OFFLINE_INTENT", "").strip().lower() in {"1", "true", "yes"}:
        run_notes.append("Intent classification used local deterministic rules; no Gemini intent calls were made.")
    if os.environ.get("AUDIT_INDEX_COVERAGE_COMPLETE") == "0":
        run_notes.append("The supplied SQLite index had incomplete evidence-source coverage during this run.")
    if run_notes:
        note_text = "\n".join(f"- {note}" for note in run_notes)
        content = content.replace("## Limitations", f"## Run conditions\n\n{note_text}\n\n## Limitations")
    Path(report_path).write_text(content, encoding="utf-8")


def write_usage_report(results_meta: list[dict], report_path: str | Path) -> None:
    """Write call, token, failure, and configurable cost totals for a run."""
    from code.llm.usage import USAGE_TRACKER

    summary = USAGE_TRACKER.get_summary()
    cases = len(results_meta)
    tokens = summary["total_tokens"]
    avg_tokens = tokens / cases if cases else 0
    breakdown = summary["model_breakdown"]
    if not breakdown:
        configured_models = {
            os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
        }
        breakdown = {
            model: {"calls": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0}
            for model in configured_models
        }

    rates: dict[str, tuple[float, float] | None] = {}
    for model in breakdown:
        model_key = re.sub(r"[^A-Z0-9]+", "_", model.upper()).strip("_")
        input_rate = os.environ.get(f"GEMINI_PRICE_{model_key}_INPUT_USD_PER_MILLION") or os.environ.get("GEMINI_INPUT_USD_PER_MILLION")
        output_rate = os.environ.get(f"GEMINI_PRICE_{model_key}_OUTPUT_USD_PER_MILLION") or os.environ.get("GEMINI_OUTPUT_USD_PER_MILLION")
        rates[model] = (float(input_rate), float(output_rate)) if input_rate and output_rate else None
    used_models = [model for model, stats in breakdown.items() if stats["calls"]]
    cost_configured = bool(used_models) and all(rates[model] is not None for model in used_models)

    lines = [
        "# Usage Report — Ghost Data Still Exists",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Run summary",
        "",
        "- Provider: Google Gemini API",
        f"- Cases processed: {cases}",
        f"- Model calls: {summary['total_calls']}",
        f"- Failed model calls: {summary['failed_calls']}",
        f"- Input tokens: {summary['input_tokens']:,}",
        f"- Output tokens: {summary['output_tokens']:,}",
        f"- Total tokens: {tokens:,}",
        f"- Average tokens per case: {avg_tokens:,.1f}",
        "",
        "## Per-model totals",
        "",
    ]
    if not breakdown:
        lines.append("No model calls were recorded.")
    for model, stats in sorted(breakdown.items()):
        model_cost = None
        model_rates = rates.get(model)
        if model_rates is not None:
            model_cost = (
                stats["input_tokens"] * model_rates[0] / 1_000_000
                + stats["output_tokens"] * model_rates[1] / 1_000_000
            )
        cost_text = f"${model_cost:.6f}" if model_cost is not None else "not calculated (model rates are not configured)"
        lines.append(
            f"- **{model}:** {stats['calls']} calls, {stats['failures']} failures, "
            f"{stats['input_tokens']:,} input tokens, {stats['output_tokens']:,} output tokens, estimated cost {cost_text}"
        )

    if cost_configured:
        total_cost = sum(
            stats["input_tokens"] * rates[model][0] / 1_000_000
            + stats["output_tokens"] * rates[model][1] / 1_000_000
            for model, stats in breakdown.items()
            if rates[model] is not None
        )
        per_case = total_cost / cases if cases else 0
        lines += [
            "",
            "## Estimated cost",
            "",
            f"- Total: ${total_cost:.6f}",
            f"- Per case: ${per_case:.6f}",
            "- Rates come from the model-specific `GEMINI_PRICE_<MODEL>_INPUT_USD_PER_MILLION` and `GEMINI_PRICE_<MODEL>_OUTPUT_USD_PER_MILLION` settings, or the shared fallback settings.",
        ]
    else:
        lines += [
            "",
            "## Estimated cost",
            "",
            "Not calculated for every used model. Set model-specific `GEMINI_PRICE_<MODEL>_INPUT_USD_PER_MILLION` and `GEMINI_PRICE_<MODEL>_OUTPUT_USD_PER_MILLION` values (or shared fallback rates); no price was assumed.",
        ]

    lines += [
        "",
        "## Notes",
        "",
        "- PDFs and images use local OCR followed by the configured text model during index construction. Cached extraction call and token totals are restored into later full-run reports, including when the SQLite index is rebuilt separately.",
        "- Token counts come from Gemini `usageMetadata` when the API returns it. Calls without usage metadata cannot be assigned token totals.",
        "- Intent classification may use deterministic fallback rules when no model call is available.",
    ]
    if os.environ.get("GEMINI_OFFLINE_INTENT", "").strip().lower() in {"1", "true", "yes"}:
        lines.append("- Intent classification used local deterministic rules in offline mode; no intent model calls were made.")
    if os.environ.get("AUDIT_INDEX_COVERAGE_COMPLETE") == "0":
        lines.append("- The existing SQLite index had incomplete evidence coverage; no PDF or image indexing was performed during this run.")
    Path(report_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
