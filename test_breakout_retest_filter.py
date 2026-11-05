import unittest

from breakout_retest_filter import RetestState, build_entry_signal, evaluate


def build_bullish_breakout():
    baseline_highs = [
        99.2, 99.3, 99.4, 99.5, 99.6, 100.0,
        99.9, 99.8, 99.7, 99.6, 99.5,
        99.6, 99.7, 99.8, 99.9, 100.0,
        99.9, 99.8, 99.7, 99.6, 99.5, 99.4, 99.3, 99.2,
        99.1, 99.2, 99.3, 99.4, 99.5, 99.6, 99.7, 99.8, 99.9,
    ]
    candles = [
        {"open": 99.0, "close": 99.1,
         "high": baseline_highs[i] if i < len(baseline_highs) else 99.8,
         "low": 98.5, "from": i * 60}
        for i in range(33)
    ]
    for index in (5, 15):
        candles[index] = {
            "open": 99.1, "close": 99.2, "high": 100.0, "low": 98.5,
            "from": index * 60,
        }
    candles[30] = {
        "open": 99.8, "close": 101.0, "high": 101.2, "low": 99.7,
        "from": 30 * 60,
    }
    candles[31] = {
        "open": 100.9, "close": 100.6, "high": 101.0, "low": 100.1,
        "from": 31 * 60,
    }
    candles[32] = {
        "open": 100.6, "close": 101.2, "high": 101.3, "low": 100.5,
        "from": 32 * 60,
    }
    return candles


class FirstRetestEntryTests(unittest.TestCase):
    def test_first_valid_closed_retest_candle_is_the_entry(self):
        candles = build_bullish_breakout()[:32]
        signal, result = build_entry_signal("EURUSD-OTC", candles)
        self.assertEqual(result.state, RetestState.RETESTED)
        self.assertEqual(result.retest_index, 31)
        self.assertIsNone(result.confirmation_index)
        self.assertEqual(signal["direction"], "call")
        self.assertEqual(signal["candle_time"], candles[31]["from"] + 60)
        self.assertEqual(signal["entry_time"], candles[31]["from"] + 60)
        self.assertEqual(signal["retest_candle_time"], candles[31]["from"] + 60)
        self.assertIn("first valid closed retest", signal["source"])

    def test_no_extra_confirmation_candle_is_required(self):
        candles = build_bullish_breakout()[:32]
        candles[31]["close"] = 100.4
        candles[31]["open"] = 100.9
        signal, result = build_entry_signal("EURUSD-OTC", candles)
        self.assertEqual(result.state, RetestState.RETESTED)
        self.assertIsNotNone(signal)

    def test_old_retest_is_not_chased(self):
        candles = build_bullish_breakout()
        signal, result = build_entry_signal("EURUSD-OTC", candles)
        self.assertEqual(result.state, RetestState.RETESTED)
        self.assertEqual(result.retest_index, 31)
        self.assertIsNone(signal)

    def test_bearish_first_retest_maps_to_put(self):
        candles = build_bullish_breakout()[:32]
        inverted = [
            {"open": 200-c["open"], "close": 200-c["close"],
             "high": 200-c["low"], "low": 200-c["high"], "from": c["from"]}
            for c in candles
        ]
        signal, result = build_entry_signal("EURUSD-OTC", inverted)
        self.assertEqual(result.state, RetestState.RETESTED)
        self.assertEqual(signal["direction"], "put")

    def test_first_retest_that_breaks_level_is_invalidated(self):
        candles = build_bullish_breakout()[:32]
        candles[31] = {
            "open": 100.3, "close": 99.7, "high": 100.4, "low": 99.5,
            "from": 31 * 60,
        }
        result = evaluate(candles)
        self.assertEqual(result.state, RetestState.INVALIDATED)


if __name__ == "__main__":
    unittest.main()
