import unittest

from code.evaluation_engine import ResourceEvaluation, ResourceState
from code.step_builder import build_steps


class StepBuilderTests(unittest.TestCase):
    def test_incomplete_case_verifies_full_resource_portfolio(self):
        account = ResourceEvaluation(
            resource_id="account_007", system_id="account_service",
            data_category="USER_PROFILE", required_action="DELETE",
            current_state=ResourceState.ABSENT, target_state=ResourceState.ABSENT,
            is_compliant=True, evidence_ids=["evidence_account"],
            verify_op="VERIFY_RECORD_ABSENT",
        )
        support = ResourceEvaluation(
            resource_id="support_001", system_id="support_platform",
            data_category="SUPPORT_ATTACHMENT", required_action="DELETE",
            current_state=ResourceState.PRESENT, target_state=ResourceState.ABSENT,
            is_compliant=False, evidence_ids=["evidence_58"],
            finding_op="REPORT_REMAINING_RECORD", finding_state="PRESENT",
            remediation_op="DELETE_RECORD", verify_op="VERIFY_RECORD_ABSENT",
        )

        steps = build_steps([account, support], "INCOMPLETE")

        self.assertEqual([step["phase"] for step in steps], [
            "FINDING", "REMEDIATION", "VERIFICATION", "VERIFICATION"
        ])
        self.assertEqual(
            [(step["system_id"], step["resource_id"]) for step in steps[-2:]],
            [("account_service", "account_007"), ("support_platform", "support_001")],
        )
        self.assertEqual([step["evidence_id"] for step in steps[1:]], ["", "", ""])

    def test_steps_follow_the_problem_statement_operation_order(self):
        evaluations = [
            ResourceEvaluation(
                resource_id="data_flow_1", system_id="integration_service",
                data_category="DATA_FLOW", required_action="DELETE",
                current_state=ResourceState.ACTIVE, target_state=ResourceState.DISABLED,
                is_compliant=False, evidence_ids=["evidence_flow"],
                finding_op="REPORT_ACTIVE_DATA_FLOW", remediation_op="DISABLE_DATA_FLOW",
                verify_op="VERIFY_DATA_FLOW_DISABLED",
            ),
            ResourceEvaluation(
                resource_id="backup_1", system_id="backup_service",
                data_category="BACKUP_ARCHIVE", required_action="RETAIN",
                current_state=ResourceState.ACTIVE, target_state=ResourceState.BLOCKED,
                is_compliant=False, evidence_ids=["evidence_backup"],
                finding_op="REPORT_RESTORE_RISK", remediation_op="BLOCK_RESTORE",
                verify_op="VERIFY_RESTORE_BLOCKED",
            ),
            ResourceEvaluation(
                resource_id="support_1", system_id="support_platform",
                data_category="SUPPORT_MESSAGE", required_action="REDACT",
                current_state=ResourceState.PRESENT, target_state=ResourceState.REDACTED,
                is_compliant=False, evidence_ids=["evidence_support"],
                finding_op="REPORT_EXPOSED_PII", remediation_op="REDACT_RECORD",
                verify_op="VERIFY_RECORD_REDACTED",
            ),
            ResourceEvaluation(
                resource_id="account_1", system_id="account_service",
                data_category="USER_PROFILE", required_action="DELETE",
                current_state=ResourceState.PRESENT, target_state=ResourceState.ABSENT,
                is_compliant=False, evidence_ids=["evidence_account"],
                finding_op="REPORT_REMAINING_RECORD", remediation_op="DELETE_RECORD",
                verify_op="VERIFY_RECORD_ABSENT",
            ),
        ]

        steps = build_steps(evaluations, "INCOMPLETE")
        findings = [step["operation"] for step in steps if step["phase"] == "FINDING"]
        remediations = [step["operation"] for step in steps if step["phase"] == "REMEDIATION"]
        verifications = [step["operation"] for step in steps if step["phase"] == "VERIFICATION"]
        self.assertEqual(findings, [
            "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII",
            "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
        ])
        self.assertEqual(remediations, [
            "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD",
        ])
        self.assertEqual(verifications, [
            "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_REDACTED",
            "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
        ])


if __name__ == "__main__":
    unittest.main()
