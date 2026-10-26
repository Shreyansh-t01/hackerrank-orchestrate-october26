import sqlite3
import unittest

from code.ingestion import _process_json_record


class JsonIngestionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE resources (
                id INTEGER PRIMARY KEY,
                resource_id TEXT, system_id TEXT, data_category TEXT, state TEXT,
                pii_fields_json TEXT, captured_at TEXT, source_file TEXT, evidence_id TEXT,
                source_filename TEXT, page_number INTEGER,
                source_scope TEXT NOT NULL DEFAULT '',
                UNIQUE(resource_id, system_id, captured_at, source_file, source_scope)
            );
            CREATE TABLE resource_identifiers (
                id INTEGER PRIMARY KEY,
                resource_id TEXT, system_id TEXT, identifier_type TEXT,
                identifier_value TEXT, identifier_norm TEXT, captured_at TEXT,
                source_file TEXT, evidence_id TEXT,
                UNIQUE(resource_id, system_id, identifier_type, identifier_norm, captured_at, source_file)
            );
            CREATE TABLE resource_links (
                id INTEGER PRIMARY KEY,
                source_resource_id TEXT, source_system_id TEXT, target_resource_id TEXT,
                target_system_id TEXT, relationship TEXT, captured_at TEXT,
                source_file TEXT, evidence_id TEXT
            );
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_flat_json_identifiers_are_not_lost_without_nested_profile(self):
        _process_json_record(
            self.connection,
            {"resource_id": "account_7", "email": "person@example.test", "phone": "+1 555 010 1234"},
            "account_service",
            "USER_PROFILE",
            "2026-01-01T00:00:00Z",
            "evidence_1",
            "evidence_1",
        )

        indexed = {
            row[0]
            for row in self.connection.execute("SELECT identifier_type FROM resource_identifiers")
        }
        self.assertEqual(indexed, {"email", "phone"})


if __name__ == "__main__":
    unittest.main()
