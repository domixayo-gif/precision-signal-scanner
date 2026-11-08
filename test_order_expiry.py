import unittest
from order_expiry import calculate_raw_expiry


class RawExpiryTests(unittest.TestCase):
    def test_uses_server_clock_plus_full_duration(self):
        self.assertEqual(calculate_raw_expiry(1800000000, 2), 1800000120)

    def test_rounds_up_fractional_server_second(self):
        self.assertEqual(calculate_raw_expiry(1800000000.2, 2), 1800000121)

    def test_rejects_missing_or_invalid_server_time(self):
        for value in (None, 0, -1, float("nan"), "bad"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    calculate_raw_expiry(value, 2)

    def test_rejects_nonpositive_duration(self):
        with self.assertRaises(ValueError):
            calculate_raw_expiry(1800000000, 0)


if __name__ == "__main__":
    unittest.main()
