import unittest
from ema_market_recovery import recovery_quality_score, rank_recovery_candidates

def candidate(asset="EURUSD", direction="call", **overrides):
    value = {"asset": asset, "direction": direction, "candle_time": 1000, "adx14": 35.0, "plus_di14": 32.0, "minus_di14": 12.0, "chop25": 38.0, "extension_atr": 0.20, "rsi14": 60.0}
    value.update(overrides); return value

class MarketRecoveryRankingTests(unittest.TestCase):
    def test_stronger_qualified_candidate_ranks_first(self):
        strong = candidate(); weak = candidate("GBPUSD", adx14=23, plus_di14=22, minus_di14=18, chop25=58, extension_atr=0.75, rsi14=51)
        ranked = rank_recovery_candidates([weak, strong], now=1010)
        self.assertEqual(ranked[0]["asset"], "EURUSD")
        self.assertGreater(ranked[0]["_recovery_quality_score"], ranked[1]["_recovery_quality_score"])
    def test_put_direction_uses_minus_di_advantage(self):
        self.assertGreater(recovery_quality_score(candidate("USDJPY", "put", plus_di14=10, minus_di14=35, rsi14=40)), 70)
    def test_stale_and_future_candidates_are_rejected(self):
        self.assertEqual(rank_recovery_candidates([candidate(candle_time=800), candidate("GBPUSD", candle_time=1020)], now=1000), [])
    def test_invalid_and_nonfinite_candidates_are_rejected(self):
        self.assertIsNone(recovery_quality_score(candidate(direction="sideways")))
        self.assertIsNone(recovery_quality_score(candidate(adx14=float("nan"))))
    def test_ranking_does_not_mutate_input(self):
        original = candidate(); ranked = rank_recovery_candidates([original], now=1000)
        self.assertNotIn("_recovery_quality_score", original); self.assertIn("_recovery_quality_score", ranked[0])

if __name__ == "__main__": unittest.main()
