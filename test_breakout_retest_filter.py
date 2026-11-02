import unittest

from breakout_retest_filter import RetestState, apply_filter, evaluate


def build_bullish_breakout(extra_candles):
    """Build two old resistance touches, a strong break, and its first retest."""
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
        for i in range(33 + len(extra_candles))
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
        "open": 100.6, "close": 100.3, "high": 100.7, "low": 99.95,
        "from": 32 * 60,
    }
    for offset, candle in enumerate(extra_candles, start=33):
        candles[offset] = {**candle, "from": offset * 60}
    return candles


class ImmediateBreakoutRetestTests(unittest.TestCase):
    def test_first_retest_triggers_without_second_pullback_or_confirmation(self):
        candles = build_bullish_breakout([])
        result = evaluate(candles)
        self.assertEqual(result.state, RetestState.RETESTED)
        self.assertEqual(result.direction, "up")
        self.assertEqual(result.retest_index, 31)

    def test_retest_can_be_a_green_or_mixed_candle_if_level_holds(self):
        candles = build_bullish_breakout([])
        candles[31] = {
            "open": 100.2, "close": 100.6, "high": 100.8, "low": 100.1,
            "from": 31 * 60,
        }
        result = evaluate(candles)
        self.assertEqual(result.state, RetestState.RETESTED)
        self.assertEqual(result.direction, "up")

    def test_support_break_invalidates_instead_of_triggering_call(self):
        candles = build_bullish_breakout([])
        candles[31] = {
            "open": 100.3, "close": 99.7, "high": 100.4, "low": 99.5,
            "from": 31 * 60,
        }
        result = evaluate(candles)
        self.assertEqual(result.state, RetestState.INVALIDATED)
        signal, verdict = apply_filter("put", result, mode="enforce")
        self.assertEqual(signal, "put")
        self.assertEqual(verdict, "PASS")

    def test_first_retest_can_happen_several_candles_after_breakout(self):
        candles = build_bullish_breakout([
            {"open": 100.9, "close": 100.8, "high": 101.1, "low": 100.6},
            {"open": 100.8, "close": 100.7, "high": 101.0, "low": 100.5},
            {"open": 100.5, "close": 100.3, "high": 100.7, "low": 99.95},
        ])
        candles[31]["low"] = 100.6
        candles[32]["low"] = 100.5
        result = evaluate(candles)
        self.assertEqual(result.state, RetestState.RETESTED)
        self.assertEqual(result.retest_index, 33)

    def test_decisive_close_before_later_touch_invalidates_setup(self):
        candles = build_bullish_breakout([
            {"open": 99.8, "close": 100.4, "high": 100.6, "low": 99.7},
            {"open": 100.4, "close": 100.3, "high": 100.6, "low": 99.9},
        ])
        candles[31] = {
            "open": 100.3, "close": 99.7, "high": 100.4, "low": 99.5,
            "from": 31 * 60,
        }
        result = evaluate(candles)
        self.assertEqual(result.state, RetestState.INVALIDATED)
        self.assertEqual(result.retest_index, 31)

    def test_valid_bullish_retest_vetoes_opposing_put(self):
        result = evaluate(build_bullish_breakout([]))
        signal, verdict = apply_filter("put", result, mode="enforce")
        self.assertIsNone(signal)
        self.assertEqual(verdict, "VETO")

    def test_wick_sweep_can_still_be_a_valid_retest(self):
        candles = build_bullish_breakout([])
        candles[31]["low"] = 99.6
        result = evaluate(candles)
        self.assertEqual(result.state, RetestState.RETESTED)
        self.assertEqual(result.direction, "up")

    def test_bearish_breakout_retest_maps_to_put(self):
        candles = build_bullish_breakout([])
        inverted = [
            {"open": 200-c["open"], "close": 200-c["close"],
             "high": 200-c["low"], "low": 200-c["high"], "from": c["from"]}
            for c in candles
        ]
        result = evaluate(inverted)
        self.assertEqual(result.state, RetestState.RETESTED)
        self.assertEqual(result.direction, "down")


if __name__ == "__main__":
    unittest.main()
