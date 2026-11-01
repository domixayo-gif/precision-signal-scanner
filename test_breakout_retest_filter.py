import unittest

from breakout_retest_filter import (
    RetestState,
    apply_filter,
    evaluate,
)


def build_bullish_breakout(extra_candles):
    """Build two old resistance touches, a strong break, and a two-candle retest."""
    baseline_highs = [
        99.2, 99.3, 99.4, 99.5, 99.6, 100.0,
        99.9, 99.8, 99.7, 99.6, 99.5,
        99.6, 99.7, 99.8, 99.9, 100.0,
        99.9, 99.8, 99.7, 99.6, 99.5, 99.4, 99.3, 99.2,
        99.1, 99.2, 99.3, 99.4, 99.5, 99.6, 99.7, 99.8, 99.9,
    ]
    candles = [
        {"open": 99.0, "close": 99.1, "high": baseline_highs[i], "low": 98.5, "from": i * 60}
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


class BreakoutRetestProtectionTests(unittest.TestCase):
    def test_weak_down_candle_does_not_release_bullish_retest_veto(self):
        candles = build_bullish_breakout([
            {"open": 100.3, "close": 100.2, "high": 100.4, "low": 100.0},
        ])

        result = evaluate(candles)

        self.assertEqual(result.state, RetestState.PENDING)
        self.assertEqual(result.direction, "up")
        signal, verdict = apply_filter("put", result, mode="enforce")
        self.assertIsNone(signal)
        self.assertEqual(verdict, "VETO")

    def test_two_later_bullish_candles_confirm_after_weak_candle(self):
        candles = build_bullish_breakout([
            {"open": 100.3, "close": 100.2, "high": 100.4, "low": 100.0},
            {"open": 100.2, "close": 100.6, "high": 100.8, "low": 100.1},
            {"open": 100.6, "close": 101.1, "high": 101.3, "low": 100.5},
        ])

        result = evaluate(candles)

        self.assertEqual(result.state, RetestState.CONFIRMED)
        self.assertEqual(result.direction, "up")
        signal, verdict = apply_filter("put", result, mode="enforce")
        self.assertIsNone(signal)
        self.assertEqual(verdict, "VETO")

    def test_support_break_invalidates_bullish_retest(self):
        candles = build_bullish_breakout([
            {"open": 100.3, "close": 99.7, "high": 100.4, "low": 99.5},
        ])

        result = evaluate(candles)

        self.assertEqual(result.state, RetestState.INVALIDATED)
        signal, verdict = apply_filter("put", result, mode="enforce")
        self.assertEqual(signal, "put")
        self.assertEqual(verdict, "PASS")

    def test_support_wick_sweep_can_still_be_a_valid_retest(self):
        candles = build_bullish_breakout([])
        candles[32]["low"] = 99.6  # Wick below the narrow zone; close remains above support.

        result = evaluate(candles)

        self.assertEqual(result.state, RetestState.PENDING)
        self.assertEqual(result.direction, "up")


if __name__ == "__main__":
    unittest.main()
