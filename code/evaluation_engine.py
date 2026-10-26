"""
evaluation_engine.py — Deterministic deletion-audit evaluation.

Reconstructs the true state of each resource at audit_at from all available
evidence, applies deletion_rules.csv policy, and decides COMPLETE/INCOMPLETE.

No LLM calls are made here — this is fully deterministic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from code.identity_resolver import parse_iso_timestamp
from code.data_catalog import (
    SPECIAL_CATEGORY_ACTIONS,
    canonical_category,
    resolve_source_scope,
)

from code.resource_discovery import (
    get_activity_for_resource,
    get_activity_for_user,
    get_deletion_rules,
    get_resource_history,
    search_resources,
    find_resources_by_user_id,
    get_connector_resources,
    get_backup_resources,
    search_activity_by_event_type,
    get_related_resources,
    get_resource_dependencies,
)


DB_PATH = Path(__file__).parent.parent / "data" / "resource_index.db"

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ResourceState(str, Enum):
    PRESENT      = "PRESENT"
    ABSENT       = "ABSENT"
    REDACTED     = "REDACTED"
    ANONYMIZED   = "ANONYMIZED"
    ACTIVE       = "ACTIVE"    # for data flows / backups (restore-capable)
    DISABLED     = "DISABLED"  # data flow stopped
    BLOCKED      = "BLOCKED"   # backup blocked from restore
    UNKNOWN      = "UNKNOWN"


class RequiredAction(str, Enum):
    DELETE    = "DELETE"
    REDACT    = "REDACT"
    ANONYMIZE = "ANONYMIZE"
    RETAIN    = "RETAIN"
    # Special
    TOMBSTONE = "ACCOUNT_TOMBSTONE"  # keep stable ID, redact direct identifiers
    DISABLE = "DISABLE"              # special process state for active data flows


# Target compliant state per required action
ACTION_TO_TARGET_STATE: dict[str, ResourceState] = {
    "DELETE":            ResourceState.ABSENT,
    "REDACT":            ResourceState.REDACTED,
    "ANONYMIZE":         ResourceState.ANONYMIZED,
    "RETAIN":            ResourceState.PRESENT,
    "ACCOUNT_TOMBSTONE": ResourceState.REDACTED,
}

# Whether a current state satisfies the required action
COMPLIANT_STATES: dict[str, set[ResourceState]] = {
    "DELETE":            {ResourceState.ABSENT},
    "REDACT":            {ResourceState.REDACTED, ResourceState.ABSENT},
    "ANONYMIZE":         {ResourceState.ANONYMIZED, ResourceState.ABSENT},
    "RETAIN":            {ResourceState.PRESENT, ResourceState.REDACTED, ResourceState.ANONYMIZED},
    "ACCOUNT_TOMBSTONE": {ResourceState.REDACTED},
}

# Remediation operation per required action
ACTION_TO_REMEDIATION_OP: dict[str, str] = {
    "DELETE":            "DELETE_RECORD",
    "REDACT":            "REDACT_RECORD",
    "ANONYMIZE":         "ANONYMIZE_RECORD",
    "RETAIN":            "",  # no remediation needed — but may need verification
    "ACCOUNT_TOMBSTONE": "REDACT_RECORD",
}

# Verification operation per required action
ACTION_TO_VERIFY_OP: dict[str, str] = {
    "DELETE":            "VERIFY_RECORD_ABSENT",
    "REDACT":            "VERIFY_RECORD_REDACTED",
    "ANONYMIZE":         "VERIFY_RECORD_ANONYMIZED",
    "RETAIN":            "VERIFY_RECORD_PRESENT",
    "ACCOUNT_TOMBSTONE": "VERIFY_RECORD_REDACTED",
}

# Data-flow / backup verifications
DATA_FLOW_VERIFY_OP   = "VERIFY_DATA_FLOW_DISABLED"
RESTORE_VERIFY_OP     = "VERIFY_RESTORE_BLOCKED"

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ResourceEvaluation:
    """Complete evaluation of one resource for a deletion case."""
    resource_id:    str
    system_id:      str
    data_category:  str
    required_action: str
    current_state:  ResourceState
    target_state:   ResourceState
    is_compliant:   bool
    evidence_ids:   list[str] = field(default_factory=list)
    finding_op:     str = ""        # REPORT_* if non-compliant
    finding_state:  str = ""
    remediation_op: str = ""
    verify_op:      str = ""
    notes:          str = ""
    dependency_rank: int = 0


# ---------------------------------------------------------------------------
# State reconstruction
# ---------------------------------------------------------------------------

# Event types in system_activity that indicate deletion
DELETION_EVENT_TYPES = {
    "DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD",
    "PURGE_RECORD", "ACCOUNT_DELETED", "ACCOUNT_CLOSURE",
}

# Event types that indicate restore / recreation
RESTORE_EVENT_TYPES = {
    "RESTORE_RECORD", "SYNC_RECORD", "IMPORT_RECORD",
    "RESTORE_FROM_BACKUP", "REPLICATE_RECORD",
}


def _raw_state_to_enum(raw: str | None) -> ResourceState:
    if not raw:
        return ResourceState.UNKNOWN
    raw = raw.strip().upper()
    mapping = {
        "PRESENT":     ResourceState.PRESENT,
        "FOUND":       ResourceState.PRESENT,
        "IDENTIFIABLE":ResourceState.PRESENT,
        "ACTIVE":      ResourceState.ACTIVE,
        "ABSENT":      ResourceState.ABSENT,
        "NOT_FOUND":   ResourceState.ABSENT,
        "REDACTED":    ResourceState.REDACTED,
        "ANONYMIZED":  ResourceState.ANONYMIZED,
        "DISABLED":    ResourceState.DISABLED,
        "BLOCKED":     ResourceState.BLOCKED,
        "DELETION_PENDING": ResourceState.PRESENT,
    }
    return mapping.get(raw, ResourceState.UNKNOWN)


def reconstruct_resource_state(
    resource_id: str,
    system_id: str,
    audit_at: str,
    db_path: str | Path,
) -> tuple[ResourceState, list[str]]:
    """Compatibility wrapper; activity claims never replace source snapshots."""
    state, evidence_ids, _note = reconstruct_snapshot_state(
        resource_id, system_id, audit_at, db_path
    )
    return state, evidence_ids


# ---------------------------------------------------------------------------
# Data category mapping
# ---------------------------------------------------------------------------

def reconstruct_snapshot_state(
    resource_id: str,
    system_id: str,
    audit_at: str,
    db_path: str | Path,
    *,
    data_category: str = "",
    required_action: str = "",
) -> tuple[ResourceState, list[str], str]:
    """Use a time-valid source snapshot as state evidence; activity is only a claim."""
    history = get_resource_history(db_path, resource_id, system_id, audit_at=audit_at)
    if not history:
        return ResourceState.UNKNOWN, [], "No time-valid source snapshot establishes this resource's state."

    latest = history[-1]
    state = _raw_state_to_enum(latest.get("state"))
    evidence_id = latest.get("evidence_id") or latest.get("source_file") or ""
    evidence_ids = [evidence_id] if evidence_id else []
    snapshot_at = latest.get("captured_at") or ""

    same_time = [row for row in history if (row.get("captured_at") or "") == snapshot_at]
    same_time_states = {_raw_state_to_enum(row.get("state")) for row in same_time}
    if len(same_time_states) > 1:
        evidence_ids = list(dict.fromkeys(
            row.get("evidence_id") or row.get("source_file") or "" for row in same_time
            if row.get("evidence_id") or row.get("source_file")
        ))
        if data_category == "DATA_FLOW":
            compliant_states = {ResourceState.DISABLED, ResourceState.ABSENT}
            target_state = ResourceState.DISABLED
        elif data_category == "BACKUP_ARCHIVE":
            compliant_states = {ResourceState.BLOCKED, ResourceState.ABSENT}
            target_state = ResourceState.BLOCKED
        else:
            compliant_states = COMPLIANT_STATES.get(required_action, set())
            target_state = ACTION_TO_TARGET_STATE.get(required_action, ResourceState.UNKNOWN)

        if required_action == "RETAIN" and any(
            value in same_time_states
            for value in (ResourceState.PRESENT, ResourceState.REDACTED, ResourceState.ANONYMIZED)
        ):
            state = ResourceState.PRESENT if ResourceState.PRESENT in same_time_states else next(
                value for value in same_time_states
                if value in {ResourceState.REDACTED, ResourceState.ANONYMIZED}
            )
            return state, evidence_ids, "Same-time source copies disagree; a retained copy is still present."
        if required_action == "ACCOUNT_TOMBSTONE" and ResourceState.REDACTED in same_time_states:
            return ResourceState.REDACTED, evidence_ids, "Same-time source copies disagree; a compliant tombstone remains."

        violations = same_time_states - compliant_states
        if violations:
            # A same-time replica disagreement is resolved conservatively: if
            # any copy still exposes data or can recreate it, plan remediation.
            if ResourceState.ACTIVE in violations:
                state = ResourceState.ACTIVE
            elif violations.intersection({
                ResourceState.PRESENT,
                ResourceState.REDACTED,
                ResourceState.ANONYMIZED,
                ResourceState.DISABLED,
                ResourceState.BLOCKED,
            }):
                state = ResourceState.PRESENT
            else:
                state = ResourceState.UNKNOWN
        else:
            state = target_state
        return state, evidence_ids, "Same-time source copies disagree; the evaluation uses a conservative state."

    # A later activity event may mean the snapshot is stale, but its reported
    # result alone cannot prove that the operation changed the resource.
    activity = get_activity_for_resource(db_path, resource_id, audit_at=audit_at)
    later_claims = [
        event for event in activity
        if event.get("event_time")
        and event["event_time"] > snapshot_at
        and (event.get("target_system") or event.get("source_system") or "") == system_id
        and (event.get("event_type") or "").upper() in DELETION_EVENT_TYPES | RESTORE_EVENT_TYPES
    ]
    if later_claims:
        refs = [event.get("activity_id", "") for event in later_claims if event.get("activity_id")]
        note = "A state-changing activity claim is newer than the latest source snapshot; its result is unverified."
        if refs:
            note += " Activity references: " + ", ".join(refs)
        return ResourceState.UNKNOWN, evidence_ids, note

    if state == ResourceState.UNKNOWN:
        return state, evidence_ids, "The latest source snapshot has an unrecognized state."
    return state, evidence_ids, ""

def _system_to_default_category(system_id: str) -> str:
    _normalized_system, category = resolve_source_scope(system_id)
    return category or "UNKNOWN"


def _resource_id_to_category(rid: str) -> str:
    _system_id, category = resolve_source_scope("", resource_id=rid)
    return category


def _resolve_policy_action(category: str, rule: dict | None, pii_fields: dict, audit_at: str) -> str:
    """Apply policy conditions that can be decided from indexed source fields."""
    if category in SPECIAL_CATEGORY_ACTIONS:
        return SPECIAL_CATEGORY_ACTIONS[category]
    if not rule:
        return "UNKNOWN"
    action = rule.get("required_action", "UNKNOWN")
    if category == "SECURITY_AUDIT_LOG":
        retention_until = pii_fields.get("retention_until")
        if not retention_until:
            return "UNKNOWN"
        valid_until = parse_iso_timestamp(retention_until)
        cutoff = parse_iso_timestamp(audit_at)
        return "RETAIN" if valid_until and cutoff and valid_until >= cutoff else "UNKNOWN"
    if category == "LEGAL_HOLD_RECORD":
        retained_until = pii_fields.get("retained_until") or pii_fields.get("retention_until")
        if not retained_until:
            return "UNKNOWN"
        valid_until = parse_iso_timestamp(retained_until)
        cutoff = parse_iso_timestamp(audit_at)
        return "RETAIN" if valid_until and cutoff and valid_until >= cutoff else "UNKNOWN"
    return action


# ---------------------------------------------------------------------------
# Portfolio building
# ---------------------------------------------------------------------------

def build_portfolio(
    db_path: str | Path,
    user_id: str,
    all_identifiers: list[str],
    audit_at: str,
    deletion_rules: dict[str, dict],
    identifier_intervals: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Discover all resources belonging to the user and attach their required action.

    Returns list of dicts: {resource_id, system_id, data_category, required_action,
                             evidence_id, state_raw, ...}
    """
    # Combine user_id with all identifiers for broadest search
    search_ids = list(dict.fromkeys([user_id] + all_identifiers))

    resources = search_resources(
        db_path, search_ids, audit_at=audit_at, identifier_intervals=identifier_intervals
    )

    # Also search by stable user_id reference
    if user_id:
        user_resources = find_resources_by_user_id(db_path, user_id, audit_at=audit_at)
        existing_keys = {(r["resource_id"], r["system_id"]) for r in resources}
        for ur in user_resources:
            if (ur["resource_id"], ur["system_id"]) not in existing_keys:
                resources.append(ur)

    # Follow explicit resource links (for example, a search row's
    # source_resource_id) for at most two hops. This is a small, deterministic
    # investigation loop; it never follows arbitrary text or model suggestions.
    frontier = list(resources)
    for _ in range(2):
        frontier_keys = [(item.get("resource_id", ""), item.get("system_id", "")) for item in frontier]
        related = get_related_resources(db_path, frontier_keys, audit_at=audit_at)
        existing = {(item.get("resource_id"), item.get("system_id")) for item in resources}
        frontier = [
            item for item in related
            if (item.get("resource_id"), item.get("system_id")) not in existing
        ]
        if not frontier:
            break
        resources.extend(frontier)

    portfolio = []
    seen_keys = set()
    for r in resources:
        rid  = r.get("resource_id", "")
        sid  = r.get("system_id", "")
        if not rid or not sid:
            continue
        key = (rid, sid)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        cat = (
            canonical_category(r.get("data_category"))
            or _resource_id_to_category(rid)
            or _system_to_default_category(sid)
        )

        rule = deletion_rules.get(cat)
        pii_fields = r.get("pii_fields", {})
        required_action = _resolve_policy_action(cat, rule, pii_fields, audit_at)

        portfolio.append({
            "resource_id":      rid,
            "system_id":        sid,
            "data_category":    cat,
            "required_action":  required_action,
            "evidence_id":      r.get("evidence_id") or r.get("source_file") or "",
            "state_raw":        r.get("state"),
            "pii_fields":       pii_fields,
            "captured_at":      r.get("captured_at"),
        })

    return portfolio


