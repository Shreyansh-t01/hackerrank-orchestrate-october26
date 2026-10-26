"""Strict, deterministic validation for the challenge output contract."""

from __future__ import annotations

import json
from typing import Iterable


STEP_FIELDS = {
    "step_no", "phase", "system_id", "resource_id",
    "operation", "state", "evidence_id",
}

OPERATION_RULES = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
OPERATION_ORDER = {
    "FINDING": [
        "REPORT_REMAINING_RECORD",
        "REPORT_EXPOSED_PII",
        "REPORT_ACTIVE_DATA_FLOW",
        "REPORT_RESTORE_RISK",
    ],
    "REMEDIATION": [
        "DISABLE_DATA_FLOW",
        "BLOCK_RESTORE",
        "DELETE_RECORD",
        "REDACT_RECORD",
        "ANONYMIZE_RECORD",
    ],
    "VERIFICATION": [
        "VERIFY_RECORD_ABSENT",
        "VERIFY_RECORD_PRESENT",
        "VERIFY_RECORD_REDACTED",
        "VERIFY_RECORD_ANONYMIZED",
        "VERIFY_DATA_FLOW_DISABLED",
        "VERIFY_RESTORE_BLOCKED",
    ],
}
REMEDIATION_FOR_FINDING = {
    "REPORT_REMAINING_RECORD": {"DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"},
    "REPORT_EXPOSED_PII": {"REDACT_RECORD", "ANONYMIZE_RECORD"},
    "REPORT_ACTIVE_DATA_FLOW": {"DISABLE_DATA_FLOW"},
    "REPORT_RESTORE_RISK": {"BLOCK_RESTORE"},
}
VERIFICATION_FOR_REMEDIATION = {
    "DISABLE_DATA_FLOW": "VERIFY_DATA_FLOW_DISABLED",
    "BLOCK_RESTORE": "VERIFY_RESTORE_BLOCKED",
    "DELETE_RECORD": "VERIFY_RECORD_ABSENT",
    "REDACT_RECORD": "VERIFY_RECORD_REDACTED",
    "ANONYMIZE_RECORD": "VERIFY_RECORD_ANONYMIZED",
}


