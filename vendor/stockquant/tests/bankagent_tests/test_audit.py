import json
import tempfile
import unittest
from pathlib import Path

from bankagent.audit import AuditChain


class AuditTests(unittest.TestCase):
    def test_chain_verifies_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            audit = AuditChain(str(path))
            audit.append("t-1", "analyst", "task_created", {"question": "q"})
            audit.append("t-1", "analyst", "report_generated", {"ok": True})
            self.assertTrue(audit.verify())

            lines = path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["payload"]["question"] = "tampered"
            lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertFalse(audit.verify())


if __name__ == "__main__":
    unittest.main()
