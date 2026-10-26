"""
step_builder.py — Build ordered audit steps from resource evaluations.

Produces the final steps_json list for a case's output row.
Steps are ordered: FINDING → REMEDIATION → VERIFICATION.
Within each phase, ordering follows operation priority, then system_id, then resource_id.
"""

from __future__ import annotations

from typing import Optional

from code.evaluation_engine import ResourceEvaluation, ResourceState


# ---------------------------------------------------------------------------
# Operation ordering within phases
# ---------------------------------------------------------------------------

FINDING_OP_ORDER = [
    "REPORT_REMAINING_RECORD",
    "REPORT_EXPOSED_PII",
    "REPORT_ACTIVE_DATA_FLOW",
    "REPORT_RESTORE_RISK",
]

REMEDIATION_OP_ORDER = [
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "DELETE_RECORD",
    "REDACT_RECORD",
    "ANONYMIZE_RECORD",
]

VERIFICATION_OP_ORDER = [
    "VERIFY_RECORD_ABSENT",
    "VERIFY_RECORD_PRESENT",
    "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED",
    "VERIFY_RESTORE_BLOCKED",
]


def _op_rank(op: str, order_list: list[str]) -> int:
    try:
        return order_list.index(op)
    except ValueError:
        return len(order_list)


def _sort_key(step: dict, order_list: list[str]) -> tuple:
    return (
        _op_rank(step["operation"], order_list),
        step.get("system_id", ""),
        step.get("resource_id", ""),
    )


def _remediation_sort_key(step: dict) -> tuple:
    """Follow policy operation order and retain downstream-first ties."""
    return (
        _op_rank(step["operation"], REMEDIATION_OP_ORDER),
        -step.get("dependency_rank", 0),
        step.get("system_id", ""),
        step.get("resource_id", ""),
    )


# ---------------------------------------------------------------------------
# State strings for operations
# ---------------------------------------------------------------------------

OP_STATE_MAP = {
    "REPORT_REMAINING_RECORD":   "PRESENT",
    "REPORT_EXPOSED_PII":        "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW":   "ACTIVE",
    "REPORT_RESTORE_RISK":       "ACTIVE",
    "DISABLE_DATA_FLOW":         "DISABLED",
    "BLOCK_RESTORE":             "BLOCKED",
    "DELETE_RECORD":             "ABSENT",
    "REDACT_RECORD":             "REDACTED",
    "ANONYMIZE_RECORD":          "ANONYMIZED",
    "VERIFY_RECORD_ABSENT":      "ABSENT",
    "VERIFY_RECORD_PRESENT":     "PRESENT",
    "VERIFY_RECORD_REDACTED":    "REDACTED",
    "VERIFY_RECORD_ANONYMIZED":  "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED":    "BLOCKED",
}


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_steps(
    evaluations: list[ResourceEvaluation],
    deletion_status: str,
) -> list[dict]:
    """Build the steps_json list for a case.

    Args:
        evaluations: List of ResourceEvaluation objects from evaluation_engine.
        deletion_status: "COMPLETE" or "INCOMPLETE" (NOT_APPLICABLE → caller returns [])

    Returns:
        Numbered list of step dicts in the required format.
    """
    if deletion_status == "NOT_APPLICABLE":
        return []

    if not evaluations:
        return []

    findings:     list[dict] = []
    remediations: list[dict] = []
    verifications: list[dict] = []

    if deletion_status == "INCOMPLETE":
        # ── Findings: only non-compliant resources
        for ev in evaluations:
            if not ev.is_compliant and ev.finding_op and ev.evidence_ids:
                findings.append({
                    "phase":       "FINDING",
                    "operation":   ev.finding_op,
                    "system_id":   ev.system_id,
                    "resource_id": ev.resource_id,
                    "state":       OP_STATE_MAP.get(ev.finding_op, ev.finding_state or "PRESENT"),
                    "evidence_id": ev.evidence_ids[0] if ev.evidence_ids else "",
                })

        # Sort findings
        findings.sort(key=lambda s: _sort_key(s, FINDING_OP_ORDER))

        # ── Remediation: fix steps for non-compliant resources
        # Order: disable flows/block restores BEFORE deleting/redacting their sources
        for ev in evaluations:
            if not ev.is_compliant and ev.remediation_op:
                remediations.append({
                    "phase":       "REMEDIATION",
                    "operation":   ev.remediation_op,
                    "system_id":   ev.system_id,
                    "resource_id": ev.resource_id,
                    "state":       OP_STATE_MAP.get(ev.remediation_op, ""),
                    "evidence_id": "",  # future work
                    "dependency_rank": ev.dependency_rank,
                })

        remediations.sort(key=_remediation_sort_key)

        # ── Verification: ALL resources in portfolio must be verified in final state
        for ev in evaluations:
            if ev.verify_op:
                verifications.append({
                    "phase":       "VERIFICATION",
                    "operation":   ev.verify_op,
                    "system_id":   ev.system_id,
                    "resource_id": ev.resource_id,
                    "state":       OP_STATE_MAP.get(ev.verify_op, ""),
                    "evidence_id": "",  # post-remediation checks, no evidence yet
                })

    else:  # COMPLETE
        # ── No findings, no remediation
        # ── Verification: prove all resources are in compliant state
        for ev in evaluations:
            if ev.verify_op:
                verifications.append({
                    "phase":       "VERIFICATION",
                    "operation":   ev.verify_op,
                    "system_id":   ev.system_id,
                    "resource_id": ev.resource_id,
                    "state":       OP_STATE_MAP.get(ev.verify_op, ""),
                    "evidence_id": ev.evidence_ids[0] if ev.evidence_ids else "",
                })

    verifications.sort(key=lambda s: _sort_key(s, VERIFICATION_OP_ORDER))

    # ── Deduplicate (same operation + system + resource in same phase)
    def _dedup(steps: list[dict]) -> list[dict]:
        seen = set()
        out = []
        for s in steps:
            key = (s["phase"], s["operation"], s["system_id"], s["resource_id"])
            if key not in seen:
                seen.add(key)
                out.append(s)
        return out

    findings      = _dedup(findings)
    remediations  = _dedup(remediations)
    verifications = _dedup(verifications)

    # ── Combine and number
    all_steps = findings + remediations + verifications
    result = []
    for i, s in enumerate(all_steps, start=1):
        result.append({
            "step_no":    i,
            "phase":      s["phase"],
            "system_id":  s["system_id"],
            "resource_id":s["resource_id"],
            "operation":  s["operation"],
            "state":      s["state"],
            "evidence_id":s["evidence_id"],
        })

    return result

