import unittest

from momentum_strategy import calculate_signal


def make_downtrend_with_bullish_reversal():
    candles = []
    price = 100.0
    for index in range(49):
        close = price - 0.1
        candles.append({
            "open": price, "close": close,
            "high": price + 0.05, "low": close - 0.05,
            "from": 1_800_000_000 + index * 60,
        })
        price = close
    candles.append({
        "open": price - 0.1, "close": price + 0.1,
        "high": price + 0.2, "low": price - 0.8,
        "from": 1_800_000_000 + 49 * 60,
    })
    return candles


class MomentumStrategyTests(unittest.TestCase):
    def test_insufficient_candles_do_not_signal(self):
        signal, reason = calculate_signal("EURUSD", [])
        self.assertIsNone(signal)
        self.assertEqual(reason, "insufficient candles")

    def test_momentum_signal_uses_close_time(self):
        candles = make_downtrend_with_bullish_reversal()
        signal, reason = calculate_signal("EURUSD", candles, 60)
        self.assertIsNone(signal, reason)
        self.assertEqual(signal["direction"], "call")
        self.assertEqual(signal["candle_time"], candles[-1]["from"] + 60)


if __name__ == "__main__":
    unittest.main()