def validate_steps(
    deletion_status: str,
    steps: list[dict],
    valid_evidence_ids: Iterable[str] | None = None,
    *,
    allow_incomplete_coverage: bool = False,
) -> list[str]:
    """Return contract errors for one parsed steps array."""
    errors: list[str] = []
    evidence_ids = set(valid_evidence_ids) if valid_evidence_ids is not None else None

    if deletion_status not in {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}:
        return [f"invalid deletion_status: {deletion_status!r}"]
    if deletion_status == "NOT_APPLICABLE" and steps:
        errors.append("NOT_APPLICABLE must have an empty steps array")
    if deletion_status == "COMPLETE" and not steps:
        errors.append("COMPLETE requires at least one evidence-backed verification step")
    if deletion_status == "COMPLETE" and any(step.get("phase") != "VERIFICATION" for step in steps):
        errors.append("COMPLETE may contain verification steps only")
    if deletion_status == "INCOMPLETE" and not allow_incomplete_coverage:
        phases_present = {step.get("phase") for step in steps}
        for phase in ("FINDING", "REMEDIATION", "VERIFICATION"):
            if phase not in phases_present:
                errors.append(f"INCOMPLETE requires at least one {phase} step")

    previous_phase = -1
    for index, step in enumerate(steps, start=1):
        prefix = f"step {index}"
        if set(step) != STEP_FIELDS:
            errors.append(f"{prefix}: fields must be exactly {sorted(STEP_FIELDS)}")
        if step.get("step_no") != index:
            errors.append(f"{prefix}: step_no must equal {index}")

        phase = step.get("phase")
        phase_rank = PHASE_ORDER.get(phase, -1)
        if phase_rank < 0:
            errors.append(f"{prefix}: invalid phase {phase!r}")
        elif phase_rank < previous_phase:
            errors.append(f"{prefix}: phase order is invalid")
        previous_phase = max(previous_phase, phase_rank)

        operation = step.get("operation")
        expected = OPERATION_RULES.get(operation)
        if expected is None:
            errors.append(f"{prefix}: invalid operation {operation!r}")
        elif (phase, step.get("state")) != expected:
            errors.append(f"{prefix}: operation, phase, and state do not match")

        if not step.get("system_id") or not step.get("resource_id"):
            errors.append(f"{prefix}: system_id and resource_id are required")

        evidence_id = step.get("evidence_id")
        requires_evidence = phase == "FINDING" or deletion_status == "COMPLETE"
        if requires_evidence and not evidence_id:
            errors.append(f"{prefix}: evidence_id is required for this step")
        elif not requires_evidence and evidence_id:
            errors.append(f"{prefix}: planned steps must have an empty evidence_id")
        elif evidence_id and evidence_ids is not None and evidence_id not in evidence_ids:
            errors.append(f"{prefix}: evidence_id is not in participant-facing sources")

    for phase, phase_order in OPERATION_ORDER.items():
        phase_steps = [step for step in steps if step.get("phase") == phase]
        ranks = [phase_order.index(step["operation"]) for step in phase_steps
                 if step.get("operation") in phase_order]
        if ranks != sorted(ranks):
            errors.append(f"{phase}: operation order does not match the problem statement")
        if phase in {"FINDING", "VERIFICATION"}:
            order_keys = [
                (
                    phase_order.index(step["operation"]),
                    step.get("system_id", ""),
                    step.get("resource_id", ""),
                )
                for step in phase_steps
                if step.get("operation") in phase_order
            ]
            if order_keys != sorted(order_keys):
                errors.append(f"{phase}: system_id/resource_id order is not ascending")

    if deletion_status == "INCOMPLETE":
        finding_steps = [step for step in steps if step.get("phase") == "FINDING"]
        remediation_steps = [step for step in steps if step.get("phase") == "REMEDIATION"]
        verification_steps = [step for step in steps if step.get("phase") == "VERIFICATION"]
        for finding in finding_steps:
            allowed = REMEDIATION_FOR_FINDING.get(finding.get("operation"), set())
            matching = [step for step in remediation_steps
                        if step.get("system_id") == finding.get("system_id")
                        and step.get("resource_id") == finding.get("resource_id")
                        and step.get("operation") in allowed]
            if not matching:
                errors.append(
                    f"{finding.get('resource_id')}: finding has no matching remediation step"
                )
        for remediation in remediation_steps:
            expected_verification = VERIFICATION_FOR_REMEDIATION.get(remediation.get("operation"))
            matching = any(
                step.get("system_id") == remediation.get("system_id")
                and step.get("resource_id") == remediation.get("resource_id")
                and step.get("operation") == expected_verification
                for step in verification_steps
            )
            if not matching:
                errors.append(
                    f"{remediation.get('resource_id')}: remediation has no matching verification step"
                )

    return errors


def validate_output_row(
    row: dict,
    valid_evidence_ids: Iterable[str] | None = None,
    *,
    allow_unresolved_incomplete: bool = False,
) -> list[str]:
    """Validate one serialized output row, including JSON syntax."""
    errors: list[str] = []
    if set(row) != {"case_id", "deletion_status", "steps_json"}:
        errors.append("output row must contain exactly case_id, deletion_status, steps_json")
    try:
        steps = json.loads(row.get("steps_json", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        return errors + [f"steps_json is not valid JSON: {exc}"]
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        return errors + ["steps_json must be an array of objects"]
    return errors + validate_steps(
        row.get("deletion_status", ""),
        steps,
        valid_evidence_ids,
        allow_incomplete_coverage=allow_unresolved_incomplete,
    )


def validate_output_rows(
    rows: list[dict],
    expected_case_ids: list[str],
    valid_evidence_ids: Iterable[str] | None = None,
    *,
    allow_unresolved_incomplete: bool = False,
) -> list[str]:
    """Validate row coverage, uniqueness, sort order, and each row payload."""
    errors: list[str] = []
    if valid_evidence_ids is not None:
        valid_evidence_ids = set(valid_evidence_ids)
    actual_ids = [str(row.get("case_id", "")) for row in rows]
    if len(actual_ids) != len(set(actual_ids)):
        errors.append("output contains duplicate case_id values")
    if set(actual_ids) != set(expected_case_ids) or len(actual_ids) != len(expected_case_ids):
        errors.append("output case_id values do not exactly match the input requests")
    if actual_ids != sorted(actual_ids):
        errors.append("output rows are not sorted by case_id")
    for row in rows:
        errors.extend(
            f"{row.get('case_id', '')}: {message}"
            for message in validate_output_row(
                row,
                valid_evidence_ids,
                allow_unresolved_incomplete=allow_unresolved_incomplete,
            )
        )
    return errors
