import csv
import tempfile
import unittest
from pathlib import Path

from ema_trade_history import EmaTradeHistory


class EmaTradeHistoryTests(unittest.TestCase):
    def test_records_accepted_trade_then_updates_confirmed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ema_trade_history.csv"
            history = EmaTradeHistory(path)
            history.record_accepted(
                12345, "EURUSD", "call", 1.22, 0.82,
                1_800_000_000, 2, 1,
            )
            self.assertTrue(history.record_result(12345, "WIN", 1.00))
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["order_id"], "12345")
            self.assertEqual(rows[0]["pair"], "EURUSD")
            self.assertEqual(rows[0]["direction"], "CALL")
            self.assertEqual(rows[0]["stake"], "1.22")
            self.assertEqual(rows[0]["payout_percent"], "82.00")
            self.assertEqual(rows[0]["recovery_attempt"], "1")
            self.assertEqual(rows[0]["outcome"], "WIN")
            self.assertEqual(rows[0]["net_profit"], "1.00")
            self.assertTrue(rows[0]["entry_time_wat"])

    def test_unverified_order_can_be_recorded_without_guessing_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            history = EmaTradeHistory(Path(directory) / "ema_trade_history.csv")
            history.record_accepted(99, "GBPUSD", "put", 2.0, 0.75,
                                    1_800_000_000, 2, 2)
            self.assertTrue(history.record_result(99, "UNVERIFIED"))
            with history.path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["outcome"], "UNVERIFIED")
            self.assertEqual(row["net_profit"], "")

    def test_unknown_order_result_does_not_create_a_row(self):
        with tempfile.TemporaryDirectory() as directory:
            history = EmaTradeHistory(Path(directory) / "ema_trade_history.csv")
            self.assertFalse(history.record_result(404, "LOSS", -1.0))


if __name__ == "__main__":
    unittest.main()
