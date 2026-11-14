import csv
import tempfile
import unittest
from pathlib import Path

from ema_trade_history import EmaTradeHistory, FIELDS


class EmaTradeHistoryTests(unittest.TestCase):
    def test_records_accepted_trade_then_updates_confirmed_result_and_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ema_trade_history.csv"
            history = EmaTradeHistory(path)
            history.record_accepted(
                12345, "EURUSD", "call", 1.22, 0.82,
                1_800_000_000, 2, 1,
                {
                    "session_id": "run-123-1",
                    "strategy": "E",
                    "market_mode": "BOTH",
                    "balance_before": 125.50,
                    "candle_time": 1_799_999_940,
                    "price": 1.08123456,
                    "ema20": 1.0801,
                    "ema50": 1.0798,
                    "rsi14": 58.25,
                    "adx14": 24.5,
                },
            )
            self.assertTrue(history.record_result(
                12345, "WIN", 1.00, balance_after=126.50
            ))
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["order_id"], "12345")
            self.assertEqual(row["pair"], "EURUSD")
            self.assertEqual(row["direction"], "CALL")
            self.assertEqual(row["stake"], "1.22")
            self.assertEqual(row["payout_percent"], "82.00")
            self.assertEqual(row["recovery_attempt"], "1")
            self.assertEqual(row["outcome"], "WIN")
            self.assertEqual(row["net_profit"], "1.00")
            self.assertEqual(row["session_id"], "run-123-1")
            self.assertEqual(row["market_mode"], "BOTH")
            self.assertEqual(row["balance_before"], "125.50")
            self.assertEqual(row["balance_after"], "126.50")
            self.assertEqual(row["rsi14"], "58.2500")
            self.assertTrue(row["entry_time_wat"])
            self.assertTrue(row["result_time_utc"])
            self.assertEqual(len(row), len(FIELDS))

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
            self.assertEqual(row["balance_after"], "")

    def test_unknown_order_result_does_not_create_a_row(self):
        with tempfile.TemporaryDirectory() as directory:
            history = EmaTradeHistory(Path(directory) / "ema_trade_history.csv")
            self.assertFalse(history.record_result(404, "LOSS", -1.0))

    def test_old_csv_header_is_migrated_before_appending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ema_trade_history.csv"
            path.write_text(
                "order_id,outcome,net_profit\n1,WIN,0.80\n",
                encoding="utf-8",
            )
            history = EmaTradeHistory(path)
            history.record_accepted(2, "USDJPY", "put", 1.0, None,
                                    1_800_000_000, 2, 1)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["order_id"], "1")
            self.assertEqual(rows[0]["outcome"], "WIN")
            self.assertEqual(rows[1]["order_id"], "2")
            self.assertEqual(len(rows[1]), len(FIELDS))


if __name__ == "__main__":
    unittest.main()
