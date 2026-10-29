import os
import sys
import types
import unittest
from unittest.mock import patch

from stockquant.execution.ccxt_broker import CcxtBroker


class FakeExchange:
    id = "okx"

    def __init__(self, options):
        self.options = dict(options)
        self.headers = {}

    def set_sandbox_mode(self, enabled):
        self.sandbox = bool(enabled)

    def sign(self, path, api="public", method="GET", params=None, headers=None, body=None):
        return {"headers": dict(headers or {})}

    def load_markets(self):
        return {}


class BrokerSafetyTests(unittest.TestCase):
    def test_live_mode_ignores_simulation_environment_marker(self):
        fake_ccxt = types.SimpleNamespace(okx=FakeExchange)
        with patch.dict(sys.modules, {"ccxt": fake_ccxt}), patch.dict(
            os.environ, {"STOCKQUANT_OKX_SIMULATED": "1"}, clear=False
        ):
            broker = CcxtBroker("okx", testnet=False)
        self.assertFalse(broker.simulated)
        self.assertFalse(getattr(broker.exchange, "sandbox", False))

    def test_simulation_mode_sets_header_after_signing(self):
        fake_ccxt = types.SimpleNamespace(okx=FakeExchange)
        with patch.dict(sys.modules, {"ccxt": fake_ccxt}), patch.dict(
            os.environ, {"STOCKQUANT_OKX_SIMULATED": "1"}, clear=False
        ):
            broker = CcxtBroker("okx", testnet=True)
            signed = broker.exchange.sign("/api/v5/account/balance", api="private")
        self.assertTrue(broker.simulated)
        self.assertEqual(signed["headers"]["x-simulated-trading"], "1")


if __name__ == "__main__":
    unittest.main()
