import unittest

from code.output_validation import validate_steps


class OutputValidationTests(unittest.TestCase):
    def test_non_deletion_has_an_empty_steps_array(self):
        self.assertEqual(validate_steps("NOT_APPLICABLE", [], set()), [])

    def test_future_remediation_and_verification_do_not_claim_evidence(self):
        steps = [
            {
                "step_no": 1, "phase": "FINDING", "system_id": "support_platform",
                "resource_id": "support_1", "operation": "REPORT_REMAINING_RECORD",
                "state": "PRESENT", "evidence_id": "evidence_1",
            },
            {
                "step_no": 2, "phase": "REMEDIATION", "system_id": "support_platform",
                "resource_id": "support_1", "operation": "DELETE_RECORD",
                "state": "ABSENT", "evidence_id": "",
            },
            {
                "step_no": 3, "phase": "VERIFICATION", "system_id": "support_platform",
                "resource_id": "support_1", "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT", "evidence_id": "",
            },
        ]
        self.assertEqual(validate_steps("INCOMPLETE", steps, {"evidence_1"}), [])

    def test_complete_requires_evidence_backed_verification(self):
        step = {
            "step_no": 1, "phase": "VERIFICATION", "system_id": "account_service",
            "resource_id": "account_1", "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT", "evidence_id": "",
        }
        self.assertTrue(validate_steps("COMPLETE", [step]))

    def test_incomplete_requires_findings_remediation_and_verification(self):
        self.assertTrue(validate_steps("INCOMPLETE", []))

    def test_incomplete_coverage_mode_keeps_unverifiable_plan_empty(self):
        self.assertEqual(
            validate_steps("INCOMPLETE", [], allow_incomplete_coverage=True),
            [],
        )

    def test_validator_rejects_operation_order_drift(self):
        steps = [
            {
                "step_no": 1, "phase": "FINDING", "system_id": "integration_service",
                "resource_id": "data_flow_1", "operation": "REPORT_ACTIVE_DATA_FLOW",
                "state": "ACTIVE", "evidence_id": "evidence_1",
            },
            {
                "step_no": 2, "phase": "FINDING", "system_id": "account_service",
                "resource_id": "account_1", "operation": "REPORT_REMAINING_RECORD",
                "state": "PRESENT", "evidence_id": "evidence_1",
            },
            {
                "step_no": 3, "phase": "REMEDIATION", "system_id": "integration_service",
                "resource_id": "data_flow_1", "operation": "DISABLE_DATA_FLOW",
                "state": "DISABLED", "evidence_id": "",
            },
            {
                "step_no": 4, "phase": "REMEDIATION", "system_id": "account_service",
                "resource_id": "account_1", "operation": "DELETE_RECORD",
                "state": "ABSENT", "evidence_id": "",
            },
            {
                "step_no": 5, "phase": "VERIFICATION", "system_id": "integration_service",
                "resource_id": "data_flow_1", "operation": "VERIFY_DATA_FLOW_DISABLED",
                "state": "DISABLED", "evidence_id": "",
            },
            {
                "step_no": 6, "phase": "VERIFICATION", "system_id": "account_service",
                "resource_id": "account_1", "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT", "evidence_id": "",
            },
        ]
        errors = validate_steps("INCOMPLETE", steps, {"evidence_1"})
        self.assertTrue(any("operation order" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
