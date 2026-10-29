import tempfile
import unittest
from pathlib import Path

from stockquant.data.versioning import build_manifest, file_sha256, verify_manifest, write_manifest


class VersioningTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.csv = self.dir / "data.csv"
        self.csv.write_text("date,symbol,close\n2018-01-02,S1,10\n2018-01-03,S1,11\n", encoding="utf-8")

    def test_sha256_detects_change(self):
        before = file_sha256(str(self.csv))
        self.csv.write_text(self.csv.read_text() + "2018-01-04,S1,12\n", encoding="utf-8")
        self.assertNotEqual(before, file_sha256(str(self.csv)))

    def test_manifest_shape(self):
        m = build_manifest(str(self.csv))
        self.assertEqual(m["rows"], 2)
        self.assertEqual(m["first_date"], "2018-01-02")
        self.assertEqual(m["last_date"], "2018-01-03")
        self.assertEqual(len(m["sha256"]), 64)

    def test_write_and_verify(self):
        write_manifest(str(self.csv))
        self.assertTrue(verify_manifest(str(self.csv)))
        self.csv.write_text("date,symbol,close\n2018-01-02,S1,10\n", encoding="utf-8")
        self.assertFalse(verify_manifest(str(self.csv)))

    def test_missing_date_field_raises(self):
        bad = self.dir / "bad.csv"
        bad.write_text("a,b\n1,2\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            build_manifest(str(bad))


if __name__ == "__main__":
    unittest.main()
