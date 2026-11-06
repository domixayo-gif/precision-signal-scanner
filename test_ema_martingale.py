import unittest

from ema_martingale import (
    EMA_MARTINGALE_STAKES, MAX_STEPS, next_step_after_result, stake_for_step,
)


class EmaMartingaleTests(unittest.TestCase):
    def test_exact_stake_sequence(self):
        self.assertEqual(EMA_MARTINGALE_STAKES, (1.0, 2.0, 4.0))
        self.assertEqual([stake_for_step(i) for i in range(1, 4)], [1.0, 2.0, 4.0])
        self.assertEqual(MAX_STEPS, 3)

    def test_only_confirmed_loss_advances_same_sequence(self):
        self.assertEqual(next_step_after_result("LOSS", 1), 2)
        self.assertEqual(next_step_after_result("LOSS", 2), 3)

    def test_win_and_draw_end_sequence_and_reset_next_signal_to_one(self):
        self.assertIsNone(next_step_after_result("WIN", 1))
        self.assertIsNone(next_step_after_result("DRAW", 2))
        self.assertEqual(stake_for_step(1), 1.0)

    def test_third_loss_stops_without_eight_dollar_trade(self):
        self.assertIsNone(next_step_after_result("LOSS", 3))

    def test_unknown_result_cannot_advance(self):
        with self.assertRaises(ValueError):
            next_step_after_result("UNKNOWN", 1)

    def test_invalid_steps_are_rejected(self):
        for step in (0, 4, "bad"):
            with self.subTest(step=step):
                with self.assertRaises(ValueError):
                    stake_for_step(step)


if __name__ == "__main__":
    unittest.main()
