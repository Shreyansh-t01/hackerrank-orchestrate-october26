"""
ingestion.py — Build and populate the SQLite evidence index.

Run once (offline) before processing deletion requests:
    python -m code.ingestion

Idempotent: re-running drops and recreates all tables.
"""

import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from code.data_catalog import (
    CATEGORY_TO_SYSTEM,
    SYSTEM_TABLE_CATEGORY,
    canonical_category,
    normalize_system_id,
    resolve_source_scope,
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
DATASET_DIR = REPO_ROOT / "dataset"
EVIDENCE_DIR = DATASET_DIR / "evidence"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "resource_index.db"
PARSER_VERSION = "5"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS source_files (
    file_id       TEXT PRIMARY KEY,
    file_path     TEXT NOT NULL,
    file_type     TEXT NOT NULL,    -- sql|json|log|txt|csv|png|pdf
    origin        TEXT,
    captured_at   TEXT,
    ingested_at   TEXT NOT NULL,
    record_count  INTEGER DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    sha256        TEXT
);

CREATE TABLE IF NOT EXISTS identity_records (
    user_id       TEXT PRIMARY KEY,
    full_name     TEXT,
    email         TEXT,
    email_norm    TEXT,
    phone         TEXT,
    email_hash    TEXT,
    updated_at    TEXT,
    source_file   TEXT
);

CREATE TABLE IF NOT EXISTS identity_history (
    audit_id      TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    event_time    TEXT NOT NULL,
    field_name    TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    source_file   TEXT
);

CREATE TABLE IF NOT EXISTS deletion_rules (
    rule_id         TEXT PRIMARY KEY,
    data_category   TEXT NOT NULL,
    condition       TEXT,
    required_action TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_activity (
    activity_id     TEXT PRIMARY KEY,
    event_time      TEXT,
    event_type      TEXT,
    source_system   TEXT,
    target_system   TEXT,
    resource_id     TEXT,
    subject_ref     TEXT,
    reported_result TEXT,
    details_json    TEXT,
    source_file     TEXT
);

-- Normalized resource records across all evidence sources
CREATE TABLE IF NOT EXISTS resources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id     TEXT NOT NULL,
    system_id       TEXT NOT NULL,
    data_category   TEXT,
    state           TEXT,           -- PRESENT|ABSENT|REDACTED|ANONYMIZED|ACTIVE|DISABLED|BLOCKED|FOUND|NOT_FOUND|IDENTIFIABLE|ANONYMIZED
    pii_fields_json TEXT,           -- JSON object of identifier fields present
    captured_at     TEXT,
    source_file     TEXT,
    evidence_id     TEXT,
    source_filename TEXT,
    page_number     INTEGER,
    source_scope    TEXT NOT NULL DEFAULT '',
    UNIQUE(resource_id, system_id, captured_at, source_file, source_scope)
);

-- Maps resource IDs to user identifiers (for lookup)
CREATE TABLE IF NOT EXISTS resource_identifiers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id     TEXT NOT NULL,
    system_id       TEXT NOT NULL,
    identifier_type TEXT NOT NULL,  -- email|email_norm|email_hash|phone|user_id|customer_reference
    identifier_value TEXT NOT NULL,
    identifier_norm  TEXT NOT NULL,
    captured_at     TEXT,
    source_file     TEXT,
    evidence_id     TEXT
);

CREATE TABLE IF NOT EXISTS resource_links (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_resource_id   TEXT NOT NULL,
    source_system_id     TEXT,
    target_resource_id   TEXT NOT NULL,
    target_system_id     TEXT,
    relationship         TEXT NOT NULL,
    captured_at          TEXT,
    source_file          TEXT,
    evidence_id          TEXT
);

CREATE TABLE IF NOT EXISTS ingestion_errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file   TEXT,
    record_ref    TEXT,
    error_message TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_metadata (
    metadata_key TEXT PRIMARY KEY,
    metadata_value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ri_norm   ON resource_identifiers(identifier_norm);
CREATE INDEX IF NOT EXISTS idx_ri_type   ON resource_identifiers(identifier_type, identifier_norm);
CREATE INDEX IF NOT EXISTS idx_rl_source ON resource_links(source_resource_id, source_system_id);
CREATE INDEX IF NOT EXISTS idx_rl_target ON resource_links(target_resource_id, target_system_id);
CREATE INDEX IF NOT EXISTS idx_res_rid   ON resources(resource_id);
CREATE INDEX IF NOT EXISTS idx_res_sys   ON resources(system_id);
CREATE INDEX IF NOT EXISTS idx_sa_res    ON system_activity(resource_id);
CREATE INDEX IF NOT EXISTS idx_sa_subj   ON system_activity(subject_ref);
CREATE INDEX IF NOT EXISTS idx_sa_time   ON system_activity(event_time);
CREATE INDEX IF NOT EXISTS idx_ih_uid    ON identity_history(user_id);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_email(email: str | None) -> str:
    if not email:
        return ""
    return email.strip().lower()


def _norm_phone(phone: str | None) -> str:
    if not phone:
        return ""
    return re.sub(r"[^+\d]", "", phone.strip())


def _norm_value(val: str | None) -> str:
    if not val:
        return ""
    return val.strip().lower()


def _log_error(conn, source_file: str, record_ref: str, msg: str):
    conn.execute(
        "INSERT OR IGNORE INTO ingestion_errors(source_file, record_ref, error_message, created_at) VALUES(?,?,?,?)",
        (source_file, record_ref, msg, _now()),
    )
    from code.run_logging import record_event
    record_event("ingestion_error", file_id=source_file, error_type="source_parse")


def _evidence_id_for_file(file_path: str, evidence_manifest: dict) -> str:
    """Look up file_id from evidence_files.csv manifest by path suffix."""
    normalized = file_path.replace("\\", "/").lower()
    for eid, ep in evidence_manifest.items():
        if normalized.endswith(ep.replace("\\", "/").lower()):
            return eid
    return ""


def _insert_resource(conn, resource_id, system_id, data_category, state, pii_fields,
                     captured_at, source_file, evidence_id, source_scope="", page_number=None,
                     source_filename=None):
    pii_json = json.dumps(pii_fields, ensure_ascii=False) if pii_fields else None
    conn.execute(
        """INSERT OR IGNORE INTO resources
           (resource_id, system_id, data_category, state, pii_fields_json, captured_at, source_file, evidence_id, source_filename, page_number, source_scope)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (resource_id, system_id, data_category, state, pii_json, captured_at, source_file, evidence_id, source_filename, page_number, source_scope),
    )


def _insert_identifier(conn, resource_id, system_id, id_type, id_value, captured_at, source_file, evidence_id):
    if not id_value:
        return
    if "email" in id_type:
        norm = _norm_email(id_value)
    elif id_type == "phone":
        norm = _norm_phone(id_value)
    else:
        norm = _norm_value(id_value)
    if not norm:
        return
    conn.execute(
        """INSERT OR IGNORE INTO resource_identifiers
           (resource_id, system_id, identifier_type, identifier_value, identifier_norm, captured_at, source_file, evidence_id)
           VALUES(?,?,?,?,?,?,?,?)""",
        (resource_id, system_id, id_type, id_value, norm, captured_at, source_file, evidence_id),
    )


def _insert_resource_link(
    conn, source_resource_id, source_system_id, target_resource_id,
    target_system_id, relationship, captured_at, source_file, evidence_id,
):
    """Store an explicit source-to-resource reference found in the source record."""
    if not source_resource_id or not target_resource_id or source_resource_id == target_resource_id:
        return
    conn.execute(
        """INSERT INTO resource_links
           (source_resource_id, source_system_id, target_resource_id, target_system_id,
            relationship, captured_at, source_file, evidence_id)
           SELECT ?,?,?,?,?,?,?,?
           WHERE NOT EXISTS (
               SELECT 1 FROM resource_links WHERE source_resource_id=? AND source_system_id=?
                 AND target_resource_id=? AND COALESCE(target_system_id,'')=COALESCE(?,'')
                 AND relationship=? AND captured_at=? AND source_file=?
           )""",
        (source_resource_id, source_system_id, target_resource_id, target_system_id,
         relationship, captured_at, source_file, evidence_id,
         source_resource_id, source_system_id, target_resource_id, target_system_id,
         relationship, captured_at, source_file),
    )


def _update_source_file_count(conn, file_id: str, count: int):
    conn.execute(
        "UPDATE source_files SET record_count = record_count + ? WHERE file_id = ?",
        (count, file_id),
    )


def _ensure_schema_compatibility(conn):
    """Add columns introduced after an older local index was created."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(source_files)")}
    if "sha256" not in columns:
        conn.execute("ALTER TABLE source_files ADD COLUMN sha256 TEXT")
    resource_columns = {row[1] for row in conn.execute("PRAGMA table_info(resources)")}
    if "source_filename" not in resource_columns:
        conn.execute("ALTER TABLE resources ADD COLUMN source_filename TEXT")
    if "page_number" not in resource_columns:
        conn.execute("ALTER TABLE resources ADD COLUMN page_number INTEGER")


