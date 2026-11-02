import unittest

from trend_filter import classify_trend


def candles_from(closes):
    return [{"open": value, "close": value, "high": value + 0.01,
             "low": value - 0.01, "from": i * 300}
            for i, value in enumerate(closes)]


class FiveMinuteTrendTests(unittest.TestCase):
    def test_uptrend(self):
        self.assertEqual(classify_trend(candles_from([100 + i * 0.2 for i in range(60)])), "up")

    def test_downtrend(self):
        self.assertEqual(classify_trend(candles_from([120 - i * 0.2 for i in range(60)])), "down")

    def test_insufficient_data_is_sideways(self):
        self.assertEqual(classify_trend(candles_from([100 + i for i in range(20)])), "sideways")

    def test_flat_market_is_sideways(self):
        self.assertEqual(classify_trend(candles_from([100] * 60)), "sideways")

    def test_mixed_data_is_sideways(self):
        closes = [100 + i * 0.2 for i in range(55)] + [108, 107, 106, 105, 104]
        self.assertEqual(classify_trend(candles_from(closes)), "sideways")


if __name__ == "__main__":
    unittest.main()

