import unittest

from bankagent.contracts import Evidence
from bankagent.knowledge import EvidenceStore
from bankagent.policy import PolicyEngine


class EvidenceAndPolicyTests(unittest.TestCase):
    def test_conflict_detection(self):
        store = EvidenceStore()
        e1 = Evidence("e1", "annual.pdf", "收入 100", as_of_date="2024-12-31")
        e2 = Evidence("e2", "research.pdf", "收入 110", as_of_date="2024-12-31")
        store.add(e1)
        store.add(e2)
        store.add_fact("revenue", "2024", 100.0, "e1")
        store.add_fact("revenue", "2024", 110.0, "e2")
        conflicts = store.find_conflicts("revenue", "2024")
        self.assertEqual(len(conflicts), 2)

    def test_as_of_filter(self):
        store = EvidenceStore()
        store.add(Evidence("e1", "annual.pdf", "收入 100", as_of_date="2024-12-31"))
        store.add(Evidence("e2", "future.pdf", "收入 120", as_of_date="2025-12-31"))
        results = store.search("收入", as_of_date="2024-12-31")
        self.assertEqual([item.evidence_id for item in results], ["e1"])

    def test_policy_blocks_unapproved_write(self):
        decision = PolicyEngine().authorize("analyst", "report", read_only=False)
        self.assertFalse(decision.allowed)


if __name__ == "__main__":
    unittest.main()
