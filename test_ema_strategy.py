import unittest

from ema_strategy import (
    MIN_CANDLES, adx, atr, calculate_signal, choppiness_index,
    ema, ema_series, rsi,
)


def trending_candles(direction="up", count=320):
    candles = []
    price = 100.0
    deltas = (0.08, 0.06, 0.07, -0.05)
    for index in range(count):
        delta = deltas[index % len(deltas)]
        if direction == "down":
            delta = -delta
        opening = price
        close = price + delta
        candles.append({
            "open": opening, "close": close,
            "high": max(opening, close) + 0.025,
            "low": min(opening, close) - 0.025,
            "from": 1_800_000_000 + index * 60,
        })
        price = close

    # Make the latest closed candle a genuine EMA20 touch-and-rejection/bounce.
    closes = [c["close"] for c in candles[:-1]]
    fast = ema(closes, 20)
    if direction == "up":
        opening, close = fast - 0.025, fast + 0.025
        low, high = fast - 0.045, close + 0.02
    else:
        opening, close = fast + 0.025, fast - 0.025
        high, low = fast + 0.045, close - 0.02
    candles[-1].update({
        "open": opening, "close": close, "high": high, "low": low,
    })
    return candles


class EmaStrategyTests(unittest.TestCase):
    def test_ema_seed_and_smoothing(self):
        self.assertAlmostEqual(ema([1, 2, 3, 4], 3), 3.0)
        self.assertIsNone(ema([1, 2], 3))
        self.assertEqual(ema_series([1, 2, 3], 3), [None, None, 2.0])

    def test_rsi_handles_flat_and_directional_series(self):
        self.assertEqual(rsi([100] * 20, 14), 50.0)
        self.assertEqual(rsi([100 + i for i in range(20)], 14), 100.0)
        self.assertTrue(0 <= rsi([100 + (-1) ** i for i in range(20)], 14) <= 100)

    def test_adx_and_atr_warmup(self):
        candles = trending_candles()
        self.assertIsNone(adx(candles[:28], 14))
        self.assertIsNotNone(adx(candles, 14))
        self.assertIsNotNone(atr(candles, 14))

    def test_choppiness_is_bounded_for_valid_trend(self):
        value = choppiness_index(trending_candles(), 14)
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 0)
        self.assertLessEqual(value, 100)

    def test_bullish_ema_pullback_bounce_signals_call(self):
        signal, reason = calculate_signal("EURUSD", trending_candles("up"))
        self.assertEqual(reason, "")
        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "call")
        self.assertGreater(signal["plus_di14"], signal["minus_di14"])
        self.assertLess(signal["extension_atr"], 1.5)

    def test_bearish_ema_pullback_rejection_signals_put(self):
        signal, reason = calculate_signal("EURUSD", trending_candles("down"))
        self.assertEqual(reason, "")
        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "put")
        self.assertGreater(signal["minus_di14"], signal["plus_di14"])

    def test_insufficient_warmup_is_blocked(self):
        signal, reason = calculate_signal("EURUSD", trending_candles()[:MIN_CANDLES - 1])
        self.assertIsNone(signal)
        self.assertIn("insufficient", reason)

    def test_sideways_market_is_blocked(self):
        candles = []
        for index in range(320):
            close = 100.0 + (0.015 if index % 2 else -0.015)
            opening = 100.0
            candles.append({
                "open": opening, "close": close,
                "high": max(opening, close) + 0.01,
                "low": min(opening, close) - 0.01,
                "from": 1_800_000_000 + index * 60,
            })
        signal, reason = calculate_signal("EURUSD", candles)
        self.assertIsNone(signal)
        self.assertTrue("CHOP" in reason or "ADX" in reason or "EMA alignment" in reason)

    def test_malformed_ohlc_is_blocked(self):
        candles = trending_candles()
        candles[-1]["high"] = candles[-1]["low"] - 1
        signal, reason = calculate_signal("EURUSD", candles)
        self.assertIsNone(signal)
        self.assertIn("invalid candle OHLC", reason)

    def test_zero_or_nonfinite_close_is_blocked(self):
        candles = trending_candles()
        candles[-1]["close"] = float("nan")
        signal, reason = calculate_signal("EURUSD", candles)
        self.assertIsNone(signal)
        self.assertEqual(reason, "invalid candle data")


if __name__ == "__main__":
    unittest.main()