# ---------------------------------------------------------------------------
# Evaluate a single resource
# ---------------------------------------------------------------------------

def evaluate_resource(
    resource_id: str,
    system_id: str,
    data_category: str,
    required_action: str,
    audit_at: str,
    db_path: str | Path,
) -> ResourceEvaluation:
    """Evaluate a single resource against its required action at audit_at."""
    target_state = ACTION_TO_TARGET_STATE.get(required_action, ResourceState.UNKNOWN)
    compliant_set = COMPLIANT_STATES.get(required_action, set())
    verify_op = ACTION_TO_VERIFY_OP.get(required_action, "")
    remediation_op = ACTION_TO_REMEDIATION_OP.get(required_action, "")
    if data_category == "DATA_FLOW":
        target_state, verify_op = ResourceState.DISABLED, "VERIFY_DATA_FLOW_DISABLED"
    elif data_category == "BACKUP_ARCHIVE":
        target_state, verify_op = ResourceState.BLOCKED, "VERIFY_RESTORE_BLOCKED"
    if required_action == "UNKNOWN" or not verify_op:
        raise ValueError(
            f"Relevant resource has no usable policy action: {system_id}/{resource_id} ({data_category})"
        )

    current_state, evidence_ids, notes = reconstruct_snapshot_state(
        resource_id,
        system_id,
        audit_at,
        db_path,
        data_category=data_category,
        required_action=required_action,
    )

    finding_op = ""
    finding_st = ""
    rem_op = ""
    vfy_op = verify_op
    tgt_state = target_state

    if current_state == ResourceState.UNKNOWN:
        is_compliant = False
    elif data_category == "DATA_FLOW":
        if current_state in {ResourceState.DISABLED, ResourceState.ABSENT}:
            is_compliant = True
        elif current_state == ResourceState.ACTIVE:
            is_compliant = False
            finding_op, finding_st, rem_op = "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "DISABLE_DATA_FLOW"
        else:
            is_compliant = False
            notes = "The data-flow record exists, but its enabled state is not established."
    elif data_category == "BACKUP_ARCHIVE":
        if current_state in {ResourceState.BLOCKED, ResourceState.ABSENT}:
            is_compliant = True
        elif current_state == ResourceState.ACTIVE:
            is_compliant = False
            finding_op, finding_st, rem_op = "REPORT_RESTORE_RISK", "ACTIVE", "BLOCK_RESTORE"
        else:
            is_compliant = False
            notes = "The backup exists, but restore access is not established."
    else:
        is_compliant = current_state in compliant_set
        if not is_compliant:
            if required_action in ("REDACT", "ACCOUNT_TOMBSTONE") and current_state == ResourceState.PRESENT:
                finding_op = "REPORT_EXPOSED_PII"
            elif current_state == ResourceState.PRESENT:
                finding_op = "REPORT_REMAINING_RECORD"
            rem_op = remediation_op

    # A policy-compliant label without a participant-visible citation is not proof.
    if is_compliant and not evidence_ids:
        is_compliant = False
        current_state = ResourceState.UNKNOWN
        notes = "The observed state has no participant-visible evidence reference."

    return ResourceEvaluation(
        resource_id=resource_id,
        system_id=system_id,
        data_category=data_category,
        required_action=required_action,
        current_state=current_state,
        target_state=tgt_state,
        is_compliant=is_compliant,
        evidence_ids=evidence_ids,
        finding_op=finding_op,
        finding_state=finding_st,
        remediation_op=rem_op,
        verify_op=vfy_op,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Evaluate all resources (full case evaluation)
# ---------------------------------------------------------------------------

def evaluate_case(
    db_path: str | Path,
    user_id: str,
    all_identifiers: list[str],
    audit_at: str,
    is_deletion_request: bool,
    portfolio: list[dict] | None = None,
    source_coverage: dict | None = None,
    identity_resolved: bool = True,
) -> tuple[str, list[ResourceEvaluation]]:
    """Evaluate a deletion case and return (deletion_status, evaluations).

    deletion_status is one of: COMPLETE, INCOMPLETE, NOT_APPLICABLE

    Args:
        db_path: Path to SQLite index.
        user_id: Stable user_id resolved for this case.
        all_identifiers: All identifiers (emails, hashes, user_id) for this user.
        audit_at: ISO-8601 audit cutoff.
        is_deletion_request: If False, returns NOT_APPLICABLE immediately.
    """
    if not is_deletion_request:
        return "NOT_APPLICABLE", []

    deletion_rules = get_deletion_rules(db_path)
    source_coverage = source_coverage or get_source_coverage(db_path, audit_at)

    # Build portfolio of all resources
    if portfolio is None:
        portfolio = build_portfolio(db_path, user_id, all_identifiers, audit_at, deletion_rules)

    if not portfolio:
        # A no-result search is not proof that no data remains.
        return "INCOMPLETE", []

    evaluations = []
    for item in portfolio:
        ev = evaluate_resource(
            resource_id=item["resource_id"],
            system_id=item["system_id"],
            data_category=item["data_category"],
            required_action=item["required_action"],
            audit_at=audit_at,
            db_path=db_path,
        )
        evaluations.append(ev)

    # Downstream copies receive lower ranks and are remediated before their
    # source records. The bounded, deterministic traversal tolerates cycles.
    resource_keys = [(ev.resource_id, ev.system_id) for ev in evaluations]
    dependencies = get_resource_dependencies(db_path, resource_keys, audit_at)
    children: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for downstream, upstream in dependencies:
        children.setdefault(upstream, []).append(downstream)
    ranks: dict[tuple[str, str], int] = {}

    def rank_for(key: tuple[str, str], path: set[tuple[str, str]]) -> int:
        if key in ranks:
            return ranks[key]
        if key in path:
            return 0
        next_path = set(path)
        next_path.add(key)
        rank = max((rank_for(child, next_path) + 1 for child in children.get(key, [])), default=0)
        ranks[key] = rank
        return rank

    for evaluation in evaluations:
        evaluation.dependency_rank = rank_for((evaluation.resource_id, evaluation.system_id), set())

    # Determine overall status
    any_non_compliant = any(not ev.is_compliant for ev in evaluations)
    deletion_status = (
        "COMPLETE"
        if identity_resolved and source_coverage.get("is_complete") and not any_non_compliant
        else "INCOMPLETE"
    )

    return deletion_status, evaluations


if __name__ == "__main__":
    import sys
    db = str(DB_PATH)
    print("Deletion rules:", list(get_deletion_rules(db).keys()))

