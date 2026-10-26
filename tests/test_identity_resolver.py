import csv
import tempfile
import unittest
from pathlib import Path

from code.identity_resolver import TemporalIdentityResolver


class TemporalIdentityResolverTests(unittest.TestCase):
    def test_reassigned_email_resolves_to_owner_at_request_time(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            identities_path = root / "user_identities.csv"
            audit_path = root / "user_audit_log.csv"

            with identities_path.open("w", newline="", encoding="utf-8") as target:
                writer = csv.writer(target)
                writer.writerow(["user_id", "name", "email", "phone", "email_hash", "updated_at"])
                writer.writerow(["user_1", "A", "new@example.test", "", "hash_1", "2026-04-01T00:00:00Z"])
                writer.writerow(["user_2", "B", "shared@example.test", "", "hash_2", "2026-04-01T00:00:00Z"])

            with audit_path.open("w", newline="", encoding="utf-8") as target:
                writer = csv.writer(target)
                writer.writerow(["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"])
                writer.writerow(["audit_1", "user_1", "2026-01-02T00:00:00Z", "email", "old@example.test", "shared@example.test"])
                writer.writerow(["audit_2", "user_1", "2026-02-02T00:00:00Z", "email", "shared@example.test", "new@example.test"])
                writer.writerow(["audit_3", "user_2", "2026-03-01T00:00:00Z", "email", "other@example.test", "shared@example.test"])

            resolver = TemporalIdentityResolver(identities_path, audit_path)
            january_owner = resolver.resolve_user_identity(
                "shared@example.test", "2026-01-15T00:00:00Z", "2026-04-15T00:00:00Z"
            )
            march_owner = resolver.resolve_user_identity(
                "shared@example.test", "2026-03-15T00:00:00Z", "2026-04-15T00:00:00Z"
            )

            self.assertEqual(january_owner.user_id, "user_1")
            self.assertEqual(march_owner.user_id, "user_2")
            self.assertNotIn("other@example.test", january_owner.all_emails)


if __name__ == "__main__":
    unittest.main()
