"""
resource_discovery.py — Query the SQLite evidence index.

All functions accept an open db_path (string or Path) and return
plain Python dicts/lists — no SQLite objects escape this module.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from code.identity_resolver import parse_iso_timestamp
from code.data_catalog import SPECIAL_CATEGORY_ACTIONS


DB_PATH = Path(__file__).parent.parent / "data" / "resource_index.db"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _norm_email(v: str) -> str:
    return v.strip().lower() if v else ""


def _norm_id(v: str) -> str:
    return v.strip().lower() if v else ""


def _ts_str(ts: str | None) -> str | None:
    """Normalise ISO-8601 timestamp to sortable string; return None if blank."""
    if not ts:
        return None
    return ts.strip().rstrip("Z") + "Z" if not ts.strip().endswith("Z") else ts.strip()


def _before_or_at(ts_col: str | None, cutoff: str | None) -> bool:
    """Return True if ts_col is None or ≤ cutoff (lexicographic ISO-8601 compare)."""
    if not cutoff or not ts_col:
        return True
    return ts_col <= cutoff


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Expand pii_fields_json inline
    pii_json = d.pop("pii_fields_json", None)
    if pii_json:
        try:
            d["pii_fields"] = json.loads(pii_json)
        except Exception:
            d["pii_fields"] = {}
    return d


# ---------------------------------------------------------------------------
# Identifier normalisation helpers
# ---------------------------------------------------------------------------

def _build_norm_set(identifiers: list[str]) -> list[str]:
    """Return normalized text and phone-digit forms without losing raw IDs."""
    norms = []
    for identifier in identifiers:
        if not identifier:
            continue
        value = _norm_email(identifier)
        norms.append(value)
        if re.fullmatch(r"[+\d\s().-]+", value):
            digits = re.sub(r"\D", "", value)
            if len(digits) >= 7:
                norms.append(digits)
                norms.append("+" + digits)
    return list(dict.fromkeys(norms))


def _identifier_valid_at(
    normalized_value: str,
    observed_at: str | None,
    identifier_intervals: dict[str, list[dict]] | None,
) -> bool:
    """Check whether a matched value belonged to the subject at observation time."""
    if identifier_intervals is None:
        return True
    observed = parse_iso_timestamp(observed_at)
    interval_rows = identifier_intervals.get(normalized_value.lower(), [])
    if not interval_rows and re.fullmatch(r"[+\d\s().-]+", normalized_value):
        digits = re.sub(r"\D", "", normalized_value)
        if len(digits) >= 7:
            interval_rows = identifier_intervals.get(digits, [])
    for interval in interval_rows:
        valid_from = parse_iso_timestamp(interval.get("valid_from"))
        valid_to = parse_iso_timestamp(interval.get("valid_to"))
        if observed is None:
            if valid_from is None and valid_to is None:
                return True
            continue
        if (valid_from is None or valid_from <= observed) and (valid_to is None or observed < valid_to):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_resources(
    db_path: str | Path,
    identifiers: list[str],
    system_id: str | None = None,
    audit_at: str | None = None,
    identifier_intervals: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Find all resources linked to any of the given identifiers.

    Args:
        db_path: Path to SQLite index.
        identifiers: List of identifier values (emails, hashes, user_ids, etc.).
        system_id: Optionally filter to a specific system.
        audit_at: ISO-8601 cutoff; only return evidence captured at or before this time.

    Returns:
        List of resource dicts, deduplicated by (resource_id, system_id),
        keeping the most-recent snapshot per pair.
    """
    if not identifiers:
        return []

    norms = _build_norm_set(identifiers)
    placeholders = ",".join("?" * len(norms))

    conn = _connect(db_path)
    try:
        # Find resource+system pairs matching any identifier
        rows = conn.execute(
            f"""SELECT DISTINCT ri.resource_id, ri.system_id, ri.identifier_norm, ri.identifier_type, ri.captured_at
                FROM resource_identifiers ri
                WHERE ri.identifier_norm IN ({placeholders})
                  AND (ri.captured_at IS NULL OR ri.captured_at <= COALESCE(?, ri.captured_at))""",
            [*norms, audit_at],
        ).fetchall()

        pairs = []
        for row in rows:
            if _identifier_valid_at(row["identifier_norm"], row["captured_at"], identifier_intervals):
                pair = (row["resource_id"], row["system_id"])
                if pair not in pairs:
                    pairs.append(pair)

        # Also check activity table for resource mentions
        act_rows = conn.execute(
            f"""SELECT DISTINCT resource_id, subject_ref, event_time,
                       COALESCE(NULLIF(target_system, ''), source_system) AS system_id
                FROM system_activity
                WHERE lower(subject_ref) IN ({placeholders})
                  AND (event_time IS NULL OR event_time <= COALESCE(?, event_time))""",
            [*norms, audit_at],
        ).fetchall()
        for r in act_rows:
            if not _identifier_valid_at((r["subject_ref"] or "").strip().lower(), r["event_time"], identifier_intervals):
                continue
            pair = (r["resource_id"], r["system_id"])
            if pair not in pairs and r["resource_id"]:
                pairs.append(pair)

        if not pairs:
            return []

        # For each pair, get the most-recent resource snapshot at or before audit_at
        results = []
        seen = set()
        for resource_id, sys_id in pairs:
            if sys_id and system_id and sys_id != system_id:
                continue
            key = (resource_id, sys_id)
            if key in seen:
                continue
            seen.add(key)

            # Pick the snapshot closest to (but not after) audit_at
            if audit_at:
                snap = conn.execute(
                    """SELECT * FROM resources
                       WHERE resource_id = ? AND system_id = ?
                         AND (captured_at IS NULL OR captured_at <= ?)
                         ORDER BY captured_at DESC, source_file DESC LIMIT 1""",
                    (resource_id, sys_id, audit_at),
                ).fetchone()
            else:
                snap = conn.execute(
                    """SELECT * FROM resources
                       WHERE resource_id = ? AND system_id = ?
                       ORDER BY captured_at DESC, source_file DESC LIMIT 1""",
                    (resource_id, sys_id),
                ).fetchone()

            if snap:
                d = _row_to_dict(snap)
                # Attach all known identifiers for this resource
                id_rows = conn.execute(
                    """SELECT identifier_type, identifier_value, identifier_norm, captured_at
                       FROM resource_identifiers
                       WHERE resource_id = ? AND system_id = ?
                         AND (captured_at IS NULL OR captured_at <= COALESCE(?, captured_at))""",
                    (resource_id, sys_id, audit_at),
                ).fetchall()
                if identifier_intervals is not None:
                    latest_ids = [
                        item for item in id_rows
                        if item["captured_at"] == d.get("captured_at")
                    ]
                    latest_matches = [
                        item for item in latest_ids
                        if item["identifier_norm"] in norms
                        and _identifier_valid_at(item["identifier_norm"], item["captured_at"], identifier_intervals)
                    ]
                    # A snapshot with identifiers for a different owner may be
                    # a reused resource ID. A snapshot with no identifiers may
                    # instead be the redacted/anonymized state we need to find;
                    # retain that pair because its earlier ownership was valid.
                    if latest_ids and not latest_matches:
                        continue
                d["identifiers"] = [dict(r) for r in id_rows]
                results.append(d)
            else:
                # Resource exists in identifier index but no snapshot row — synthesise minimal entry
                id_rows = conn.execute(
                    """SELECT identifier_type, identifier_value, captured_at
                       FROM resource_identifiers
                       WHERE resource_id = ? AND system_id = ?
                         AND (captured_at IS NULL OR captured_at <= COALESCE(?, captured_at))""",
                    (resource_id, sys_id, audit_at),
                ).fetchall()
                if id_rows:
                    results.append({
                        "resource_id": resource_id,
                        "system_id": sys_id,
                        "data_category": None,
                        "state": None,
                        "pii_fields": {},
                        "captured_at": id_rows[0]["captured_at"],
                        "source_file": "",
                        "evidence_id": "",
                        "identifiers": [dict(r) for r in id_rows],
                    })
        return results
    finally:
        conn.close()


