import unittest

from code.data_catalog import resolve_source_scope


class DataCatalogTests(unittest.TestCase):
    def test_service_export_names_are_resolved_with_their_service(self):
        cases = [
            ("analytics_platform", "profiles", "ANALYTICS_PROFILE"),
            ("support_platform", "tickets", "SUPPORT_MESSAGE"),
            ("search_service", "index_queries", "SEARCH_INDEX_ENTRY"),
            ("account_service", "profile_lookups", "USER_PROFILE"),
            ("backup_service", "restore_entries", "BACKUP_ARCHIVE"),
            ("export_service", "exports", "USER_EXPORT"),
            ("integration_service", "connectors", "DATA_FLOW"),
            ("security_service", "audit_records", "SECURITY_AUDIT_LOG"),
        ]
        for system_id, table_name, expected_category in cases:
            with self.subTest(system_id=system_id, table_name=table_name):
                resolved_system, category = resolve_source_scope(system_id, table_name)
                self.assertEqual(resolved_system, system_id)
                self.assertEqual(category, expected_category)

    def test_snapshot_collections_and_operational_history_are_resolved(self):
        self.assertEqual(
            resolve_source_scope("support_platform", "ticket_attachments"),
            ("support_platform", "SUPPORT_ATTACHMENT"),
        )

    def test_sql_replica_suffixes_keep_their_logical_category(self):
        self.assertEqual(
            resolve_source_scope("", "customer_profiles_failover_replica"),
            ("account_service", "USER_PROFILE"),
        )
        self.assertEqual(
            resolve_source_scope("", "search_documents_primary"),
            ("search_service", "SEARCH_INDEX_ENTRY"),
        )
        self.assertEqual(
            resolve_source_scope(
                "backup_restore_service",
                "operational_record_history",
                {"system": "search_service", "record_type": "SEARCH_INDEX_ENTRY"},
            ),
            ("search_service", "SEARCH_INDEX_ENTRY"),
        )

    def test_unknown_container_does_not_inherit_a_generic_service_category(self):
        self.assertEqual(resolve_source_scope("support_platform", "unmapped_table"), (
            "support_platform", ""
        ))

    def test_category_or_resource_prefix_can_recover_system_and_category(self):
        self.assertEqual(
            resolve_source_scope("", "", {"record_type": "ACCOUNT_TOMBSTONE"}),
            ("identity_service", "ACCOUNT_TOMBSTONE"),
        )
        self.assertEqual(
            resolve_source_scope("", "", {}, "support_attachment_7"),
            ("support_platform", "SUPPORT_ATTACHMENT"),
        )


if __name__ == "__main__":
    unittest.main()
