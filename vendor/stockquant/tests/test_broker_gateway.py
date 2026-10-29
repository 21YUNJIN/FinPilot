"""Broker gateway safety: read-only first, paper integration, missing QMT."""

import tempfile
import unittest
from pathlib import Path

from stockquant.execution.broker_gateway import (
    BrokerNotConfiguredError,
    QmtReadOnlyGateway,
    ReadOnlyBrokerError,
    create_gateway,
)
from stockquant.execution.manual_gateway import ManualBrokerGateway, ManualExecutionRequired
from stockquant.execution.paper import PaperBroker
from stockquant.execution.types import OrderRequest, Quote


class BrokerGatewayTests(unittest.TestCase):
    def test_paper_gateway_can_be_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            broker = PaperBroker(str(Path(td) / "paper.json"), initial_cash=10000)
            gateway = create_gateway("paper", state_path=str(Path(td) / "paper.json"),
                                     initial_cash=10000, read_only=True)
            status = gateway.connect()
            self.assertTrue(status.connected)
            self.assertTrue(gateway.read_only)
            q = Quote("A", 10.0, 10.1, 10.05, 1000)
            with self.assertRaises(ReadOnlyBrokerError):
                gateway.submit_order(OrderRequest("A", "buy", 100), q)

    def test_qmt_requires_path_and_account(self):
        with self.assertRaises(BrokerNotConfiguredError):
            QmtReadOnlyGateway("", "", 0)
        with self.assertRaises(BrokerNotConfiguredError):
            QmtReadOnlyGateway("/tmp/qmt", "", 0)

    def test_manual_gateway_requires_human_fill_and_updates_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            gateway = ManualBrokerGateway(str(Path(td) / "manual.json"), initial_cash=10000)
            gateway.connect()
            q = Quote("A", 10.0, 10.1, 10.05, 1000)
            with self.assertRaises(ManualExecutionRequired) as ctx:
                gateway.submit_order(OrderRequest("A", "buy", 100), q)
            ticket = ctx.exception.ticket
            self.assertEqual(ticket["status"], "PENDING_MANUAL")
            fill = gateway.record_fill(ticket["manual_order_id"], filled_quantity=40,
                                       fill_price=10.1, commission=1)
            self.assertEqual(fill.quantity, 40)
            account = gateway.get_account()
            self.assertEqual(account.positions["A"].quantity, 40)
            self.assertEqual(len(gateway.get_open_orders()), 1)
            gateway.record_fill(ticket["manual_order_id"], filled_quantity=60,
                                fill_price=10.2, commission=1)
            self.assertEqual(gateway.get_open_orders(), [])
            self.assertEqual(gateway.get_account().positions["A"].quantity, 100)

    def test_manual_gateway_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            gateway = ManualBrokerGateway(str(Path(td) / "manual.json"), initial_cash=10000,
                                          read_only=True)
            gateway.connect()
            with self.assertRaises(ReadOnlyBrokerError):
                gateway.prepare_order(OrderRequest("A", "buy", 100), Quote("A", 10, 10.1, 10))

    def test_unsupported_provider_fails_closed(self):
        with self.assertRaises(BrokerNotConfiguredError):
            create_gateway("unknown")


if __name__ == "__main__":
    unittest.main()
