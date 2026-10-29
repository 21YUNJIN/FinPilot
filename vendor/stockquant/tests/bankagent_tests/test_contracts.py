import unittest

from bankagent.contracts import (
    AnalysisResult,
    CalculationTrace,
    Claim,
    ClaimType,
    Evidence,
    PlanNode,
    ResultStatus,
    TaskPlan,
    TaskRequest,
    TaskStatus,
    sha256_json,
    validate_task_transition,
)


class ContractTests(unittest.TestCase):
    def test_evidence_round_trip(self):
        evidence = Evidence(
            evidence_id="ev-1",
            source_name="annual.pdf",
            quote="revenue",
            page=3,
            as_of_date="2024-12-31",
            document_id="doc-1",
            char_start=120,
            char_end=127,
        )
        payload = evidence.to_dict()
        self.assertEqual(payload["evidence_id"], "ev-1")
        self.assertEqual(payload["page"], 3)
        self.assertEqual(payload["char_start"], 120)
        self.assertEqual(payload["document_id"], "doc-1")

    def test_task_request_serializes_risk_level(self):
        request = TaskRequest(
            task_id="t-1",
            actor_id="a-1",
            role="analyst",
            question="q",
            as_of_date="2024-12-31",
        )
        payload = request.to_dict()
        self.assertEqual(payload["risk_level"], "medium")
        self.assertEqual(payload["schema_version"], "2.0.0")

    def test_state_machine_accepts_canonical_path(self):
        path = [
            TaskStatus.RECEIVED,
            TaskStatus.PLANNED,
            TaskStatus.RETRIEVING,
            TaskStatus.EVIDENCE_COLLECTED,
            TaskStatus.COMPUTING,
            TaskStatus.COMPUTED,
            TaskStatus.CHALLENGING,
            TaskStatus.CHALLENGED,
            TaskStatus.REPORTING,
            TaskStatus.REPORTED,
        ]
        for current, target in zip(path, path[1:]):
            validate_task_transition(current, target)

    def test_state_machine_rejects_skipped_state(self):
        with self.assertRaisesRegex(ValueError, "STATE_TRANSITION_INVALID"):
            validate_task_transition(TaskStatus.PLANNED, TaskStatus.REPORTED)

    def test_task_plan_serializes_nodes(self):
        plan = TaskPlan(
            plan_id="plan-1",
            task_id="t-1",
            planner_version="planner-1.0",
            nodes=[
                PlanNode(
                    step_id="step-1",
                    name="extract_financials",
                    tool_names=["document.parse"],
                    input_refs=["doc-1"],
                    output_schema={"type": "object"},
                )
            ],
        )
        payload = plan.to_dict()
        self.assertEqual(payload["nodes"][0]["name"], "extract_financials")
        self.assertEqual(payload["max_steps"], 20)

    def test_calculation_trace_and_claim_serialize(self):
        trace = CalculationTrace(
            calculation_id="calc-1",
            name="cash_conversion",
            formula="cfo / net_income",
            inputs={"cfo": 8.0, "net_income": 10.0},
            output=0.8,
            unit="ratio",
            tool_name="finance.metrics",
            tool_version="1.0.0",
            evidence_ids=["ev-1"],
        )
        claim = Claim(
            claim_id="claim-1",
            claim_type=ClaimType.CALCULATION,
            statement="经营现金流/净利润为0.8",
            value=0.8,
            unit="ratio",
            calculation_ids=[trace.calculation_id],
        )
        self.assertEqual(trace.to_dict()["formula"], "cfo / net_income")
        self.assertEqual(claim.to_dict()["claim_type"], "calculation")

    def test_analysis_result_uses_result_status(self):
        result = AnalysisResult(
            task_id="t-1",
            status=ResultStatus.NEEDS_REVIEW,
        )
        payload = result.to_dict()
        self.assertEqual(payload["status"], "needs_review")
        self.assertEqual(payload["schema_version"], "2.0.0")

    def test_sha256_is_stable(self):
        self.assertEqual(sha256_json({"a": 1}), sha256_json({"a": 1}))
        self.assertNotEqual(sha256_json({"a": 1}), sha256_json({"a": 2}))


if __name__ == "__main__":
    unittest.main()
