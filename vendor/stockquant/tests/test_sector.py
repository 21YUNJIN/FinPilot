import unittest

from stockquant.data.sector import coarsen, coarsen_map


class SectorTests(unittest.TestCase):
    def test_common_labels(self):
        self.assertEqual(coarsen("证券"), "金融")
        self.assertEqual(coarsen("城商行Ⅲ"), "金融")
        self.assertEqual(coarsen("数字芯片设计"), "TMT")
        self.assertEqual(coarsen("白酒Ⅲ"), "消费")
        self.assertEqual(coarsen("炼油化工"), "周期材料")
        self.assertEqual(coarsen("光伏电池组件"), "公用事业")
        self.assertEqual(coarsen("医疗器械"), "医药")
        self.assertEqual(coarsen("汽车零部件"), "制造")

    def test_fallback(self):
        self.assertEqual(coarsen(""), "其他")
        self.assertEqual(coarsen("综合Ⅲ"), "其他")

    def test_map(self):
        out = coarsen_map({"A": "证券", "B": "白酒Ⅲ"})
        self.assertEqual(out, {"A": "金融", "B": "消费"})


if __name__ == "__main__":
    unittest.main()
