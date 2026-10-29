"""Hard ETF execution gates: stale/expired/legacy/demo plans must not execute."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stockquant.data.calendar import (
    latest_completed_trading_day,
    next_trading_day,
    previous_trading_day,
)
from stockquant.etf_plan import evaluate_plan_gate, plan_expiry
from scripts.etf_plan import _append_orders_once

SH = ZoneInfo("Asia/Shanghai")


def make_plan(**overrides):
    plan = {
        "schema_version": 2,
        "plan_id": "etf-test",
        "status": "ready",
        "execution_allowed": True,
        "data_fresh": True,
        "capital": 50_000.0,
        "asof": "2026-10-16",
        "execute_on": "2026-10-19",
        "expires_at": plan_expiry("2026-10-19"),
        "orders": [{"symbol": "510300", "side": "BUY", "shares": 100}],
    }
    plan.update(overrides)
    return plan


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cal = self.dir / "calendar.csv"
        self.cal.write_text(
            "date\n2026-10-15\n2026-10-16\n2026-10-19\n2026-10-20\n",
            encoding="utf-8",
        )

    def test_sunday_uses_friday_and_monday_open_uses_friday(self):
        self.assertEqual(latest_completed_trading_day(
            datetime(2026, 10, 18, 12, 0, tzinfo=SH), path=str(self.cal)), "2026-10-16")
        self.assertEqual(latest_completed_trading_day(
            datetime(2026, 10, 19, 8, 0, tzinfo=SH), path=str(self.cal)), "2026-10-16")

    def test_monday_after_close_uses_monday(self):
        self.assertEqual(latest_completed_trading_day(
            datetime(2026, 10, 19, 15, 1, tzinfo=SH), path=str(self.cal)), "2026-10-19")

    def test_previous_and_next_are_strict(self):
        self.assertEqual(previous_trading_day("2026-10-19", path=str(self.cal)), "2026-10-16")
        self.assertEqual(next_trading_day("2026-10-16", path=str(self.cal)), "2026-10-19")


class ExecutionLogIdempotencyTests(unittest.TestCase):
    def test_same_plan_is_not_appended_twice(self):
        log = Path(tempfile.mkdtemp()) / "execution_log.csv"
        orders = [
            {"symbol": "510300", "side": "BUY", "shares": 100, "ref_price": 4.0},
            {"symbol": "518880", "side": "BUY", "shares": 200, "ref_price": 8.0},
        ]
        first = _append_orders_once(log, orders, signal_date="2026-10-16",
                                    execute_on="2026-10-19", plan_id="etf-test")
        second = _append_orders_once(log, orders, signal_date="2026-10-16",
                                     execute_on="2026-10-19", plan_id="etf-test")
        self.assertEqual(first, (2, 0))
        self.assertEqual(second, (0, 2))
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)  # header + two orders
        self.assertIn("plan_id", lines[0])
        self.assertTrue(all("etf-test" in line for line in lines[1:]))

    def test_legacy_header_is_migrated_without_losing_rows(self):
        log = Path(tempfile.mkdtemp()) / "execution_log.csv"
        log.write_text(
            "signal_date,order_date,symbol,side,planned_shares,planned_price,"
            "filled_shares,fill_price,commission,filled_at,note\n"
            "2026-10-16,2026-10-16,510300,BUY,700,4.2440,,,,,\n",
            encoding="utf-8",
        )
        appended, skipped = _append_orders_once(
            log, [{"symbol": "518880", "side": "BUY", "shares": 200, "ref_price": 8.0}],
            signal_date="2026-10-16", execute_on="2026-10-19", plan_id="etf-new",
        )
        self.assertEqual((appended, skipped), (1, 0))
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertIn("plan_id", lines[0])
        self.assertEqual(lines[1].split(",")[-1], "")          # legacy row has no plan id
        self.assertEqual(lines[2].split(",")[-1], "etf-new")


class PlanGateTests(unittest.TestCase):
    def test_ready_plan_passes(self):
        gate = evaluate_plan_gate(
            make_plan(), {"capital": 50_000, "min_executable_capital": 50_000},
            now=datetime(2026, 10, 18, 12, 0, tzinfo=SH),
        )
        self.assertTrue(gate["ok"], gate)
        self.assertFalse(gate["reasons"])

    def test_expired_opening_window_blocks(self):
        gate = evaluate_plan_gate(
            make_plan(), {"capital": 50_000, "min_executable_capital": 50_000},
            now=datetime(2026, 10, 19, 10, 0, tzinfo=SH),
        )
        self.assertFalse(gate["ok"])
        self.assertIn("plan_expired", gate["reasons"])

    def test_past_execute_date_blocks(self):
        gate = evaluate_plan_gate(
            make_plan(), {"capital": 50_000, "min_executable_capital": 50_000},
            now=datetime(2026, 10, 20, 8, 0, tzinfo=SH),
        )
        self.assertFalse(gate["ok"])
        self.assertIn("execute_date_passed", gate["reasons"])

    def test_legacy_plan_blocks(self):
        gate = evaluate_plan_gate(
            {"asof": "2026-10-16", "execute_on": "next_trading_day_open", "capital": 20_000},
            {"capital": 50_000, "min_executable_capital": 50_000},
            now=datetime(2026, 10, 18, 12, 0, tzinfo=SH),
        )
        self.assertFalse(gate["ok"])
        self.assertIn("legacy_plan", gate["reasons"])

    def test_demo_plan_is_never_executable(self):
        gate = evaluate_plan_gate(
            make_plan(status="demo", execution_allowed=False),
            {"capital": 50_000, "min_executable_capital": 50_000},
            now=datetime(2026, 10, 18, 12, 0, tzinfo=SH),
        )
        self.assertFalse(gate["ok"])
        self.assertIn("plan_status_demo", gate["reasons"])

    def test_unconfigured_or_below_gate_blocks(self):
        gate = evaluate_plan_gate(
            make_plan(capital=20_000),
            {"capital": None, "min_executable_capital": 50_000},
            now=datetime(2026, 10, 18, 12, 0, tzinfo=SH),
        )
        self.assertFalse(gate["ok"])
        self.assertIn("capital_not_configured", gate["reasons"])
        self.assertIn("plan_capital_below_gate", gate["reasons"])

    def test_capital_mismatch_blocks(self):
        gate = evaluate_plan_gate(
            make_plan(capital=60_000),
            {"capital": 50_000, "min_executable_capital": 50_000},
            now=datetime(2026, 10, 18, 12, 0, tzinfo=SH),
        )
        self.assertFalse(gate["ok"])
        self.assertIn("capital_mismatch", gate["reasons"])

    def test_plan_expiry_is_open_grace(self):
        self.assertEqual(plan_expiry("2026-10-19"), "2026-10-19T09:35:00+08:00")


if __name__ == "__main__":
    unittest.main()
