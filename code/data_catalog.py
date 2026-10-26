"""Canonical evidence categories and source-name aliases.

Evidence arrives with different container/table names. This module resolves
those names to the categories declared by ``deletion_rules.csv`` so ingestion
and policy evaluation use one shared vocabulary.
"""

from __future__ import annotations

from typing import Any


CATEGORY_TO_SYSTEM = {
    "USER_PROFILE": "account_service",
    "SECONDARY_IDENTIFIER": "identity_service",
    "SEARCH_INDEX_ENTRY": "search_service",
    "CACHE_ENTRY": "cache_service",
    "SUPPORT_MESSAGE": "support_platform",
    "SUPPORT_ATTACHMENT": "support_platform",
    "ANALYTICS_PROFILE": "analytics_platform",
    "PRODUCT_TELEMETRY": "analytics_platform",
    "USER_EXPORT": "export_service",
    "MARKETING_PROFILE": "marketing_service",
    "EMAIL_DELIVERY_LOG": "notification_service",
    "SECURITY_AUDIT_LOG": "security_service",
    "LEGAL_HOLD_RECORD": "legal_service",
    "BACKUP_ARCHIVE": "backup_service",
    "DATA_FLOW": "integration_service",
    "ACCOUNT_TOMBSTONE": "identity_service",
}

CANONICAL_CATEGORIES = frozenset(CATEGORY_TO_SYSTEM)

# Processes have dedicated operation/state rules in the challenge contract
# rather than a row in deletion_rules.csv.
SPECIAL_CATEGORY_ACTIONS = {"DATA_FLOW": "DISABLE"}

SYSTEM_ALIASES = {
    "analytics_identity_store": "analytics_platform",
    "backup_restore_service": "backup_service",
    "support_storage_service": "support_platform",
    "support_replica": "support_platform",
    "user_export_store": "export_service",
}

SYSTEM_DEFAULT_CATEGORY = {
    "account_service": "USER_PROFILE",
    "analytics_platform": "ANALYTICS_PROFILE",
    "backup_service": "BACKUP_ARCHIVE",
    "export_service": "USER_EXPORT",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "integration_service": "DATA_FLOW",
    "legal_service": "LEGAL_HOLD_RECORD",
    "marketing_service": "MARKETING_PROFILE",
    "notification_service": "EMAIL_DELIVERY_LOG",
    "search_service": "SEARCH_INDEX_ENTRY",
    "security_service": "SECURITY_AUDIT_LOG",
    "support_platform": "SUPPORT_MESSAGE",
    "cache_service": "CACHE_ENTRY",
}

# Unambiguous names shared by SQL tables and JSON collections.
SYSTEM_TABLE_CATEGORY = {
    "customer_profiles": ("account_service", "USER_PROFILE"),
    "customer_search_documents": ("search_service", "SEARCH_INDEX_ENTRY"),
    "search_documents": ("search_service", "SEARCH_INDEX_ENTRY"),
    "customer_analytics_profiles": ("analytics_platform", "ANALYTICS_PROFILE"),
    "customer_analytics_reports": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support_records": ("support_platform", "SUPPORT_MESSAGE"),
    "support_conversations": ("support_platform", "SUPPORT_MESSAGE"),
    "ticket_attachments": ("support_platform", "SUPPORT_ATTACHMENT"),
    "customer_exports": ("export_service", "USER_EXPORT"),
    "customer_export_jobs": ("export_service", "USER_EXPORT"),
    "connector_subscriptions": ("integration_service", "DATA_FLOW"),
    "connector_configurations": ("integration_service", "DATA_FLOW"),
    "backup_restore_entries": ("backup_service", "BACKUP_ARCHIVE"),
    "backup_catalog": ("backup_service", "BACKUP_ARCHIVE"),
    "security_audit_records": ("security_service", "SECURITY_AUDIT_LOG"),
    "security_audit_events": ("security_service", "SECURITY_AUDIT_LOG"),
    "retained_order_records": ("legal_service", "LEGAL_HOLD_RECORD"),
    "deleted_customer_references": ("identity_service", "ACCOUNT_TOMBSTONE"),
}

