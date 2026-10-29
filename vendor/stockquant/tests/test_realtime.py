import unittest

from stockquant.data.realtime import parse_sina_quotes, sina_code

SAMPLE = (
    'var hq_str_sh600309="万华化学,67.720,67.660,68.900,69.290,67.720,68.890,68.900,19876670,1366019035.000,'
    '21900,68.890,29000,68.900,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-09-30,15:34:59,00,";\n'
    'var hq_str_sz000651="格力电器,38.130,38.100,38.280,38.300,37.970,38.270,38.280,27418341,1047764644.000,'
    '0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-09-30,15:34:59,00,";\n'
)


class RealtimeTests(unittest.TestCase):
    def test_sina_code(self):
        self.assertEqual(sina_code("600309.SH"), "sh600309")
        self.assertEqual(sina_code("000651.SZ"), "sz000651")
        self.assertEqual(sina_code("830799.BJ"), "bj830799")

    def test_parse_quotes(self):
        quotes = parse_sina_quotes(SAMPLE)
        self.assertEqual(set(quotes), {"600309.SH", "000651.SZ"})
        q = quotes["600309.SH"]
        self.assertEqual(q["name"], "万华化学")
        self.assertAlmostEqual(q["price"], 68.90)
        self.assertAlmostEqual(q["prev_close"], 67.66)
        self.assertEqual(q["date"], "2026-09-30")
        self.assertEqual(q["time"], "15:34:59")
        self.assertGreater(q["change_pct"], 0)

    def test_parse_ignores_garbage(self):
        self.assertEqual(parse_sina_quotes("garbage\n\n"), {})


if __name__ == "__main__":
    unittest.main()


class SinaCodeConvertibleTests(unittest.TestCase):
    def test_convertible_bond_prefix(self):
        # 沪市转债 110/111/113/118 必须以 sh 前缀请求，深市 123/127/128 用 sz。
        # 之前一律落到 "sz"，导致所有沪市转债在实时盯盘里静默消失。
        self.assertEqual(sina_code("113046"), "sh113046")
        self.assertEqual(sina_code("110075"), "sh110075")
        self.assertEqual(sina_code("111000"), "sh111000")
        self.assertEqual(sina_code("118077"), "sh118077")
        self.assertEqual(sina_code("128134"), "sz128134")
        self.assertEqual(sina_code("127025"), "sz127025")
        self.assertEqual(sina_code("123288"), "sz123288")
        self.assertEqual(sina_code("113046.SH"), "sh113046")

    def test_stock_prefix_unchanged(self):
        self.assertEqual(sina_code("600519"), "sh600519")
        self.assertEqual(sina_code("000001"), "sz000001")
        self.assertEqual(sina_code("300750"), "sz300750")
        self.assertEqual(sina_code("688981"), "sh688981")
        self.assertEqual(sina_code("830799"), "bj830799")


class BidAskParsingTests(unittest.TestCase):
    """五档解析：paper 对账要靠它拿"卖一价"（买方实际成交价）。

    样本是 113042 上银转债的真实新浪返回（2026-10-12 收盘），
    卖一 50@117.138 与券商 App 显示一致。
    """

    SAMPLE = (
        'var hq_str_sh113042="上银转债,117.000,116.977,117.140,117.746,117.000,117.130,117.138,'
        '2738850,321477987.000,600,117.130,390,117.119,480,117.118,480,117.112,560,117.100,'
        '50,117.138,430,117.140,140,117.195,100,117.196,500,117.198,2026-10-12,15:00:00,00,";\n'
    )

    def test_ask_bid_levels(self):
        q = parse_sina_quotes(self.SAMPLE)["113042.SH"]
        self.assertAlmostEqual(q["ask1"], 117.138)      # 卖一 = 你买入要付的价
        self.assertAlmostEqual(q["bid1"], 117.130)      # 买一 = 你卖出能拿到的价
        self.assertEqual(int(q["ask1_volume"]), 50)
        self.assertEqual(int(q["bid1_volume"]), 600)
        self.assertAlmostEqual(q["price"], 117.140)
        self.assertGreater(q["ask1"], q["bid1"])        # 卖一必须高于买一

    def test_missing_levels_degrade_to_zero(self):
        """没有盘口（停牌/样本缺档）时必须是 0.0，不能编出一个价格。

        调用方（paper 对账）据此退化用最新价，并在界面提示"该债无盘口，请手动填"。
        """
        text = ('var hq_str_sh600309="万华化学,67.720,67.660,68.900,69.290,67.720,68.890,68.900,'
                '19876670,1366019035.000,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
                '2026-09-30,15:34:59,00,";\n')
        q = parse_sina_quotes(text)["600309.SH"]
        self.assertEqual(q["bid1"], 0.0)
        self.assertEqual(q["ask1"], 0.0)
        self.assertAlmostEqual(q["price"], 68.90)   # 最新价仍然可用


class TencentProviderTests(unittest.TestCase):
    """第二数据源的代码前缀与解析（网络调用不在这里测，避免单测依赖外网）。"""

    def test_tx_code(self):
        from stockquant.data.tencent import tx_code
        self.assertEqual(tx_code("113042"), "sh113042")     # 沪市转债
        self.assertEqual(tx_code("128142"), "sz128142")     # 深市转债
        self.assertEqual(tx_code("600519"), "sh600519")
        self.assertEqual(tx_code("000001"), "sz000001")
        self.assertEqual(tx_code("300750"), "sz300750")
        self.assertEqual(tx_code("113042.SH"), "sh113042")

    def test_kline_url_has_six_fields(self):
        """腾讯 K 线接口 param 必须 6 段，少一段会返回 bad params（2026-10-13 踩过）。"""
        from stockquant.data import tencent
        url = tencent._KLINE_URL.format(code="sh113042", end="", count=5)
        param = url.split("param=")[1]
        self.assertEqual(len(param.split(",")), 6)