def get_resource_history(
    db_path: str | Path,
    resource_id: str,
    system_id: str,
    audit_at: str | None = None,
) -> list[dict]:
    """Return all snapshots of a resource, ordered by captured_at ascending.

    Args:
        db_path: Path to SQLite index.
        resource_id: The resource_id to look up.
        system_id: The system (e.g. "account_service").
        audit_at: ISO-8601 cutoff; only return snapshots captured at or before.
    """
    conn = _connect(db_path)
    try:
        if audit_at:
            rows = conn.execute(
                """SELECT * FROM resources
                   WHERE resource_id = ? AND system_id = ?
                     AND (captured_at IS NULL OR captured_at <= ?)
                   ORDER BY captured_at ASC, source_file ASC""",
                (resource_id, system_id, audit_at),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM resources
                   WHERE resource_id = ? AND system_id = ?
                   ORDER BY captured_at ASC, source_file ASC""",
                (resource_id, system_id),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_activity_for_resource(
    db_path: str | Path,
    resource_id: str,
    audit_at: str | None = None,
) -> list[dict]:
    """Return all system_activity events mentioning a resource_id."""
    conn = _connect(db_path)
    try:
        if audit_at:
            rows = conn.execute(
                """SELECT * FROM system_activity
                   WHERE resource_id = ?
                     AND (event_time IS NULL OR event_time <= ?)
                   ORDER BY event_time ASC""",
                (resource_id, audit_at),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM system_activity
                   WHERE resource_id = ?
                   ORDER BY event_time ASC""",
                (resource_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_activity_for_user(
    db_path: str | Path,
    identifiers: list[str],
    audit_at: str | None = None,
) -> list[dict]:
    """Return all system_activity events for a user (by any identifier)."""
    if not identifiers:
        return []
    norms = _build_norm_set(identifiers)
    placeholders = ",".join("?" * len(norms))
    conn = _connect(db_path)
    try:
        if audit_at:
            rows = conn.execute(
                f"""SELECT * FROM system_activity
                    WHERE lower(subject_ref) IN ({placeholders})
                      AND (event_time IS NULL OR event_time <= ?)
                    ORDER BY event_time ASC""",
                [*norms, audit_at],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT * FROM system_activity
                    WHERE lower(subject_ref) IN ({placeholders})
                    ORDER BY event_time ASC""",
                norms,
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_deletion_rules(db_path: str | Path) -> dict[str, dict]:
    """Return deletion rules keyed by data_category.

    Returns: {data_category: {rule_id, condition, required_action}}
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM deletion_rules").fetchall()
        result = {}
        for row in rows:
            d = dict(row)
            cat = d.get("data_category", "")
            if cat:
                result[cat] = d
        return result
    finally:
        conn.close()


def get_identity_record(db_path: str | Path, user_id: str) -> dict | None:
    """Return the identity_records row for a user_id."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM identity_records WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_evidence_file(db_path: str | Path, file_id: str) -> dict | None:
    """Return metadata for an evidence file by file_id."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM source_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_source_coverage(db_path: str | Path, audit_at: str | None = None) -> dict:
    """Return indexed-source counts and explicit parsing/coverage gaps."""
    conn = _connect(db_path)
    try:
        file_filter = ""
        params: list[str] = []
        if audit_at:
            file_filter = "WHERE captured_at IS NULL OR captured_at <= ?"
            params.append(audit_at)
        status_rows = conn.execute(
            f"SELECT status, COUNT(*) AS count FROM source_files {file_filter} GROUP BY status",
            params,
        ).fetchall()
        status_counts = {str(row["status"] or "unknown"): int(row["count"]) for row in status_rows}
        source_count = sum(status_counts.values())
        unresolved_statuses = {"pending", "processing", "partial", "failed", "error", "unsupported"}
        gap_sql = "SELECT file_id FROM source_files WHERE status IN ('pending','processing','partial','failed','error','unsupported')"
        if audit_at:
            gap_sql += " AND (captured_at IS NULL OR captured_at <= ?)"
        gaps = [row["file_id"] for row in conn.execute(gap_sql, params).fetchall()]
        error_sql = "SELECT COUNT(*) FROM ingestion_errors e"
        if audit_at:
            error_sql += (
                " LEFT JOIN source_files sf ON sf.file_id = e.source_file "
                "WHERE sf.file_id IS NULL OR sf.captured_at IS NULL OR sf.captured_at <= ?"
            )
        error_count = int(conn.execute(error_sql, params).fetchone()[0])
        category_sql = """
            SELECT DISTINCT COALESCE(r.data_category, '') AS data_category
            FROM resources r
            WHERE NOT EXISTS (
                SELECT 1 FROM deletion_rules d WHERE d.data_category = r.data_category
            )
        """
        category_params: list[str] = []
        if SPECIAL_CATEGORY_ACTIONS:
            special_marks = ",".join("?" for _ in SPECIAL_CATEGORY_ACTIONS)
            category_sql += f" AND r.data_category NOT IN ({special_marks})"
            category_params.extend(SPECIAL_CATEGORY_ACTIONS)
        if audit_at:
            category_sql += " AND (r.captured_at IS NULL OR r.captured_at <= ?)"
            category_params.append(audit_at)
        unclassified_categories = sorted(
            row["data_category"] or "<blank>"
            for row in conn.execute(category_sql, category_params).fetchall()
        )
        category_gaps = [f"unclassified category: {category}" for category in unclassified_categories]
        gaps.extend(category_gaps)
        return {
            "resources":    conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
            "resource_links": conn.execute("SELECT COUNT(*) FROM resource_links").fetchone()[0],
            "identifiers":  conn.execute("SELECT COUNT(*) FROM resource_identifiers").fetchone()[0],
            "activity":     conn.execute("SELECT COUNT(*) FROM system_activity").fetchone()[0],
            "source_files": source_count,
            "files_by_status": status_counts,
            "coverage_gaps": gaps,
            "unclassified_categories": unclassified_categories,
            "ingestion_errors": error_count,
            "is_complete": (
                source_count > 0
                and not gaps
                and error_count == 0
                and set(status_counts).issubset({"ok"})
                and not unresolved_statuses.intersection(status_counts)
            ),
        }
    finally:
        conn.close()


def find_resources_by_user_id(
    db_path: str | Path,
    user_id: str,
    audit_at: str | None = None,
) -> list[dict]:
    """Find resources linked to a stable user_id / customer_reference."""
    conn = _connect(db_path)
    try:
        norm = user_id.strip().lower()
        if audit_at:
            rows = conn.execute(
                """SELECT DISTINCT ri.resource_id, ri.system_id
                   FROM resource_identifiers ri
                   WHERE ri.identifier_norm = ?
                     AND ri.identifier_type IN ('customer_reference', 'user_id')
                     AND (ri.captured_at IS NULL OR ri.captured_at <= ?)""",
                (norm, audit_at),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT DISTINCT ri.resource_id, ri.system_id
                   FROM resource_identifiers ri
                   WHERE ri.identifier_norm = ?
                     AND ri.identifier_type IN ('customer_reference', 'user_id')""",
                (norm,),
            ).fetchall()
        pairs = [(r["resource_id"], r["system_id"]) for r in rows]

        results = []
        for resource_id, sys_id in pairs:
            if audit_at:
                snap = conn.execute(
                    """SELECT * FROM resources
                       WHERE resource_id = ? AND system_id = ?
                         AND (captured_at IS NULL OR captured_at <= ?)
                       ORDER BY captured_at DESC LIMIT 1""",
                    (resource_id, sys_id, audit_at),
                ).fetchone()
            else:
                snap = conn.execute(
                    """SELECT * FROM resources
                       WHERE resource_id = ? AND system_id = ?
                       ORDER BY captured_at DESC LIMIT 1""",
                    (resource_id, sys_id),
                ).fetchone()
            if snap:
                results.append(_row_to_dict(snap))
        return results
    finally:
        conn.close()


def get_related_resources(
    db_path: str | Path,
    resource_keys: list[tuple[str, str]],
    audit_at: str | None = None,
) -> list[dict]:
    """Follow explicit resource-to-resource links found in source records."""
    resource_ids = list(dict.fromkeys(resource_id for resource_id, _ in resource_keys if resource_id))
    if not resource_ids:
        return []
    placeholders = ",".join("?" * len(resource_ids))
    conn = _connect(db_path)
    found: dict[tuple[str, str], dict] = {}
    try:
        time_filter = ""
        params: list[str] = [*resource_ids, *resource_ids]
        if audit_at:
            time_filter = " AND (captured_at IS NULL OR captured_at <= ?)"
            params.append(audit_at)
        rows = conn.execute(
            f"""SELECT * FROM resource_links
                WHERE (source_resource_id IN ({placeholders}) OR target_resource_id IN ({placeholders}))
                {time_filter}
                ORDER BY captured_at, id""",
            params,
        ).fetchall()

        for link in rows:
            if link["source_resource_id"] in resource_ids:
                related_id, related_system = link["target_resource_id"], link["target_system_id"]
            else:
                related_id, related_system = link["source_resource_id"], link["source_system_id"]
            if related_system and related_system != "unknown":
                systems = [related_system]
            else:
                systems = [
                    row[0] for row in conn.execute(
                        "SELECT DISTINCT system_id FROM resources WHERE resource_id=? ORDER BY system_id",
                        (related_id,),
                    ).fetchall()
                ]
            for system_id in systems:
                if (related_id, system_id) in resource_keys:
                    continue
                if audit_at:
                    snapshot = conn.execute(
                        """SELECT * FROM resources WHERE resource_id=? AND system_id=?
                           AND (captured_at IS NULL OR captured_at <= ?)
                           ORDER BY captured_at DESC, source_file DESC LIMIT 1""",
                        (related_id, system_id, audit_at),
                    ).fetchone()
                else:
                    snapshot = conn.execute(
                        """SELECT * FROM resources WHERE resource_id=? AND system_id=?
                           ORDER BY captured_at DESC, source_file DESC LIMIT 1""",
                        (related_id, system_id),
                    ).fetchone()
                if snapshot:
                    item = _row_to_dict(snapshot)
                    item["link_evidence_id"] = link["evidence_id"] or ""
                    found[(related_id, system_id)] = item
        return list(found.values())
    except sqlite3.OperationalError:
        # Older indexes can be used safely; they simply have no link table yet.
        return []
    finally:
        conn.close()


def get_resource_dependencies(
    db_path: str | Path,
    resource_keys: list[tuple[str, str]],
    audit_at: str | None = None,
) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """Return (downstream copy, upstream source) pairs inside this portfolio."""
    keys = set(resource_keys)
    ids = list(dict.fromkeys(resource_id for resource_id, _ in keys if resource_id))
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    conn = _connect(db_path)
    try:
        sql = (
            f"SELECT source_resource_id, source_system_id, target_resource_id, target_system_id "
            f"FROM resource_links WHERE source_resource_id IN ({placeholders}) "
            f"AND target_resource_id IN ({placeholders})"
        )
        params: list[str] = [*ids, *ids]
        if audit_at:
            sql += " AND (captured_at IS NULL OR captured_at <= ?)"
            params.append(audit_at)
        result = []
        for row in conn.execute(sql, params).fetchall():
            downstream_candidates = [
                key for key in keys if key[0] == row["source_resource_id"]
                and (not row["source_system_id"] or row["source_system_id"] == key[1])
            ]
            upstream_candidates = [
                key for key in keys if key[0] == row["target_resource_id"]
                and (not row["target_system_id"] or row["target_system_id"] == "unknown" or row["target_system_id"] == key[1])
            ]
            result.extend((downstream, upstream) for downstream in downstream_candidates for upstream in upstream_candidates)
        return result
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def get_backup_resources(
    db_path: str | Path,
    identifiers: list[str],
    audit_at: str | None = None,
) -> list[dict]:
    """Specifically fetch backup_service resources for a user."""
    return [
        r for r in search_resources(db_path, identifiers, system_id="backup_service", audit_at=audit_at)
    ]


def get_connector_resources(
    db_path: str | Path,
    identifiers: list[str],
    audit_at: str | None = None,
) -> list[dict]:
    """Specifically fetch integration_service (data flow) resources for a user."""
    return [
        r for r in search_resources(db_path, identifiers, system_id="integration_service", audit_at=audit_at)
    ]


def search_activity_by_event_type(
    db_path: str | Path,
    identifiers: list[str],
    event_types: list[str],
    audit_at: str | None = None,
) -> list[dict]:
    """Return system_activity rows of specific event types for a user."""
    if not identifiers or not event_types:
        return []
    norms = _build_norm_set(identifiers)
    id_ph = ",".join("?" * len(norms))
    et_ph = ",".join("?" * len(event_types))

    conn = _connect(db_path)
    try:
        if audit_at:
            rows = conn.execute(
                f"""SELECT * FROM system_activity
                    WHERE subject_ref IN ({id_ph})
                      AND upper(event_type) IN ({et_ph})
                      AND (event_time IS NULL OR event_time <= ?)
                    ORDER BY event_time ASC""",
                [*norms, *event_types, audit_at],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT * FROM system_activity
                    WHERE lower(subject_ref) IN ({id_ph})
                      AND upper(event_type) IN ({et_ph})
                    ORDER BY event_time ASC""",
                [*norms, *event_types],
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else str(DB_PATH)
    print("Coverage:", get_source_coverage(db))
    print("Sample rules:", list(get_deletion_rules(db).keys()))

