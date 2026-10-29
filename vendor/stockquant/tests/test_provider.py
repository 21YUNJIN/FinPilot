import unittest

from stockquant.data.akshare_provider import normalize_symbol


class ProviderTests(unittest.TestCase):
    def test_normalize_symbol(self):
        self.assertEqual(normalize_symbol("600000"), "600000.SH")
        self.assertEqual(normalize_symbol("000001"), "000001.SZ")
        self.assertEqual(normalize_symbol("300750"), "300750.SZ")
        self.assertEqual(normalize_symbol("830799"), "830799.BJ")
        self.assertEqual(normalize_symbol("600000", "上海证券交易所"), "600000.SH")


if __name__ == "__main__":
    unittest.main()
