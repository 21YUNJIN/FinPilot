import tempfile
import unittest
from pathlib import Path

from stockquant.analysis.data_diagnostics import (
    diagnose_csv_missingness,
    format_report,
    is_missing,
)


class DataDiagnosticsTests(unittest.TestCase):
    def _write_csv(self, text):
        tmp = Path(tempfile.mkdtemp()) / "data.csv"
        tmp.write_text(text, encoding="utf-8")
        return str(tmp)

    def test_is_missing(self):
        self.assertTrue(is_missing(""))
        self.assertTrue(is_missing("nan"))
        self.assertTrue(is_missing("N/A"))
        self.assertFalse(is_missing("1.5"))
        self.assertTrue(is_missing("unknown", extra_tokens=("unknown",)))

    def test_diagnose_rates(self):
        path = self._write_csv(
            "date,symbol,pe,roe\n"
            "2018-01-02,000001.SZ,10.0,0.15\n"
            "2018-01-02,000002.SZ,,0.12\n"
            "2019-01-02,000001.SZ,12.0,\n"
            "2019-01-02,000003.SZ,11.0,0.10\n"
        )
        report = diagnose_csv_missingness(path, fields=["pe", "roe"])
        self.assertEqual(report.total_rows, 4)
        self.assertAlmostEqual(report.field_rate("pe"), 0.25)
        self.assertAlmostEqual(report.field_rate("roe"), 0.25)
        self.assertEqual(len(report.by_year), 2)

    def test_missing_column_raises(self):
        path = self._write_csv("date,symbol,pe\n2018-01-02,000001.SZ,10.0\n")
        with self.assertRaises(ValueError):
            diagnose_csv_missingness(path, fields=["roe"])

    def test_format_report(self):
        path = self._write_csv(
            "date,symbol,pe\n2018-01-02,000001.SZ,\n2019-01-02,000002.SZ,10.0\n"
        )
        report = diagnose_csv_missingness(path, fields=["pe"])
        text = format_report(report)
        self.assertIn("Total rows: 2", text)
        self.assertIn("pe", text)


if __name__ == "__main__":
    unittest.main()
