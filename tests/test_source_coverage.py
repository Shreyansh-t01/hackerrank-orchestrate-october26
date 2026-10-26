import sqlite3
import tempfile
import unittest
from pathlib import Path

from code.resource_discovery import get_source_coverage


class SourceCoverageTests(unittest.TestCase):
    def test_unruled_resource_category_makes_coverage_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "coverage.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE source_files (file_id TEXT, status TEXT, captured_at TEXT);
                CREATE TABLE ingestion_errors (source_file TEXT);
                CREATE TABLE resources (data_category TEXT, captured_at TEXT);
                CREATE TABLE deletion_rules (data_category TEXT);
                CREATE TABLE resource_links (id INTEGER);
                CREATE TABLE resource_identifiers (id INTEGER);
                CREATE TABLE system_activity (id INTEGER);
                INSERT INTO source_files VALUES ('evidence_1', 'ok', '2026-01-01T00:00:00Z');
                INSERT INTO resources VALUES ('UNKNOWN', '2026-01-01T00:00:00Z');
                INSERT INTO deletion_rules VALUES ('USER_PROFILE');
                """
            )
            connection.commit()
            connection.close()

            coverage = get_source_coverage(str(db_path), "2026-01-02T00:00:00Z")

            self.assertFalse(coverage["is_complete"])
            self.assertEqual(coverage["unclassified_categories"], ["UNKNOWN"])


if __name__ == "__main__":
    unittest.main()
