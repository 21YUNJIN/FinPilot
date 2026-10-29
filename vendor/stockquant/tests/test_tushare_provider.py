import unittest

try:
    import pandas as pd
    HAS_PANDAS = True
except Exception:
    HAS_PANDAS = False


def _make_provider(pro):
    from stockquant.data.tushare_provider import TushareProvider
    obj = TushareProvider.__new__(TushareProvider)
    obj.pro = pro
    obj.pause = 0.0
    return obj


class _FakePro:
    def fina_indicator(self, **kwargs):
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "ann_date": "20240115", "end_date": "20231231",
             "update_flag": "0", "eps": 1.5, "bps": 12.0, "roe": 12.5,
             "grossprofit_margin": 30.0, "or_yoy": 8.0, "netprofit_yoy": 10.0},
        ])

    def forecast(self, **kwargs):
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "ann_date": "20240120", "end_date": "20231231",
             "type": "预增", "p_change_min": 5.0, "p_change_max": 15.0},
        ])

    def daily(self, **kwargs):
        return pd.DataFrame([
            {"trade_date": "20240116", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "vol": 1000},
            {"trade_date": "20240117", "open": 10.2, "high": 10.6, "low": 10.0, "close": 10.4, "vol": 1100},
            {"trade_date": "20240122", "open": 10.4, "high": 10.8, "low": 10.3, "close": 10.7, "vol": 1200},
        ])

    def daily_basic(self, **kwargs):
        return pd.DataFrame([
            {"trade_date": "20240116", "pe": 7.0, "pb": 0.9, "total_mv": 3000000, "turnover_rate": 1.0},
            {"trade_date": "20240117", "pe": 7.1, "pb": 0.9, "total_mv": 3000000, "turnover_rate": 1.0},
            {"trade_date": "20240122", "pe": 7.2, "pb": 0.9, "total_mv": 3000000, "turnover_rate": 1.0},
        ])

    def adj_factor(self, **kwargs):
        return pd.DataFrame([
            {"trade_date": "20240116", "adj_factor": 1.0},
            {"trade_date": "20240117", "adj_factor": 1.0},
            {"trade_date": "20240122", "adj_factor": 1.0},
        ])

    def stock_basic(self, **kwargs):
        status = kwargs.get("list_status", "L")
        rows = {
            "L": [{"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行", "industry": "银行", "list_status": "L", "list_date": "19910403", "delist_date": None}],
            "D": [{"ts_code": "000002.SZ", "symbol": "000002", "name": "万科A", "industry": "房地产", "list_status": "D", "list_date": "19910129", "delist_date": "20220101"}],
            "P": [],
        }.get(status, [])
        return pd.DataFrame(rows)


@unittest.skipUnless(HAS_PANDAS, "pandas is not installed")
class TushareProviderTests(unittest.TestCase):
    def test_fundamentals_point_in_time(self):
        provider = _make_provider(_FakePro())
        snapshots = provider._fundamentals_by_announcement("000001.SZ", "2024-01-01", "2024-12-31")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["ann_date"], "2024-01-15")
        self.assertAlmostEqual(snapshots[0]["roe"], 12.5)
        self.assertAlmostEqual(snapshots[0]["eps"], 1.5)
        self.assertAlmostEqual(snapshots[0]["gross_margin"], 30.0)

    def test_forecast_parsing(self):
        provider = _make_provider(_FakePro())
        events = provider._forecast_by_announcement("000001.SZ", "2024-01-01", "2024-12-31")
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["forecast_growth"], 10.0)
        self.assertEqual(events[0]["forecast_positive"], 1.0)

    def test_daily_rows_fill_fundamentals_only_after_announcement(self):
        provider = _make_provider(_FakePro())
        rows = provider._daily_rows("000001.SZ", {"industry": "银行", "name": "平安银行"}, "2024-01-16", "2024-01-22")
        self.assertEqual(len(rows), 3)
        # ann_date 20240115: all three trading days are after announcement.
        self.assertAlmostEqual(rows[0]["roe"], 12.5)
        self.assertAlmostEqual(rows[0]["eps"], 1.5)
        self.assertAlmostEqual(rows[0]["gross_margin"], 30.0)
        self.assertAlmostEqual(rows[0]["revenue_growth"], 8.0)
        self.assertAlmostEqual(rows[0]["profit_growth"], 10.0)
        # forecast ann_date 20240120: only the last trading day sees it.
        self.assertEqual(rows[0]["forecast_growth"], "")
        self.assertAlmostEqual(rows[2]["forecast_growth"], 10.0)
        self.assertEqual(rows[2]["forecast_positive"], 1.0)

    def test_stock_basic_all_includes_delisted(self):
        provider = _make_provider(_FakePro())
        all_stocks = provider.stock_basic_all()
        self.assertIn("000001.SZ", all_stocks)
        self.assertIn("000002.SZ", all_stocks)


if __name__ == "__main__":
    unittest.main()
