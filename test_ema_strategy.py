import unittest

from ema_strategy import adx, calculate_signal, ema, rsi


def make_trending_candles():
    candles = []
    price = 100.0
    for index in range(90):
        delta = (0.4, 0.4, -0.5)[index % 3]
        opening = price
        close = price + delta
        candles.append({
            "open": opening, "close": close,
            "high": max(opening, close) + 0.04,
            "low": min(opening, close) - 0.04,
            "from": 1_800_000_000 + index * 60,
        })
        price = close
    return candles


class EmaStrategyTests(unittest.TestCase):
    def test_ema_seed_and_smoothing(self):
        self.assertAlmostEqual(ema([1, 2, 3, 4], 3), 3.0)
        self.assertIsNone(ema([1, 2], 3))

    def test_rsi_range(self):
        value = rsi([100 + i * 0.2 for i in range(20)], 14)
        self.assertEqual(value, 100.0)
        self.assertTrue(0 <= rsi([100 + (-1) ** i for i in range(20)], 14) <= 100)

    def test_adx_warmup(self):
        self.assertIsNone(adx(make_trending_candles()[:28], 14))
        self.assertIsNotNone(adx(make_trending_candles(), 14))

    def test_ema_strategy_produces_valid_direction_when_filters_align(self):
        candles = make_trending_candles()
        signal, reason = calculate_signal("EURUSD", candles)
        self.assertEqual(reason, "")
        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "call")
        self.assertGreater(signal["adx14"], 20)
        self.assertGreater(signal["rsi14"], 50)
        self.assertLess(signal["rsi14"], 70)

    def test_insufficient_candles_do_not_signal(self):
        signal, reason = calculate_signal("EURUSD", make_trending_candles()[:40])
        self.assertIsNone(signal)
        self.assertEqual(reason, "insufficient closed candles")


if __name__ == "__main__":
    unittest.main()
