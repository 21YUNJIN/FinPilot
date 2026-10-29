import tempfile
import unittest

from bankagent.demo import run_demo


class OrchestratorTests(unittest.TestCase):
    def test_end_to_end_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = run_demo(state_dir=tmp)
            result = payload["result"]
            self.assertEqual(result["status"], "reported")
            self.assertGreaterEqual(len(result["citations"]), 1)
            self.assertGreater(len(result["calculations"]), 3)
            self.assertGreaterEqual(len(result["risks"]), 1)
            self.assertTrue(payload["audit_verified"])
            self.assertGreaterEqual(len(payload["steps"]), 5)


if __name__ == "__main__":
    unittest.main()
