import unittest
from market_filters import choppiness_index, evaluate_market_filters

def candles(values):
    return [{"open": v - 0.01, "close": v, "high": v + 0.02, "low": v - 0.02, "from": i * 60} for i, v in enumerate(values)]

class MarketFilterTests(unittest.TestCase):
    def test_flat_market_is_choppy(self):
        flat = candles([1.0] * 40)
        self.assertEqual(choppiness_index(flat), 100.0)
        result = evaluate_market_filters(flat, candles([1.0 + i * 0.001 for i in range(60)]))
        self.assertTrue(result["ready"])
        self.assertTrue(result["consolidating"])

    def test_insufficient_data_fails_closed(self):
        result = evaluate_market_filters(candles([1.0] * 20), candles([1.0] * 30))
        self.assertFalse(result["ready"])

    def test_rising_five_minute_trend(self):
        result = evaluate_market_filters(candles([1.0 + i * 0.01 for i in range(40)]), candles([1.0 + i * 0.02 for i in range(60)]))
        self.assertTrue(result["ready"])
        self.assertEqual(result["trend"], "up")

if __name__ == "__main__":
    unittest.main()