# Service exports reuse short names such as "profiles" and "exports"; the
# owning service is part of the key so those names cannot collide.
SERVICE_TABLE_CATEGORY = {
    ("account_service", "profile_lookups"): "USER_PROFILE",
    ("analytics_platform", "profiles"): "ANALYTICS_PROFILE",
    ("backup_service", "restore_entries"): "BACKUP_ARCHIVE",
    ("export_service", "exports"): "USER_EXPORT",
    ("integration_service", "connectors"): "DATA_FLOW",
    ("search_service", "index_queries"): "SEARCH_INDEX_ENTRY",
    ("security_service", "audit_records"): "SECURITY_AUDIT_LOG",
    ("support_platform", "tickets"): "SUPPORT_MESSAGE",
}


def normalize_system_id(system_id: str | None) -> str:
    """Normalize known service aliases while preserving unknown source labels."""
    value = str(system_id or "").strip().lower()
    return SYSTEM_ALIASES.get(value, value)


def canonical_category(value: Any) -> str:
    """Return a supported policy category, or an empty string if unrecognized."""
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return normalized if normalized in CANONICAL_CATEGORIES else ""


def _base_container_name(value: str) -> str:
    """Strip common physical-snapshot suffixes before resolving a SQL table."""
    name = value
    suffixes = (
        "_failover_replica", "_reporting_replica", "_read_replica",
        "_regional_replica", "_replica", "_primary",
    )
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                changed = True
                break
    return name


def _category_from_resource_id(resource_id: str | None) -> str:
    rid = str(resource_id or "").strip().lower()
    prefix_categories = (
        ("account_tombstone", "ACCOUNT_TOMBSTONE"),
        ("support_attachment", "SUPPORT_ATTACHMENT"),
        ("secondary_identifier", "SECONDARY_IDENTIFIER"),
        ("legal_hold", "LEGAL_HOLD_RECORD"),
        ("audit_record", "SECURITY_AUDIT_LOG"),
        ("analytics", "ANALYTICS_PROFILE"),
        ("telemetry", "PRODUCT_TELEMETRY"),
        ("marketing", "MARKETING_PROFILE"),
        ("search", "SEARCH_INDEX_ENTRY"),
        ("support", "SUPPORT_MESSAGE"),
        ("account", "USER_PROFILE"),
        ("export", "USER_EXPORT"),
        ("backup", "BACKUP_ARCHIVE"),
        ("data_flow", "DATA_FLOW"),
        ("cache", "CACHE_ENTRY"),
        ("email_delivery", "EMAIL_DELIVERY_LOG"),
    )
    return next((category for prefix, category in prefix_categories if rid.startswith(prefix)), "")


def resolve_source_scope(
    system_id: str | None,
    container_name: str | None = None,
    record: dict[str, Any] | None = None,
    resource_id: str | None = None,
) -> tuple[str, str]:
    """Return ``(normalized_system_id, canonical_category)`` for one record.

    Explicit per-record type information wins, followed by a system-aware
    collection/table alias, a resource-id prefix, and finally a system default.
    Unknown categories remain empty so callers can record a visible ingestion
    gap instead of silently inventing a policy action.
    """
    rec = record or {}
    record_system = rec.get("system_id") or rec.get("system") or system_id
    normalized_system = normalize_system_id(record_system)
    container = str(container_name or rec.get("collection") or "").strip().lower()
    base_container = _base_container_name(container)

    category = canonical_category(rec.get("data_category")) or canonical_category(rec.get("record_type"))
    if not category:
        mapped = SERVICE_TABLE_CATEGORY.get((normalized_system, container))
        if mapped:
            category = mapped
    if not category:
        mapped = SYSTEM_TABLE_CATEGORY.get(container) or SYSTEM_TABLE_CATEGORY.get(base_container)
        if mapped:
            mapped_system, mapped_category = mapped
            category = mapped_category
            if not normalized_system or normalized_system in {"unknown", "backup_restore_service", "support_storage_service"}:
                normalized_system = mapped_system
    if not category:
        category = _category_from_resource_id(resource_id or rec.get("resource_id"))
    if not category and normalized_system and not container:
        category = SYSTEM_DEFAULT_CATEGORY.get(normalized_system, "")

    # System aliases for evidence stores that wrap records are replaced by the
    # service named by the explicit record category when no real service was
    # supplied on the record.
    if normalized_system in {"unknown", "backup_restore_service", "support_storage_service", "privacy_treatment_service"}:
        normalized_system = CATEGORY_TO_SYSTEM.get(category, normalized_system)
    if not normalized_system and category:
        normalized_system = CATEGORY_TO_SYSTEM.get(category, "")
    return normalized_system, category
