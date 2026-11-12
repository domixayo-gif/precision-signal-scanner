import unittest

from ema_strategy import (
    MIN_CANDLES, adx, atr, calculate_signal, current_trend_direction, choppiness_index,
    ema, ema_series, rsi,
)


def trending_candles(direction="up", count=320):
    candles = []
    price = 100.0
    deltas = (0.12, 0.08, 0.06, -0.07)
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

    # Create a pullback candle followed by a closed bounce/rejection candle.
    closes = [c["close"] for c in candles[:-2]]
    fast = ema(closes, 20)
    if direction == "up":
        candles[-2].update({
            "open": fast + 0.05, "close": fast - 0.05,
            "high": fast + 0.07, "low": fast - 0.08,
        })
    else:
        candles[-2].update({
            "open": fast - 0.05, "close": fast + 0.05,
            "high": fast + 0.08, "low": fast - 0.07,
        })
    fast = ema([c["close"] for c in candles[:-1]], 20)
    if direction == "up":
        opening, close = fast - 0.025, fast + 0.025
        low, high = fast - 0.045, close + 0.02
    else:
        opening, close = fast + 0.025, fast - 0.025
        high, low = fast + 0.045, close - 0.02
    candles[-1].update({"open": opening, "close": close, "high": high, "low": low})
    return candles



def higher_timeframe_candles(direction="up", count=100):
    output = []
    price = 100.0
    steps = (0.8, 0.6, 0.7, -0.25)
    for index in range(count):
        delta = steps[index % len(steps)]
        if direction == "down":
            delta = -delta
        opening = price
        close = price + delta
        output.append({
            "open": opening, "close": close,
            "high": max(opening, close) + 0.12,
            "low": min(opening, close) - 0.12,
            "from": 1_800_000_000 + index * 300,
        })
        price = close
    return output


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
        signal, reason = calculate_signal("EURUSD", trending_candles("up"), candles_5m=higher_timeframe_candles("up"))
        self.assertEqual(reason, "")
        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "call")
        self.assertGreater(signal["plus_di14"], signal["minus_di14"])
        self.assertLess(signal["extension_atr"], 0.85)

    def test_bearish_ema_pullback_rejection_signals_put(self):
        signal, reason = calculate_signal("EURUSD", trending_candles("down"), candles_5m=higher_timeframe_candles("down"))
        self.assertEqual(reason, "")
        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "put")
        self.assertGreater(signal["minus_di14"], signal["plus_di14"])

    def test_recovery_guard_confirms_same_direction_without_requiring_pullback(self):
        self.assertEqual(current_trend_direction(trending_candles("up"), higher_timeframe_candles("up")), "call")
        self.assertEqual(current_trend_direction(trending_candles("down"), higher_timeframe_candles("down")), "put")

    def test_recovery_guard_rejects_opposing_five_minute_trend(self):
        self.assertIsNone(current_trend_direction(trending_candles("up"), higher_timeframe_candles("down")))

    def test_recovery_guard_rejects_missing_or_insufficient_context(self):
        self.assertIsNone(current_trend_direction(trending_candles()[:MIN_CANDLES - 1], higher_timeframe_candles()))
        self.assertIsNone(current_trend_direction(trending_candles(), []))

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
        signal, reason = calculate_signal("EURUSD", candles, candles_5m=higher_timeframe_candles())
        self.assertIsNone(signal)
        self.assertTrue("CHOP" in reason or "ADX" in reason or "EMA alignment" in reason)

    def test_malformed_ohlc_is_blocked(self):
        candles = trending_candles()
        candles[-1]["high"] = candles[-1]["low"] - 1
        signal, reason = calculate_signal("EURUSD", candles, candles_5m=higher_timeframe_candles())
        self.assertIsNone(signal)
        self.assertIn("invalid candle OHLC", reason)

    def test_zero_or_nonfinite_close_is_blocked(self):
        candles = trending_candles()
        candles[-1]["close"] = float("nan")
        signal, reason = calculate_signal("EURUSD", candles, candles_5m=higher_timeframe_candles())
        self.assertIsNone(signal)
        self.assertEqual(reason, "invalid candle data")

    def test_missing_five_minute_context_is_blocked(self):
        signal, reason = calculate_signal("EURUSD", trending_candles("up"))
        self.assertIsNone(signal)
        self.assertIn("5-minute", reason)

    def test_opposing_five_minute_trend_blocks_entry(self):
        signal, reason = calculate_signal(
            "EURUSD", trending_candles("up"),
            candles_5m=higher_timeframe_candles("down"),
        )
        self.assertIsNone(signal)
        self.assertIn("5-minute trend mismatch", reason)

    def test_unordered_candle_timestamps_are_blocked(self):
        candles = trending_candles()
        candles[-1]["from"] = candles[-2]["from"]
        signal, reason = calculate_signal(
            "EURUSD", candles, candles_5m=higher_timeframe_candles()
        )
        self.assertIsNone(signal)
        self.assertIn("unordered candle timestamps", reason)


if __name__ == "__main__":
    unittest.main()
