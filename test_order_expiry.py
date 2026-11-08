import unittest
from order_expiry import calculate_raw_expiry


class RawExpiryTests(unittest.TestCase):
    def test_selects_minute_aligned_expiry_nearest_two_minutes(self):
        # At an exact minute, the 2-minute expiry is exactly 120 seconds away.
        self.assertEqual(calculate_raw_expiry(1800000000, 2), 1800000120)

    def test_skips_next_minute_when_purchase_window_is_closing(self):
        # Only 25 seconds remain to the next minute, so the broker's next
        # eligible mark is two minutes from the current minute boundary.
        expiry = calculate_raw_expiry(1800000035, 2)
        self.assertEqual(expiry % 60, 0)
        self.assertGreater(expiry - 1800000035, 30)
        self.assertEqual(expiry, 1800000120)

    def test_fractional_server_time_uses_broker_aligned_mark(self):
        expiry = calculate_raw_expiry(1800000000.2, 2)
        self.assertEqual(expiry, 1800000120)
        self.assertEqual(expiry % 60, 0)

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
