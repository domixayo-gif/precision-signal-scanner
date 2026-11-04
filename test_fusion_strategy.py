import unittest
from unittest.mock import patch

import fusion_strategy as fusion


def candles(count=70, step=60):
    output = []
    price = 100.0
    for index in range(count):
        close = price + 0.05
        output.append({
            "open": price,
            "close": close,
            "high": close + 0.1,
            "low": price - 0.1,
            "from": 1_800_000_000 + index * step,
        })
        price = close
    return output


class PrecisionFusionTests(unittest.TestCase):
    def setUp(self):
        self.one_minute = candles(70, 60)
        self.five_minute = candles(70, 300)
        stamp = self.one_minute[-1]["from"] + 60
        self.breakout = {
            "asset": "EURUSD",
            "direction": "call",
            "candle_time": stamp,
            "entry_time": stamp,
            "price": self.one_minute[-1]["close"],
            "level": 103.0,
            "zone": (102.9, 103.1),
            "source": "Pure breakout + first valid retest",
        }
        self.momentum = {"direction": "call", "candle_time": stamp, "score": 80}
        self.ema = {
            "direction": "call", "candle_time": stamp,
            "ema20": 103.0, "ema50": 102.0, "adx14": 27.0, "rsi14": 61.0,
        }

    def patch_all(self, direction="call", trend="up"):
        return (
            patch.object(fusion, "build_entry_signal", return_value=(self.breakout, None)),
            patch.object(fusion, "calculate_momentum_signal", return_value=(self.momentum, "")),
            patch.object(fusion, "calculate_ema_signal", return_value=(self.ema, "")),
            patch.object(fusion, "_alma_direction", return_value=direction),
            patch.object(fusion, "_chop_is_acceptable", return_value=(True, 48.0, "")),
            patch.object(fusion, "classify_trend", return_value=trend),
        )

    def test_all_six_components_must_agree(self):
        patches = self.patch_all()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            signal, reason = fusion.calculate_signal("EURUSD", self.one_minute, self.five_minute)
        self.assertEqual(reason, "")
        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "call")
        self.assertEqual(signal["confluence"], "B+M+E+ALMA+CHOP+5M")
        self.assertEqual(signal["score"], 6)

    def test_alma_disagreement_blocks_signal(self):
        patches = self.patch_all(direction="put")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            signal, reason = fusion.calculate_signal("EURUSD", self.one_minute, self.five_minute)
        self.assertIsNone(signal)
        self.assertIn("ALMA", reason)

    def test_five_minute_disagreement_blocks_signal(self):
        patches = self.patch_all(trend="sideways")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            signal, reason = fusion.calculate_signal("EURUSD", self.one_minute, self.five_minute)
        self.assertIsNone(signal)
        self.assertIn("5-minute trend disagreement", reason)

    def test_strategy_direction_disagreement_blocks_signal(self):
        self.momentum = {"direction": "put", "candle_time": self.breakout["candle_time"], "score": 80}
        patches = self.patch_all()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            signal, reason = fusion.calculate_signal("EURUSD", self.one_minute, self.five_minute)
        self.assertIsNone(signal)
        self.assertIn("direction disagreement", reason)

    def test_timestamp_mismatch_blocks_signal(self):
        self.momentum = {
            "direction": "call",
            "candle_time": self.breakout["candle_time"] - 60,
            "score": 80,
        }
        patches = self.patch_all()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            signal, reason = fusion.calculate_signal("EURUSD", self.one_minute, self.five_minute)
        self.assertIsNone(signal)
        self.assertIn("timestamp", reason)

    def test_insufficient_five_minute_data_blocks_signal(self):
        signal, reason = fusion.calculate_signal(
            "EURUSD", self.one_minute, self.five_minute[:40]
        )
        self.assertIsNone(signal)
        self.assertIn("5-minute", reason)

    def test_alma_returns_weighted_average(self):
        value = fusion.alma([1, 2, 3, 4, 5, 6, 7], 7)
        self.assertGreater(value, 1)
        self.assertLess(value, 7)
        self.assertIsNone(fusion.alma([1, 2], 7))

    def test_choppiness_requires_sufficient_candles(self):
        self.assertIsNone(fusion.choppiness_index(self.one_minute[:20], 25))


if __name__ == "__main__":
    unittest.main()
