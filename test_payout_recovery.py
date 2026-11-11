import unittest
from decimal import Decimal
from payout_recovery import (
    MAX_SINGLE_STAKE, StakeLimitExceeded, broker_turbo_payout,
    normalize_payout, stake_for_recovery,
)

class PayoutRecoveryTests(unittest.TestCase):
    def test_first_stake_uses_actual_payout_and_targets_two_dollars(self):
        self.assertEqual(stake_for_recovery(0, 0.82), 2.44)
        self.assertEqual(stake_for_recovery(0, 0.84), 2.39)
        self.assertEqual(stake_for_recovery(0, 0.60), 3.34)

    def test_recovery_stakes_include_all_confirmed_losses(self):
        self.assertEqual(stake_for_recovery(2.44, 0.82), 5.42)
        self.assertEqual(stake_for_recovery(7.86, 0.82), 12.03)
        self.assertEqual(stake_for_recovery(2.71, 0.74), 6.37)
        self.assertEqual(stake_for_recovery(9.08, 0.74), 14.98)

    def test_three_attempt_sequence_at_sixty_percent(self):
        first = stake_for_recovery(0, 0.60)
        second = stake_for_recovery(first, 0.60)
        third = stake_for_recovery(first + second, 0.60)
        self.assertEqual((first, second, third), (3.34, 8.90, 23.74))

    def test_changed_payout_is_used_for_next_recovery_order(self):
        first = stake_for_recovery(0, 0.82)
        second = stake_for_recovery(first, 0.74)
        self.assertEqual((first, second), (2.44, 6.00))

    def test_payout_can_be_ratio_or_percent(self):
        self.assertEqual(normalize_payout(0.82), normalize_payout(82))
        self.assertEqual(stake_for_recovery(0, 82), 2.44)

    def test_stake_rounds_up_to_cents(self):
        stake = stake_for_recovery(0, 0.83)
        self.assertGreaterEqual(stake * 0.83, 2.00)

    def test_hard_cap_blocks_stake_instead_of_underfunding_target(self):
        self.assertEqual(MAX_SINGLE_STAKE, Decimal('33.74'))
        with self.assertRaises(StakeLimitExceeded):
            stake_for_recovery(30, 0.60)

    def test_invalid_payout_is_rejected(self):
        for value in (0, -0.2, 101, None, "bad"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_payout(value)

    def test_broker_lookup_requires_exact_asset_and_turbo_payout(self):
        class FakeClient:
            def get_all_profit(self):
                return {"EURUSD-op": {"turbo": 0.82}}
        self.assertEqual(broker_turbo_payout(FakeClient(), "EURUSD-op"), 0.82)
        with self.assertRaises(RuntimeError):
            broker_turbo_payout(FakeClient(), "GBPUSD-op")

if __name__ == "__main__":
    unittest.main()
