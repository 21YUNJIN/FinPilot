import json
import tempfile
import unittest
from pathlib import Path

from stockquant.execution.store import (
    AuditLog,
    SqliteStrategyRegistry,
    StateStore,
    import_json_state,
)


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = StateStore(str(self.dir / "state.db"))

    def test_state_roundtrip(self):
        self.store.set_state("portfolio", {"cash": 100.0, "positions": {"A": 1}})
        self.assertEqual(self.store.get_state("portfolio")["cash"], 100.0)
        self.store.set_state("portfolio", {"cash": 200.0})
        self.assertEqual(self.store.get_state("portfolio")["cash"], 200.0)
        self.assertIn("portfolio", self.store.all_state())
        self.assertIsNone(self.store.get_state("missing"))

    def test_audit_log_append_and_query(self):
        log = AuditLog(self.store)
        log.record("order", {"symbol": "A", "side": "buy", "qty": 10})
        log.record("fill", {"symbol": "A", "price": 10.0})
        log.record("order", {"symbol": "B", "side": "sell", "qty": 5})
        self.assertEqual(self.store.count_audit(), 3)
        orders = log.entries(event_type="order")
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[0]["payload"]["symbol"], "B")  # newest first
        self.assertEqual(len(log.entries()), 3)

    def test_import_json_state(self):
        path = self.dir / "paper_broker.json"
        path.write_text(json.dumps({"cash": 42.0}), encoding="utf-8")
        key = import_json_state(self.store, str(path))
        self.assertEqual(key, "paper_broker")
        self.assertEqual(self.store.get_state("paper_broker")["cash"], 42.0)


class SqliteRegistryTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.registry = SqliteStrategyRegistry(str(self.dir / "registry.db"))

    def test_register_and_promote(self):
        rec = self.registry.register("v1", {"factor": "value"}, {"sharpe": 0.5})
        self.assertEqual(rec["stage"], "backtest")
        self.assertFalse(rec["active"])
        self.assertEqual(len(self.registry.list_versions()), 1)

        promoted = self.registry.promote(rec["version_id"], "paper")
        self.assertEqual(promoted["stage"], "paper")
        self.assertIsNone(self.registry.active())  # paper is not active live

        live = self.registry.promote(rec["version_id"], "live")
        self.assertTrue(live["active"])
        self.assertEqual(self.registry.active()["version_id"], rec["version_id"])

    def test_register_is_idempotent(self):
        a = self.registry.register("v1", {"factor": "value"}, {"sharpe": 0.5})
        b = self.registry.register("v1", {"factor": "value"}, {"sharpe": 0.9})
        self.assertEqual(a["version_id"], b["version_id"])
        self.assertEqual(len(self.registry.list_versions()), 1)

    def test_rollback(self):
        first = self.registry.register("v1", {"factor": "value"}, {})
        self.registry.promote(first["version_id"], "live")
        second = self.registry.register("v2", {"factor": "momentum"}, {})
        self.registry.promote(second["version_id"], "live")
        self.assertEqual(self.registry.active()["version_id"], second["version_id"])
        rolled = self.registry.rollback()
        self.assertEqual(rolled["version_id"], first["version_id"])
        self.assertEqual(self.registry.active()["version_id"], first["version_id"])

    def test_invalid_stage(self):
        with self.assertRaises(ValueError):
            self.registry.register("v1", {}, {}, stage="moon")


if __name__ == "__main__":
    unittest.main()
