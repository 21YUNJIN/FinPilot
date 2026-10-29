import tempfile
import unittest

from bankagent.audit import AuditChain
from bankagent.tools import ToolRegistry, ToolSpec


class ToolRegistryTests(unittest.TestCase):
    def test_unknown_tool_returns_structured_error(self):
        registry = ToolRegistry()
        result = registry.call("missing.tool", "analyst")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TOOL_NOT_FOUND")
        self.assertTrue(result.call_id.startswith("call-"))

    def test_role_denial_is_structured_and_audited(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="finance.metrics",
                description="metrics",
                handler=lambda: {"ok": True},
                required_roles=("analyst",),
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditChain(f"{tmp}/audit.jsonl")
            registry.audit = audit
            result = registry.call("finance.metrics", "auditor", task_id="task-1")
            self.assertFalse(result.success)
            self.assertEqual(result.error_code, "TOOL_UNAUTHORIZED")
            self.assertTrue(audit.verify())
            self.assertEqual(audit.read_all()[-1]["error_code"], "TOOL_UNAUTHORIZED")

    def test_write_tool_requires_approval(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="report.write",
                description="write report",
                handler=lambda text: {"written": text},
                read_only=False,
                requires_approval=True,
                required_roles=("analyst", "risk_manager", "admin"),
            )
        )
        denied = registry.call("report.write", "analyst", {"text": "x"})
        self.assertFalse(denied.success)
        self.assertEqual(denied.error_code, "TOOL_APPROVAL_REQUIRED")

        approved = registry.call("report.write", "analyst", {"text": "x"}, approval=True)
        self.assertTrue(approved.success)
        self.assertEqual(approved.result["written"], "x")
        self.assertIsNotNone(approved.duration_ms)

    def test_write_tool_registration_requires_approval_policy(self):
        registry = ToolRegistry()
        with self.assertRaisesRegex(ValueError, "must require approval"):
            registry.register(
                ToolSpec(
                    name="report.write",
                    description="unsafe",
                    handler=lambda: {},
                    read_only=False,
                    requires_approval=False,
                )
            )


if __name__ == "__main__":
    unittest.main()
