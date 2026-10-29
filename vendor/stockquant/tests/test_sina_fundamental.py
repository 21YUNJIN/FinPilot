import unittest

from stockquant.data.sina_fundamental_provider import availability_date, latest_snapshot_asof


class AvailabilityDateTests(unittest.TestCase):
    def test_statutory_deadlines(self):
        self.assertEqual(availability_date("2023-03-31"), "2023-04-30")
        self.assertEqual(availability_date("2023-06-30"), "2023-08-31")
        self.assertEqual(availability_date("2023-09-30"), "2023-10-31")
        self.assertEqual(availability_date("2023-12-31"), "2024-04-30")

    def test_invalid_period(self):
        self.assertIsNone(availability_date("2023-05-31"))
        self.assertIsNone(availability_date(""))
        self.assertIsNone(availability_date("2023/12/31"))

    def test_no_lookahead(self):
        # A FY2023 report must NOT be available on the report date itself.
        self.assertGreater(availability_date("2023-12-31"), "2023-12-31")


class PitJoinTests(unittest.TestCase):
    def setUp(self):
        self.snaps = [
            {"report_period": "2023-03-31", "available_at": "2023-04-30", "eps": 1.0},
            {"report_period": "2023-06-30", "available_at": "2023-08-31", "eps": 2.0},
            {"report_period": "2023-12-31", "available_at": "2024-04-30", "eps": 4.0},
        ]

    def test_join_uses_only_available_reports(self):
        self.assertEqual(latest_snapshot_asof(self.snaps, "2023-04-29"), {})
        self.assertEqual(latest_snapshot_asof(self.snaps, "2023-04-30")["eps"], 1.0)
        self.assertEqual(latest_snapshot_asof(self.snaps, "2023-08-31")["eps"], 2.0)
        self.assertEqual(latest_snapshot_asof(self.snaps, "2024-01-01")["eps"], 2.0)
        self.assertEqual(latest_snapshot_asof(self.snaps, "2024-04-30")["eps"], 4.0)


if __name__ == "__main__":
    unittest.main()
