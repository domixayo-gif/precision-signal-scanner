import unittest

from ema_strategy import (
    ADX_MIN, CHOP_MAX, CHOP_PERIOD, MAX_EXTENSION_ATR,
    PULLBACK_TOLERANCE_ATR, MIN_CANDLES,
    adx, atr, calculate_signal, choppiness_index, ema, rsi,
)


def make_candles(count=300):
    candles = []
    price = 100.0
    for index in range(count):
        delta = 0.08 if index % 4 != 3 else -0.02
        opening = price
        close = price + delta
        candles.append({
            "open": opening, "close": close,
            "high": max(opening, close) + 0.03,
            "low": min(opening, close) - 0.03,
            "from": 1_800_000_000 + index * 60,
        })
        price = close
    return candles


class EmaStrategyTests(unittest.TestCase):
    def test_precision_parameters(self):
        self.assertEqual(CHOP_PERIOD, 25)
        self.assertEqual(ADX_MIN, 20.0)
        self.assertEqual(CHOP_MAX, 61.8)
        self.assertEqual(MAX_EXTENSION_ATR, 0.85)
        self.assertEqual(PULLBACK_TOLERANCE_ATR, 0.20)
        self.assertEqual(MIN_CANDLES, 260)

    def test_ema_seed(self):
        self.assertAlmostEqual(ema([1, 2, 3, 4], 3), 3.0)
        self.assertIsNone(ema([1, 2], 3))

    def test_rsi_range(self):
        self.assertEqual(rsi([100 + i * 0.2 for i in range(20)], 14), 100.0)

    def test_indicator_calculations(self):
        candles = make_candles()
        self.assertIsNotNone(adx(candles, 14))
        self.assertIsNotNone(atr(candles, 14))
        self.assertIsNotNone(choppiness_index(candles, 25))

    def test_requires_260_closed_one_minute_candles(self):
        signal, reason = calculate_signal("EURUSD", make_candles(259), candles_5m=make_candles(80))
        self.assertIsNone(signal)
        self.assertIn("insufficient closed candles", reason)

    def test_requires_5m_confirmation(self):
        signal, reason = calculate_signal("EURUSD", make_candles(300))
        self.assertIsNone(signal)
        self.assertIn("5-minute trend candles", reason)

    def test_rejects_invalid_ohlc(self):
        candles = make_candles()
        candles[-1]["high"] = -1
        signal, reason = calculate_signal("EURUSD", candles, candles_5m=make_candles(80))
        self.assertIsNone(signal)
        self.assertEqual(reason, "invalid candle OHLC")


if __name__ == "__main__":
    unittest.main()
