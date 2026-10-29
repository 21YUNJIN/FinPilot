import os
import tempfile
import unittest

from stockquant.data.membership import deterministic_sample, load_membership_csv, write_membership_csv


class MembershipTests(unittest.TestCase):
    def test_roundtrip_and_sample(self):
        membership = {"2024-01-31": {"A", "B"}, "2024-02-29": {"B", "C"}}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "membership.csv")
            write_membership_csv(membership, path)
            loaded = load_membership_csv(path)
        self.assertEqual(loaded["2024-01-31"], {"A", "B"})
        self.assertEqual(deterministic_sample({"A", "B", "C"}, 2, seed=1), deterministic_sample({"A", "B", "C"}, 2, seed=1))


if __name__ == "__main__":
    unittest.main()
