import unittest

from execution_guard import TradeExposureGuard, classify_profit


class TradeExposureGuardTests(unittest.TestCase):
    def test_blocks_second_trade_on_same_asset_until_expiry(self):
        guard = TradeExposureGuard(max_open_trades=3, expiry_seconds=120, expiry_grace_seconds=0)
        guard.register("1", "EURUSD-OTC", "call", 1000)
        allowed, reason = guard.can_open("EURUSD-OTC", 1050)
        self.assertFalse(allowed)
        self.assertIn("same asset", reason)

    def test_allows_asset_again_after_expiry(self):
        guard = TradeExposureGuard(max_open_trades=3, expiry_seconds=120, expiry_grace_seconds=0)
        guard.register("1", "EURUSD-OTC", "call", 1000)
        allowed, _ = guard.can_open("EURUSD-OTC", 1120)
        self.assertTrue(allowed)

    def test_enforces_global_open_trade_limit(self):
        guard = TradeExposureGuard(max_open_trades=2, expiry_seconds=120, expiry_grace_seconds=0)
        guard.register("1", "EURUSD-OTC", "call", 1000)
        guard.register("2", "GBPUSD-OTC", "put", 1000)
        allowed, reason = guard.can_open("USDJPY-OTC", 1050)
        self.assertFalse(allowed)
        self.assertIn("maximum", reason)

    def test_prunes_expired_trades(self):
        guard = TradeExposureGuard(max_open_trades=1, expiry_seconds=120, expiry_grace_seconds=0)
        guard.register("1", "EURUSD-OTC", "call", 1000)
        self.assertEqual(guard.count(1120), 0)

    def test_classifies_broker_net_profit(self):
        self.assertEqual(classify_profit(0.80), "WIN")
        self.assertEqual(classify_profit(-1.0), "LOSS")
        self.assertEqual(classify_profit(0.0), "DRAW")


if __name__ == "__main__":
    unittest.main()
