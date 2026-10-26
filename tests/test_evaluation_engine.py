import sqlite3
import tempfile
import unittest

from code.evaluation_engine import ResourceState, evaluate_resource, reconstruct_snapshot_state


class SnapshotEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db_path = self.temporary_directory.name + "/evidence.db"
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE resources (
                resource_id TEXT, system_id TEXT, data_category TEXT, state TEXT,
                pii_fields_json TEXT, captured_at TEXT, source_file TEXT, evidence_id TEXT
            );
            CREATE TABLE system_activity (
                activity_id TEXT, event_time TEXT, event_type TEXT, source_system TEXT,
                target_system TEXT, resource_id TEXT, subject_ref TEXT,
                reported_result TEXT, details_json TEXT, source_file TEXT
            );
            """
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_success_claim_after_snapshot_does_not_prove_absence(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)",
            ("account_1", "account_service", "USER_PROFILE", "PRESENT", None,
             "2026-01-01T00:00:00Z", "evidence_1", "evidence_1"),
        )
        connection.execute(
            "INSERT INTO system_activity VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("activity_1", "2026-01-02T00:00:00Z", "DELETE_RECORD", "worker",
             "account_service", "account_1", "user_1", "SUCCESS", None, "activity.csv"),
        )
        connection.commit()
        connection.close()

        state, evidence_ids, note = reconstruct_snapshot_state(
            "account_1", "account_service", "2026-01-03T00:00:00Z", self.db_path
        )

        self.assertEqual(state, ResourceState.UNKNOWN)
        self.assertEqual(evidence_ids, ["evidence_1"])
        self.assertIn("unverified", note)

    def test_account_tombstone_with_direct_identifiers_requires_redaction(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)",
            ("account_tombstone_7", "identity_service", "ACCOUNT_TOMBSTONE", "PRESENT",
             '{"email":"member@example.test"}', "2026-01-01T00:00:00Z",
             "evidence_tombstone", "evidence_tombstone"),
        )
        connection.commit()
        connection.close()

        evaluation = evaluate_resource(
            resource_id="account_tombstone_7",
            system_id="identity_service",
            data_category="ACCOUNT_TOMBSTONE",
            required_action="ACCOUNT_TOMBSTONE",
            audit_at="2026-01-02T00:00:00Z",
            db_path=self.db_path,
        )

        self.assertFalse(evaluation.is_compliant)
        self.assertEqual(evaluation.finding_op, "REPORT_EXPOSED_PII")
        self.assertEqual(evaluation.remediation_op, "REDACT_RECORD")
        self.assertEqual(evaluation.verify_op, "VERIFY_RECORD_REDACTED")


if __name__ == "__main__":
    unittest.main()