def _source_digest(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ingest_cached_file(conn, file_id: str, file_path: Path, parser, *parser_args):
    """Parse one evidence file once per content version and retain failures."""
    digest = _source_digest(file_path)
    existing = conn.execute(
        "SELECT status, sha256 FROM source_files WHERE file_id = ?", (file_id,)
    ).fetchone()
    if existing and existing["status"] == "ok" and existing["sha256"] == digest:
        from code.run_logging import record_event
        record_event("evidence_cached", file_id=file_id, file_type=file_path.suffix.lstrip("."))
        return None
    if not existing:
        conn.execute(
            """INSERT INTO source_files
               (file_id, file_path, file_type, origin, captured_at, ingested_at, record_count, status, sha256)
               VALUES(?,?,?,?,NULL,?,0,'pending',?)""",
            (file_id, str(file_path), file_path.suffix.lstrip(".").lower() or "unknown",
             str(file_path.parent), _now(), digest),
        )

    # A changed source is re-indexed cleanly instead of accumulating stale rows.
    conn.execute("DELETE FROM resource_identifiers WHERE source_file = ?", (file_id,))
    conn.execute("DELETE FROM resources WHERE source_file = ?", (file_id,))
    conn.execute("DELETE FROM resource_links WHERE source_file = ?", (file_id,))
    conn.execute("DELETE FROM system_activity WHERE source_file = ?", (file_id,))
    conn.execute("DELETE FROM ingestion_errors WHERE source_file = ?", (file_id,))
    conn.execute(
        "UPDATE source_files SET status='processing', sha256=?, record_count=0 WHERE file_id=?",
        (digest, file_id),
    )
    errors_before = conn.execute(
        "SELECT COUNT(*) FROM ingestion_errors WHERE source_file = ?", (file_id,)
    ).fetchone()[0]

    try:
        count = parser(conn, file_path, file_id, *parser_args)
    except Exception as exc:
        _log_error(conn, file_id, file_path.name, f"Parser error: {type(exc).__name__}")
        count = 0

    errors_after = conn.execute(
        "SELECT COUNT(*) FROM ingestion_errors WHERE source_file = ?", (file_id,)
    ).fetchone()[0]
    status = "partial" if errors_after > errors_before else "ok"
    conn.execute(
        "UPDATE source_files SET status=?, record_count=?, ingested_at=?, sha256=? WHERE file_id=?",
        (status, count, _now(), digest, file_id),
    )
    conn.commit()
    from code.run_logging import record_event
    record_event(
        "evidence_ingested", file_id=file_id, file_type=file_path.suffix.lstrip("."),
        record_count=count, status=status,
    )
    return count


# ---------------------------------------------------------------------------
# System ID → data_category mapping (from deletion_rules + SQL schema)
# ---------------------------------------------------------------------------
RECORD_TYPE_TO_SYSTEM = CATEGORY_TO_SYSTEM


# ---------------------------------------------------------------------------
# Ingestion: deletion_rules.csv
# ---------------------------------------------------------------------------
def _ingest_deletion_rules(conn, file_path: Path):
    count = 0
    with open(file_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rule_id   = row.get("rule_id", "").strip()
            category  = row.get("data_category", "").strip()
            condition = row.get("condition", "").strip()
            action    = row.get("required_action", "").strip()
            if not rule_id:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO deletion_rules(rule_id, data_category, condition, required_action) VALUES(?,?,?,?)",
                (rule_id, category, condition, action),
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# Ingestion: user_identities.csv
# ---------------------------------------------------------------------------
def _ingest_user_identities(conn, file_path: Path):
    count = 0
    with open(file_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            uid  = row.get("user_id", "").strip()
            if not uid:
                continue
            email      = row.get("email", "").strip()
            email_norm = _norm_email(email)
            conn.execute(
                """INSERT OR REPLACE INTO identity_records
                   (user_id, full_name, email, email_norm, phone, email_hash, updated_at, source_file)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (uid,
                 (row.get("name") or row.get("full_name", "")).strip() or None,
                 email or None,
                 email_norm or None,
                 row.get("phone", "").strip() or None,
                 row.get("email_hash", "").strip() or None,
                 row.get("updated_at", "").strip() or None,
                 str(file_path.name)),
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# Ingestion: user_audit_log.csv
# ---------------------------------------------------------------------------
def _ingest_user_audit_log(conn, file_path: Path):
    count = 0
    with open(file_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            aid = row.get("audit_id", "").strip()
            if not aid:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO identity_history
                   (audit_id, user_id, event_time, field_name, old_value, new_value, source_file)
                   VALUES(?,?,?,?,?,?,?)""",
                (aid,
                 row.get("user_id", "").strip(),
                 (row.get("event_time") or row.get("changed_at", "")).strip(),
                 row.get("field_name", "").strip(),
                 row.get("old_value", "").strip() or None,
                 row.get("new_value", "").strip() or None,
                 str(file_path.name)),
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# Ingestion: system_activity.csv
# ---------------------------------------------------------------------------
def _ingest_system_activity(conn, file_path: Path):
    count = 0
    with open(file_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            aid = row.get("activity_id", "").strip()
            if not aid:
                continue
            details = {}
            for k in ("event_type", "source_system", "target_system", "resource_id",
                      "subject_reference", "reported_result"):
                v = row.get(k, "").strip()
                if v:
                    details[k] = v
            # Additional columns go into details_json
            extra = {k: v for k, v in row.items()
                     if k not in ("activity_id", "event_time", "event_type",
                                  "source_system", "target_system", "resource_id",
                                  "subject_reference", "reported_result") and v.strip()}
            conn.execute(
                """INSERT OR IGNORE INTO system_activity
                   (activity_id, event_time, event_type, source_system, target_system,
                    resource_id, subject_ref, reported_result, details_json, source_file)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (aid,
                 row.get("event_time", "").strip() or None,
                 row.get("event_type", "").strip() or None,
                 row.get("source_system", "").strip() or None,
                 row.get("target_system", "").strip() or None,
                 row.get("resource_id", "").strip() or None,
                 row.get("subject_reference", "").strip() or None,
                 row.get("reported_result", "").strip() or None,
                 json.dumps(extra) if extra else None,
                 str(file_path.name)),
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# Ingestion: evidence_files.csv  (returns manifest dict: file_id → path)
# ---------------------------------------------------------------------------
def _load_evidence_manifest(conn, file_path: Path) -> dict:
    manifest = {}
    with open(file_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            fid = row.get("file_id", "").strip()
            fp  = row.get("file_path", "").strip()
            if fid and fp:
                manifest[fid] = fp
                conn.execute(
                    """INSERT OR IGNORE INTO source_files
                       (file_id, file_path, file_type, origin, captured_at, ingested_at, record_count, status)
                       VALUES(?,?,?,?,?,?,0,'pending')""",
                    (fid, fp,
                     Path(fp).suffix.lstrip(".") or "bin",
                     row.get("origin", "").strip(),
                     row.get("captured_at", "").strip() or None,
                     _now()),
                )
                conn.execute(
                    """UPDATE source_files SET file_path=?, file_type=?, origin=?, captured_at=?
                       WHERE file_id=?""",
                    (fp,
                     Path(fp).suffix.lstrip(".") or "bin",
                     row.get("origin", "").strip(),
                     row.get("captured_at", "").strip() or None,
                     fid),
                )
    return manifest


# ---------------------------------------------------------------------------
# Ingestion: SQL dump files
# ---------------------------------------------------------------------------
def _state_from_sql_row(table: str, row: dict) -> str:
    if table == "customer_profiles":
        status = row.get("account_status", "").upper()
        if status == "DELETED":
            return "ABSENT"
        if status in ("DELETION_PENDING", "ACTIVE"):
            return "PRESENT"
        if "REDACT" in status:
            return "REDACTED"
        return "PRESENT"
    if table == "customer_search_documents":
        return "PRESENT"
    if table == "customer_analytics_profiles":
        identifiers = ("customer_reference", "email", "phone", "email_hash", "user_id")
        return "PRESENT" if any(row.get(field) for field in identifiers) else "ANONYMIZED"
    if table == "support_records":
        identifiers = ("requester_name", "requester_email", "requester_phone", "owner_email", "owner_phone")
        excerpt = str(row.get("message_excerpt", ""))
        contains_direct_id = bool(
            re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", excerpt)
            or re.search(r"\+?\d[\d\s().-]{7,}\d", excerpt)
        )
        return "PRESENT" if contains_direct_id or any(row.get(field) for field in identifiers) else "REDACTED"
    if table == "customer_exports":
        return "PRESENT"
    if table == "connector_subscriptions":
        enabled = str(row.get("enabled", "0"))
        return "ACTIVE" if enabled == "1" else "DISABLED"
    if table == "backup_restore_entries":
        allowed = str(row.get("restore_allowed", "0"))
        return "ACTIVE" if allowed == "1" else "BLOCKED"
    if table == "security_audit_records":
        return "PRESENT"
    return "PRESENT"


def _record_category_gap(conn, file_id: str, record_index: int, system_id: str, container: str) -> None:
    """Keep unmapped records visible to source coverage checks."""
    _log_error(
        conn,
        file_id,
        str(record_index),
        f"Unmapped evidence category for system={system_id!r}, container={container!r}",
    )


def _ingest_sql_file(conn, sql_path: Path, file_id: str, captured_at: str, evidence_manifest: dict):
    """Execute SQL dump into temp in-memory DB, then extract rows into index.

    Handles three SQL schemas:
    1. Standard multi-table (customer_data_inventory_*.sql)
    2. customer_data_primary with embedded record_json (read_replica, regional_replicas)
    3. customer_profiles_primary / _failover_replica tables (failover_replica)
    """
    sql_text = sql_path.read_text(encoding="utf-8", errors="replace")

    # Parse snapshot time from comment
    m_cap = re.search(r"Captured at:\s*([^\s\r\n]+)", sql_text)
    m_snap = re.search(r"snapshot_completed_at=([^\s\r\n]+)", sql_text)
    snap_at = (m_cap.group(1) if m_cap else None) or (m_snap.group(1) if m_snap else None) or captured_at

    tmp = sqlite3.connect(":memory:")
    tmp.row_factory = sqlite3.Row
    try:
        # Strip INDEX creation to avoid conflicts
        cleaned = re.sub(r"CREATE\s+INDEX[^;]+;", "", sql_text, flags=re.IGNORECASE)
        tmp.executescript(cleaned)
    except Exception as e:
        _log_error(conn, file_id, str(sql_path.name), f"SQL parse error: {e}")
        tmp.close()
        return 0

    count = 0

    # ── Schema 1: Standard named tables (inventory files)
    for table, (system_id, data_category) in SYSTEM_TABLE_CATEGORY.items():
        try:
            rows = tmp.execute(f"SELECT * FROM {table}").fetchall()
        except Exception:
            continue
        for row in rows:
            r = dict(row)
            rid = r.get("resource_id", "")
            if not rid:
                continue
            state = _state_from_sql_row(table, r)
            pii = {col: r[col] for col in (
                "full_name", "email", "phone", "email_hash", "customer_reference",
                "requester_name", "requester_email", "requester_phone",
                "owner_email", "owner_phone", "customer_email", "customer_phone", "subject_reference",
                "retention_until", "retained_until", "retention_reason",
            ) if r.get(col)}
            _insert_resource(conn, rid, system_id, data_category, state, pii,
                             snap_at, file_id, file_id, source_scope=table)
            related_id = r.get("source_resource_id")
            if related_id:
                target_category = _data_category_from_rid(related_id, "")
                target_system = RECORD_TYPE_TO_SYSTEM.get(target_category, "unknown")
                _insert_resource_link(
                    conn, rid, system_id, related_id, target_system,
                    "SOURCE_RESOURCE", snap_at, file_id, file_id,
                )
            for col, id_type in [
                ("email",              "email"),
                ("requester_email",    "email"),
                ("owner_email",        "email"),
                ("customer_email",     "email"),
                ("email_hash",         "email_hash"),
                ("phone",              "phone"),
                ("requester_phone",    "phone"),
                ("owner_phone",        "phone"),
                ("customer_phone",     "phone"),
                ("customer_reference", "customer_reference"),
                ("subject_reference",  "user_id"),
            ]:
                _insert_identifier(conn, rid, system_id, id_type, r.get(col) or "",
                                   snap_at, file_id, file_id)
            count += 1

    # Embedded JSON tables appear in primary, read, regional, reporting, and
    # failover replica snapshots. Inspect their columns instead of assuming a
    # single table name.
    tables_in_db = [
        r["name"] for r in tmp.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    embedded_tables = []
    for table_name in tables_in_db:
        columns = {row[1] for row in tmp.execute(f'PRAGMA table_info("{table_name}")')}
        if {"resource_id", "origin_system", "collection_name", "record_json"}.issubset(columns):
            embedded_tables.append(table_name)

    for table_name in embedded_tables:
        try:
            cdp_rows = tmp.execute(f'SELECT * FROM "{table_name}"').fetchall()
        except Exception as exc:
            _log_error(conn, file_id, table_name, f"SQL table read failed: {type(exc).__name__}")
            continue
        for row_index, row in enumerate(cdp_rows, start=1):
            r = dict(row)
            rid         = r.get("resource_id", "")
            origin_sys  = r.get("origin_system", "unknown")
            coll        = r.get("collection_name", "")
            rec_json_str = r.get("record_json", "{}")
            row_at      = r.get("updated_at") or snap_at

            try:
                rec = json.loads(rec_json_str)
                if not isinstance(rec, dict):
                    raise ValueError("record_json is not an object")
            except Exception:
                _log_error(conn, file_id, f"{table_name}:{row_index}", "Embedded record_json is invalid")
                rec = {}

            # Merge top-level fields + parsed JSON
            merged = {**r, **rec}

            system_id, data_category = resolve_source_scope(origin_sys, coll, merged, rid)
            if not data_category:
                _record_category_gap(conn, file_id, row_index, system_id or origin_sys, coll)
                data_category = "UNKNOWN"
            if not rid:
                _log_error(conn, file_id, f"{table_name}:{row_index}", "Embedded row is missing resource_id")
                continue

            merged["__source_scope"] = table_name
            count += _process_json_record(
                conn,
                merged,
                system_id or origin_sys,
                data_category,
                row_at,
                file_id,
                file_id,
            )

    # ── Schema 3: ordinary tables including profile failover snapshots.
    for tbl in tables_in_db:
        if tbl in SYSTEM_TABLE_CATEGORY:
            continue  # already handled in schema 1
        if tbl in embedded_tables:
            continue  # handled above for every replica table
        if tbl == "snapshot_scope":
            continue

        system_id, data_category = resolve_source_scope("", tbl)
        if not data_category:
            _log_error(conn, file_id, tbl, "Unmapped SQL table category")
            continue

        try:
            rows = tmp.execute(f"SELECT * FROM \"{tbl}\"").fetchall()
        except Exception:
            continue

        for row in rows:
            r = dict(row)
            rid = r.get("resource_id", "")
            if not rid:
                continue
            row_at = r.get("updated_at") or snap_at
            pii = {col: r[col] for col in (
                "full_name", "email", "phone", "email_hash", "user_id",
                "customer_reference", "requester_email", "requester_phone", "owner_email",
                "owner_phone", "customer_email", "customer_phone",
                "retention_until", "retained_until", "retention_reason",
            ) if r.get(col)}
            account_status = r.get("account_status", "")
            state = "ABSENT" if "DELETED" in account_status.upper() else "PRESENT"

            _insert_resource(conn, rid, system_id, data_category, state, pii,
                             row_at, file_id, file_id, source_scope=tbl)
            related_id = r.get("source_resource_id")
            if related_id:
                target_category = _data_category_from_rid(related_id, "")
                target_system = RECORD_TYPE_TO_SYSTEM.get(target_category, "")
                _insert_resource_link(
                    conn, rid, system_id, related_id, target_system,
                    "SOURCE_RESOURCE", row_at, file_id, file_id,
                )
            for col, id_type in [
                ("email",     "email"),
                ("phone",     "phone"),
                ("user_id",   "customer_reference"),
                ("email_hash","email_hash"),
            ]:
                _insert_identifier(conn, rid, system_id, id_type, r.get(col) or "",
                                   row_at, file_id, file_id)
            count += 1

    tmp.close()
    return count


# ---------------------------------------------------------------------------
# Ingestion: JSON files
# ---------------------------------------------------------------------------
def _process_json_record(conn, rec: dict, system_id: str, data_category: str,
                         captured_at: str, file_id: str, evidence_id: str):
    """Extract a single record dict into resources + resource_identifiers."""
    resource_id = rec.get("resource_id", "")
    if not resource_id:
        return 0
    system_id = normalize_system_id(system_id)
    data_category = canonical_category(data_category) or "UNKNOWN"

    # Determine state
    result = str(
        rec.get("state") or rec.get("result") or rec.get("status") or rec.get("account_status") or ""
    ).strip().upper()
    state_map = {
        "FOUND": "PRESENT", "NOT_FOUND": "ABSENT",
        "DELETED": "ABSENT", "PURGED": "ABSENT",
        "IDENTIFIABLE": "PRESENT", "ANONYMIZED": "ANONYMIZED",
        "ACTIVE": "ACTIVE", "DISABLED": "DISABLED",
        "BLOCKED": "BLOCKED", "REDACTED": "REDACTED",
        "PRESENT": "PRESENT", "ABSENT": "ABSENT",
    }
    state = state_map.get(result, "PRESENT")
    restore_allowed = rec.get("restore_allowed")
    enabled = rec.get("enabled")
    if restore_allowed is not None:
        state = "ACTIVE" if str(restore_allowed).strip().lower() in {"1", "true", "yes", "enabled"} else "BLOCKED"
    elif enabled is not None:
        state = "ACTIVE" if str(enabled).strip().lower() in {"1", "true", "yes", "enabled"} else "DISABLED"

    # Keep identity and policy-relevant fields from both flat and nested JSON
    # layouts. Read the top-level value first; do not lose it when no profile
    # object is present.
    pii = {}
    profile = rec.get("profile")
    doc = rec.get("document")
    tracked_fields = (
        "email", "phone", "email_hash", "customer_reference", "user_id",
        "full_name", "name", "display_name", "owner_email", "owner_phone",
        "customer_email", "customer_phone", "requester_email", "requester_phone",
        "requester_name", "owner_name", "subject_reference", "subject_selector",
        "lookup_value", "uploaded_by", "retention_until", "retained_until",
        "retention_reason",
    )
    for col in tracked_fields:
        value = rec.get(col)
        if not value and isinstance(profile, dict):
            value = profile.get(col)
        if not value and isinstance(doc, dict):
            value = doc.get(col)
        if value:
            pii[col] = value

    # Support messages can expose identifiers in their unstructured body.
    body = " ".join(str(rec.get(key) or "") for key in ("message_text", "message_excerpt", "message"))
    body_emails = re.findall(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", body)
    body_phones = re.findall(r"\+?\d[\d\s().-]{7,}\d", body)
    if data_category == "SUPPORT_MESSAGE" and not result:
        state = "PRESENT" if any(pii.get(key) for key in (
            "requester_name", "requester_email", "requester_phone", "owner_email", "owner_phone"
        )) or body_emails or body_phones else "REDACTED"
    elif data_category in {"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"} and not result:
        state = "PRESENT" if any(pii.get(key) for key in (
            "email", "phone", "email_hash", "user_id", "customer_reference"
        )) else "ANONYMIZED"
    elif data_category == "ACCOUNT_TOMBSTONE" and not result:
        direct_identifiers = ("email", "phone", "email_hash", "full_name", "name", "display_name")
        reference_fields = ("subject_reference", "subject_selector", "lookup_value", "uploaded_by")
        reference_exposes_direct_id = any(
            "@" in str(pii.get(key) or "")
            or bool(re.fullmatch(r"\+?\d[\d\s().-]{7,}\d", str(pii.get(key) or "")))
            for key in reference_fields
        )
        state = "PRESENT" if any(pii.get(key) for key in direct_identifiers) or reference_exposes_direct_id else "REDACTED"

    obs_at = rec.get("observed_at") or rec.get("recorded_at") or rec.get("captured_at") or captured_at

    _insert_resource(conn, resource_id, system_id, data_category, state, pii,
                     obs_at, file_id, evidence_id, source_scope=str(rec.get("__source_scope") or ""))

    related_id = rec.get("source_resource_id")
    if related_id:
        target_category = _data_category_from_rid(related_id, "")
        target_system = RECORD_TYPE_TO_SYSTEM.get(target_category, "")
        _insert_resource_link(
            conn, resource_id, system_id, related_id, target_system,
            "SOURCE_RESOURCE", obs_at, file_id, evidence_id,
        )

    # Index identifiers
    email_fields = [("email", "email"), ("owner_email", "email"), ("customer_email", "email"),
                    ("requester_email", "email"), ("subject_reference", "email"),
                    ("subject_selector", "email"), ("lookup_value", "email"),
                    ("uploaded_by", "email")]
    for field, id_type in email_fields:
        v = pii.get(field)
        if v and "@" in v:
            _insert_identifier(conn, resource_id, system_id, id_type, v, obs_at, file_id, evidence_id)
    if pii.get("email_hash"):
        _insert_identifier(conn, resource_id, system_id, "email_hash", pii["email_hash"], obs_at, file_id, evidence_id)
    for field in ("phone", "owner_phone", "customer_phone", "requester_phone"):
        value = pii.get(field)
        if value:
            _insert_identifier(conn, resource_id, system_id, "phone", value, obs_at, file_id, evidence_id)
    for field, id_type in [
        ("customer_reference", "customer_reference"), ("user_id", "customer_reference"),
        ("subject_reference", "user_id"), ("subject_selector", "customer_reference"),
        ("lookup_value", "customer_reference"),
        ("uploaded_by", "customer_reference"),
    ]:
        v = pii.get(field)
        if v:
            if field in {"subject_selector", "lookup_value", "uploaded_by"} and "@" in str(v):
                continue
            if field in {"subject_selector", "lookup_value", "uploaded_by"} and re.fullmatch(
                r"\+?\d[\d\s().-]{7,}\d", str(v)
            ):
                _insert_identifier(conn, resource_id, system_id, "phone", v, obs_at, file_id, evidence_id)
                continue
            _insert_identifier(conn, resource_id, system_id, id_type, v, obs_at, file_id, evidence_id)
    for value in body_emails:
        _insert_identifier(conn, resource_id, system_id, "email", value, obs_at, file_id, evidence_id)
    for value in body_phones:
        _insert_identifier(conn, resource_id, system_id, "phone", value, obs_at, file_id, evidence_id)

    return 1


def _ingest_json_file(conn, json_path: Path, file_id: str, captured_at: str, evidence_manifest: dict):
    try:
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        _log_error(conn, file_id, str(json_path.name), f"JSON parse error: {e}")
        return 0

    count = 0
    evidence_id = file_id

    # ── Format 1: snapshot bundle  {"snapshots": [{data_store, collection, records: [...]}]}
    if "snapshots" in data:
        for snapshot_index, snap in enumerate(data.get("snapshots", []), start=1):
            if not isinstance(snap, dict):
                _log_error(conn, file_id, f"snapshot:{snapshot_index}", "Snapshot entry is not an object")
                continue
            data_store  = snap.get("data_store", "")
            collection  = snap.get("collection", "")
            snap_at     = snap.get("captured_at") or captured_at
            for record_index, rec in enumerate(snap.get("records", []), start=1):
                if not isinstance(rec, dict):
                    _log_error(conn, file_id, f"{snapshot_index}:{record_index}", "Snapshot record is not an object")
                    continue
                system_id, data_category = resolve_source_scope(data_store, collection, rec)
                if not data_category:
                    _record_category_gap(conn, file_id, record_index, system_id or data_store, collection)
                    data_category = "UNKNOWN"
                scoped_record = {**rec, "__source_scope": f"snapshot:{snapshot_index}:{collection}"}
                count += _process_json_record(conn, scoped_record, system_id, data_category,
                                              snap_at, file_id, evidence_id)

    # ── Format 2: service_resource_export  {"service_exports": {system: {table: [...]}}}
    elif "service_exports" in data:
        exp_at = data.get("exported_at") or captured_at
        for system_id, tables in data["service_exports"].items():
            if not isinstance(tables, dict):
                _log_error(conn, file_id, system_id, "Service export is not an object")
                continue
            for table_name, records in tables.items():
                if not isinstance(records, list):
                    _log_error(conn, file_id, table_name, "Service table is not a record array")
                    continue
                for record_index, rec in enumerate(records, start=1):
                    if not isinstance(rec, dict):
                        _log_error(conn, file_id, f"{table_name}:{record_index}", "Export record is not an object")
                        continue
                    normalized_system, data_category = resolve_source_scope(system_id, table_name, rec)
                    if not data_category:
                        _record_category_gap(conn, file_id, record_index, normalized_system or system_id, table_name)
                        data_category = "UNKNOWN"
                    # Handle nested profile
                    obs_at = rec.get("observed_at") or exp_at
                    profile = rec.get("profile") or {}
                    flat_rec = {
                        **rec,
                        **profile,
                        "__source_scope": f"service_exports:{system_id}:{table_name}",
                    }
                    count += _process_json_record(conn, flat_rec, normalized_system, data_category,
                                                  obs_at, file_id, evidence_id)

    # ── Format 3: flat records list  {"records": [...], "system"?: ..., "source"?: ...}
    elif "records" in data:
        rec_at = data.get("captured_at") or data.get("exported_at") or captured_at
        system_hint = data.get("source", "")
        container = data.get("collection", "")
        for record_index, rec in enumerate(data.get("records", []), start=1):
            if not isinstance(rec, dict):
                _log_error(conn, file_id, str(record_index), "Flat record is not an object")
                continue
            system_id, data_category = resolve_source_scope(
                rec.get("system") or system_hint,
                rec.get("collection") or container,
                rec,
            )
            if not data_category:
                _record_category_gap(conn, file_id, record_index, system_id or system_hint, container)
                data_category = "UNKNOWN"
            # subject_reference is the identifier
            subj = rec.get("subject_reference", "")
            r = {**rec, "email": subj if "@" in subj else None,
                 "customer_reference": subj if subj.startswith("user_") else None,
                 "__source_scope": f"records:{container}"}
            obs_at = rec.get("recorded_at") or rec_at
            count += _process_json_record(conn, r, system_id, data_category,
                                          obs_at, file_id, evidence_id)

    # ── Format 4: connector_configurations  {"records": [...]} with resource_id + connector fields
    #    (already handled above, but add specific connector field extraction)

    # ── Format 5: customer_export_jobs  {"records": [...]} with owner_email
    #    (handled by format 3)

    # ── Format 6: security_audit_ledger same as format 3
    return count


# ---------------------------------------------------------------------------
# Ingestion: Log files
# ---------------------------------------------------------------------------

# Key events we extract from logs
LOG_EVENT_PATTERNS = [
    # Profile/index lookups with resource_id and result
    re.compile(
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s.*?"
        r"service=(?P<service>\S+).*?"
        r"resource_id=(?P<rid>\S+).*?"
        r"event=(?P<event>\S+).*?"
        r"(?:rows=(?P<rows>\d+))?.*?"
        r"(?:http_status=(?P<status>\d+))?.*?"
        r"(?:correlation_key=\"(?P<corr>[^\"]+)\")?",
        re.IGNORECASE,
    ),
    # PII scan events
    re.compile(
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s.*?"
        r"service=(?P<service>\S+).*?"
        r"resource_id=(?P<rid>\S+).*?"
        r"event=(?P<event>(?:message_pii_scan|attachment_pii_scan|analytics_identity_scan))\s+"
        r"matched_value=\"(?P<matched>[^\"]+)\"",
        re.IGNORECASE,
    ),
    # restore_candidate_evaluated
    re.compile(
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s.*?"
        r"service=(?P<service>\S+).*?"
        r"resource_id=(?P<rid>\S+).*?"
        r"event=restore_candidate_evaluated\s+"
        r"eligible=(?P<eligible>\w+)\s+deletion_block=(?P<block>\w+)",
        re.IGNORECASE,
    ),
    # connector_heartbeat
    re.compile(
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s.*?"
        r"service=(?P<service>\S+).*?"
        r"resource_id=(?P<rid>\S+).*?"
        r"event=connector_heartbeat\s+"
        r"enabled=(?P<enabled>\w+)",
        re.IGNORECASE,
    ),
    # lookup with lookup_key / match_key / subject_key
    re.compile(
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s.*?"
        r"service=(?P<service>\S+).*?"
        r"(?:lookup_key|match_key|subject_key|scan_key)=\"(?P<lookup_key>[^\"]+)\"",
        re.IGNORECASE,
    ),
]


def _parse_log_line(line: str) -> dict | None:
    """Try to extract structured fields from a log line.

    Handles two formats:
    1. key=value pairs (plain text logs)
    2. <timestamp> {JSON object} (structured JSON logs)
    """
    line = line.strip()
    if not line:
        return None

    # Timestamp at start
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", line)
    if not m:
        return None
    ts = m.group(1)

    # Format 2: JSON payload after timestamp
    rest = line[len(ts):].strip()
    if rest.startswith("{"):
        try:
            kv = json.loads(rest)
            kv["ts"] = ts
            return kv
        except Exception:
            pass

    # Format 1: key=value pairs
    kv = {"ts": ts}
    # Quoted values: key="value"
    for match in re.finditer(r'(\w+)="([^"]*)"', line):
        kv[match.group(1)] = match.group(2)
    # Unquoted values: key=value (skip already captured keys)
    for match in re.finditer(r'(\w+)=([^\s"]\S*)', line):
        if match.group(1) not in kv:
            kv[match.group(1)] = match.group(2)

    return kv if len(kv) > 1 else None


def _state_from_log_event(event: str, kv: dict) -> str:
    """Map log event + kv to a state."""
    event = event.lower()
    if "completed" in event:
        rows = kv.get("rows", "")
        status = kv.get("http_status", "")
        if rows == "0" or status == "404":
            return "UNKNOWN"  # a negative query result is a claim, not deletion proof
        return "PRESENT"
    if "pii_scan" in event or "identity_scan" in event:
        return "PRESENT"  # found PII
    if "restore_candidate_evaluated" in event:
        eligible = kv.get("eligible", "false").lower()
        block    = kv.get("deletion_block", "false").lower()
        if eligible == "true" and block == "false":
            return "ACTIVE"  # backup can restore
        return "UNKNOWN"  # a negative evaluation does not prove restore is blocked
    if "connector_heartbeat" in event:
        enabled = kv.get("enabled", "false").lower()
        return "ACTIVE" if enabled == "true" else "UNKNOWN"
    return "PRESENT"


def _system_id_from_service(service: str) -> str:
    return normalize_system_id(service)


def _data_category_from_rid(rid: str, service: str) -> str:
    _system_id, category = resolve_source_scope(service, record={}, resource_id=rid)
    return category or "UNKNOWN"


def _ingest_log_file(conn, log_path: Path, file_id: str, captured_at: str, evidence_manifest: dict):
    count = 0
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        _log_error(conn, file_id, str(log_path.name), f"Read error: {e}")
        return 0

    for line in lines:
        kv = _parse_log_line(line)
        if not kv:
            continue

        rid   = kv.get("resource_id", "")
        event = kv.get("event", "")
        svc   = kv.get("service", "")
        ts    = kv.get("ts") or captured_at

        if not rid or not event or not svc:
            continue

        # Filter to interesting events
        interesting = (
            "completed" in event
            or "pii_scan" in event
            or "identity_scan" in event
            or "heartbeat" in event
            or "restore_candidate" in event
            or "attachment_pii" in event
        )
        if not interesting:
            continue

        system_id     = _system_id_from_service(svc)
        data_category = _data_category_from_rid(rid, svc)
        if data_category == "UNKNOWN":
            _record_category_gap(conn, file_id, count + 1, system_id, "log resource_id")
        state         = _state_from_log_event(event, kv)

        # PII from log
        pii = {}
        for key in ("lookup_key", "match_key", "correlation_key", "subject_key",
                    "matched_value", "scan_key"):
            v = kv.get(key)
            if v:
                pii[key] = v

        _insert_resource(conn, rid, system_id, data_category, state, pii,
                         ts, file_id, file_id, source_scope="log")

        # Index the identifier
        for key in ("lookup_key", "match_key", "correlation_key", "subject_key",
                    "matched_value", "scan_key"):
            v = kv.get(key)
            if not v:
                continue
            if "@" in v:
                _insert_identifier(conn, rid, system_id, "email", v, ts, file_id, file_id)
            elif v.startswith("user_"):
                _insert_identifier(conn, rid, system_id, "customer_reference", v, ts, file_id, file_id)
            elif re.match(r"^\+?[\d\s\-()]{7,}$", v):
                _insert_identifier(conn, rid, system_id, "phone", v, ts, file_id, file_id)
        count += 1

    return count


# ---------------------------------------------------------------------------
# Ingestion: Text file (handover format)
# ---------------------------------------------------------------------------
def _ingest_txt_file(conn, txt_path: Path, file_id: str, captured_at: str, evidence_manifest: dict):
    """Parse shift handover text format:
    - <ts> | <system> | <resource_id> | <record_type> | <result> | <identifier>
    """
    count = 0
    try:
        lines = txt_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        _log_error(conn, file_id, str(txt_path.name), f"Read error: {e}")
        return 0

    # Parse header for snapshot time
    snap_at = captured_at
    for line in lines[:5]:
        m = re.search(r"Snapshot:\s*(\S+)", line)
        if m:
            snap_at = m.group(1)
            break

    for line in lines:
        # Match: - <ts> | <system> | <resource_id> | <record_type> | <result> | <identifier>
        m = re.match(
            r"\s*-\s*(\d{4}-\d{2}-\d{2}T[\d:]+Z)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(.+)",
            line,
        )
        if not m:
            continue
        ts, system_id, resource_id, record_type, result, identifier = m.groups()
        identifier = identifier.strip()

        state_map = {
            "NOT_FOUND": "ABSENT", "FOUND": "PRESENT",
            "ACTIVE": "ACTIVE", "DISABLED": "DISABLED",
            "BLOCKED": "BLOCKED", "ANONYMIZED": "ANONYMIZED",
            "IDENTIFIABLE": "PRESENT", "REDACTED": "REDACTED",
        }
        state = state_map.get(result.upper(), "PRESENT")
        system_id, data_category = resolve_source_scope(
            system_id, record_type, {"record_type": record_type}, resource_id
        )
        if not data_category:
            _record_category_gap(conn, file_id, count + 1, system_id, record_type)
            data_category = "UNKNOWN"

        pii = {"identifier": identifier}
        _insert_resource(conn, resource_id, system_id, data_category, state, pii,
                         ts, file_id, file_id, source_scope="text")

        # Index identifier
        if "@" in identifier:
            _insert_identifier(conn, resource_id, system_id, "email", identifier, ts, file_id, file_id)
        elif identifier.startswith("user_"):
            _insert_identifier(conn, resource_id, system_id, "customer_reference", identifier, ts, file_id, file_id)
        elif "no direct identifier" not in identifier.lower():
            _insert_identifier(conn, resource_id, system_id, "phone", identifier, ts, file_id, file_id)
        count += 1

    return count


# ---------------------------------------------------------------------------
# PDFs and images: local OCR followed by text-only interpretation
# ---------------------------------------------------------------------------
def _ingest_document_file(conn, file_path: Path, file_id: str, captured_at: str):
    """Index semantic records from locally extracted page text."""
    try:
        from code.evidence_parsers.document_text import extract_records
        records = extract_records(file_path, evidence_id=file_id, captured_at=captured_at)
    except Exception as exc:
        _log_error(conn, file_id, str(file_path.name), f"Document text extraction failed: {type(exc).__name__}")
        return 0

    if records is None:
        _log_error(conn, file_id, str(file_path.name), "Document text extraction unavailable or response could not be parsed")
        return 0
    if not isinstance(records, list):
        _log_error(conn, file_id, file_path.name, "Document text extraction did not return a records array")
        return 0

    count = 0
    known_systems = set(RECORD_TYPE_TO_SYSTEM.values())
    known_states = {"PRESENT", "ABSENT", "REDACTED", "ANONYMIZED", "ACTIVE", "DISABLED", "BLOCKED", "FOUND", "NOT_FOUND"}
    for index, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            _log_error(conn, file_id, str(index), "Document extraction returned a non-object record")
            continue
        rid = rec.get("resource_id")
        if not rid:
            _log_error(conn, file_id, str(index), "Document record is missing resource_id")
            continue
        sid, cat = resolve_source_scope(rec.get("system_id"), rec.get("data_category"), rec, rid)
        if not sid or sid not in known_systems:
            _log_error(conn, file_id, str(index), "Document record has an unknown or missing system_id")
            continue
        if not cat:
            _record_category_gap(conn, file_id, index, sid, str(rec.get("data_category") or "<document_text>"))
            cat = "UNKNOWN"
        state = str(rec.get("state") or "PRESENT").upper()
        if state not in known_states:
            _log_error(conn, file_id, str(index), "Document record has an unsupported state")
            continue
        # The manifest timestamp is authoritative for point-in-time availability.
        obs_at = captured_at

        pii = {k: rec[k] for k in ("email", "customer_reference", "phone", "email_hash")
               if rec.get(k)}
        page_number = rec.get("_page_number")
        try:
            page_number = int(page_number) if page_number is not None else None
        except (TypeError, ValueError):
            page_number = None
        _insert_resource(
            conn, rid, sid, cat, state, pii, obs_at, file_id, file_id,
            source_scope=f"ocr_text_page_{page_number or 1}", page_number=page_number,
            source_filename=rec.get("_source_filename") or file_path.name,
        )
        related_id = rec.get("source_resource_id")
        if related_id:
            target_category = _data_category_from_rid(related_id, "")
            target_system = RECORD_TYPE_TO_SYSTEM.get(target_category, "unknown")
            _insert_resource_link(
                conn, rid, sid, related_id, target_system,
                "SOURCE_RESOURCE", obs_at, file_id, file_id,
            )

        if rec.get("email"):
            _insert_identifier(conn, rid, sid, "email", rec["email"], obs_at, file_id, file_id)
        if rec.get("customer_reference"):
            _insert_identifier(conn, rid, sid, "customer_reference", rec["customer_reference"],
                               obs_at, file_id, file_id)
        if rec.get("phone"):
            _insert_identifier(conn, rid, sid, "phone", rec["phone"], obs_at, file_id, file_id)
        if rec.get("email_hash"):
            _insert_identifier(conn, rid, sid, "email_hash", rec["email_hash"], obs_at, file_id, file_id)
        count += 1

    return count


# ---------------------------------------------------------------------------
# Main build_index function
# ---------------------------------------------------------------------------
def build_index(db_path: str | Path = DEFAULT_DB_PATH, force_rebuild: bool = False) -> Path:
    """Build or rebuild the SQLite evidence index.

    Args:
        db_path: Path to SQLite database (created if missing).
        force_rebuild: Drop and recreate all tables even if DB exists.
    Returns:
        Path to the created database.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if force_rebuild:
        print("Dropping existing tables for rebuild…")
        for tbl in ("resource_links", "resource_identifiers", "resources", "system_activity",
                    "identity_history", "identity_records", "deletion_rules",
                    "ingestion_errors", "source_files", "index_metadata"):
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.commit()

    conn.executescript(SCHEMA)
    _ensure_schema_compatibility(conn)
    version_row = conn.execute(
        "SELECT metadata_value FROM index_metadata WHERE metadata_key='parser_version'"
    ).fetchone()
    if not force_rebuild and (version_row is None or version_row[0] != PARSER_VERSION):
        conn.close()
        return build_index(db_path, force_rebuild=True)
    conn.execute(
        "INSERT OR REPLACE INTO index_metadata(metadata_key, metadata_value) VALUES('parser_version', ?)",
        (PARSER_VERSION,),
    )
    conn.commit()

    # ── Load evidence manifest (must be first)
    manifest_path = DATASET_DIR / "evidence_files.csv"
    evidence_manifest: dict[str, str] = {}
    if manifest_path.exists():
        print("Loading evidence manifest…")
        evidence_manifest = _load_evidence_manifest(conn, manifest_path)
        conn.commit()
        print(f"  {len(evidence_manifest)} evidence files registered")

    # Build reverse lookup: relative path → file_id
    path_to_fid = {v.replace("\\", "/").lower(): k for k, v in evidence_manifest.items()}

    def _get_fid(rel_path: str) -> str:
        norm = rel_path.replace("\\", "/").lower()
        return path_to_fid.get(norm, "")

    def _get_captured_at(file_id: str) -> str:
        if not file_id:
            return _now()
        row = conn.execute("SELECT captured_at FROM source_files WHERE file_id=?", (file_id,)).fetchone()
        return (row["captured_at"] if row and row["captured_at"] else _now())

    # ── CSV files
    print("\nIngesting CSV files…")
    csv_files = {
        "deletion_rules":    (DATASET_DIR / "deletion_rules.csv",    _ingest_deletion_rules),
        "user_identities":   (DATASET_DIR / "user_identities.csv",   _ingest_user_identities),
        "user_audit_log":    (DATASET_DIR / "user_audit_log.csv",    _ingest_user_audit_log),
        "system_activity":   (DATASET_DIR / "system_activity.csv",   _ingest_system_activity),
    }
    for name, (path, fn) in csv_files.items():
        if path.exists():
            fid_lookup = _get_fid(f"dataset/{path.name}")
            count = fn(conn, path)
            print(f"  {name}: {count} rows")
            # Register source file if not already
            conn.execute(
                """INSERT OR IGNORE INTO source_files
                   (file_id, file_path, file_type, origin, captured_at, ingested_at, record_count, status)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (fid_lookup or name, str(path), "csv", "dataset", None, _now(), count, "ok"),
            )
        else:
            print(f"  [MISSING] {path}")
    conn.commit()

    # ── SQL dump files
    sql_dir = EVIDENCE_DIR / "databases"
    if sql_dir.exists():
        sql_files = sorted(sql_dir.glob("*.sql"))
        print(f"\nIngesting {len(sql_files)} SQL dump files…")
        for sql_path in sql_files:
            rel = f"evidence/databases/{sql_path.name}"
            fid = _get_fid(rel)
            cap = _get_captured_at(fid)
            source_id = fid or sql_path.stem
            count = _ingest_cached_file(
                conn, source_id, sql_path, _ingest_sql_file, cap, evidence_manifest
            )
            if count is None:
                print(f"  {sql_path.name}: unchanged; using cached extraction")
                continue
            print(f"  {sql_path.name}: {count} rows")
        conn.commit()

    # ── JSON record files
    json_dir = EVIDENCE_DIR / "records"
    if json_dir.exists():
        json_files = sorted(json_dir.glob("*.json"))
        print(f"\nIngesting {len(json_files)} JSON files…")
        for jp in json_files:
            rel = f"evidence/records/{jp.name}"
            fid = _get_fid(rel)
            cap = _get_captured_at(fid)
            count = _ingest_cached_file(
                conn, fid or jp.stem, jp, _ingest_json_file, cap, evidence_manifest
            )
            if count is None:
                print(f"  {jp.name}: unchanged; using cached extraction")
                continue
            print(f"  {jp.name}: {count} rows")
        conn.commit()

    # ── Text files
    txt_dir = EVIDENCE_DIR / "records"
    if txt_dir.exists():
        for tp in txt_dir.glob("*.txt"):
            rel = f"evidence/records/{tp.name}"
            fid = _get_fid(rel)
            cap = _get_captured_at(fid)
            print(f"\nIngesting text file: {tp.name}…")
            count = _ingest_cached_file(
                conn, fid or tp.stem, tp, _ingest_txt_file, cap, evidence_manifest
            )
            if count is None:
                print(f"  {tp.name}: unchanged; using cached extraction")
                continue
            print(f"  {tp.name}: {count} rows")
        conn.commit()

    # ── Log files
    log_dir = EVIDENCE_DIR / "logs"
    if log_dir.exists():
        log_files = sorted(log_dir.glob("*.log"))
        print(f"\nIngesting {len(log_files)} log files…")
        for lp in log_files:
            rel = f"evidence/logs/{lp.name}"
            fid = _get_fid(rel)
            cap = _get_captured_at(fid)
            count = _ingest_cached_file(
                conn, fid or lp.stem, lp, _ingest_log_file, cap, evidence_manifest
            )
            if count is None:
                print(f"  {lp.name}: unchanged; using cached extraction")
                continue
            print(f"  {lp.name}: {count} rows")
        conn.commit()

    # ── PDFs and images via local OCR + text interpretation
    img_dir  = EVIDENCE_DIR / "images"
    doc_dir  = EVIDENCE_DIR / "documents"
    multimodal_files = []
    image_extensions = ("*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff", "*.webp")
    if img_dir.exists():
        for extension in image_extensions:
            multimodal_files.extend(img_dir.glob(extension))
    if doc_dir.exists():
        multimodal_files.extend(sorted(doc_dir.glob("*.pdf")))

    if multimodal_files:
        multimodal_files.sort()
        print(f"\nIngesting {len(multimodal_files)} PDF/image files via local OCR and text model…")
        for mp in multimodal_files:
            subdir = mp.parent.name
            rel = f"evidence/{subdir}/{mp.name}"
            fid = _get_fid(rel)
            cap = _get_captured_at(fid)
            count = _ingest_cached_file(
                conn, fid or mp.stem, mp, _ingest_document_file, cap
            )
            if count is None:
                from code.evidence_parsers.document_text import account_cached_usage
                account_cached_usage(mp, evidence_id=fid or mp.stem, captured_at=cap)
                print(f"  {mp.name}: unchanged; using cached document extraction")
                continue
            print(f"  {mp.name}: {count} records extracted")
            status_row = conn.execute(
                "SELECT status FROM source_files WHERE file_id = ?", (fid or mp.stem,)
            ).fetchone()
            if status_row and status_row["status"] != "ok":
                print("  Stopping document parsing after the first incomplete PDF/image source.")
                break
        conn.commit()

    # ── Summary
    total_resources = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    total_identifiers = conn.execute("SELECT COUNT(*) FROM resource_identifiers").fetchone()[0]
    total_activity = conn.execute("SELECT COUNT(*) FROM system_activity").fetchone()[0]
    errors = conn.execute("SELECT COUNT(*) FROM ingestion_errors").fetchone()[0]

    print(f"""
Index build complete:
  Resources:   {total_resources}
  Identifiers: {total_identifiers}
  Activity:    {total_activity}
  Errors:      {errors}
  DB:          {db_path}
""")

    conn.close()
    return db_path


if __name__ == "__main__":
    force = "--rebuild" in sys.argv
    build_index(force_rebuild=force)
