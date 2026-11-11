import unittest
from session_reporting import SessionLedger


class SessionLedgerTests(unittest.TestCase):
    def test_sums_confirmed_wins_losses_and_draws(self):
        ledger = SessionLedger()
        ledger.start()
        ledger.record_accepted(1, 1, "E")
        ledger.record_accepted(2, 2, "E")
        ledger.record_accepted(3, 1, "E")
        ledger.record_result(1, 0.82)
        ledger.record_result(2, -2.00)
        ledger.record_result(3, 0.00)
        ledger.end("Stopped manually")
        report = ledger.build_report()
        self.assertEqual((ledger.wins, ledger.losses, ledger.draws), (1, 1, 1))
        self.assertEqual(ledger.total_profit, 0.82)
        self.assertEqual(ledger.total_loss, 2.00)
        self.assertEqual(ledger.net_profit, -1.18)
        self.assertIn("NET BOT P/L: -$1.18", report)
        self.assertTrue(ledger.ready_to_report())

    def test_unconfirmed_outcomes_are_not_guessed_as_losses(self):
        ledger = SessionLedger()
        ledger.start()
        ledger.record_accepted(11, 1, "E")
        ledger.end("Safety stop")
        self.assertFalse(ledger.ready_to_report())
        ledger.mark_unconfirmed(11)
        report = ledger.build_report()
        self.assertIn("Broker result not verified: 1", report)
        self.assertIn("NET BOT P/L: +$0.00", report)
        self.assertTrue(ledger.ready_to_report())

    def test_non_ema_pending_results_are_flagged_unconfirmed_at_session_end(self):
        ledger = SessionLedger()
        ledger.start()
        ledger.record_accepted(12, 1, "B")
        ledger.end("Stopped manually")
        self.assertEqual(len(ledger.pending), 0)
        self.assertIn("Broker result not verified: 1", ledger.build_report())

    def test_new_session_waits_until_previous_report_is_ready(self):
        ledger = SessionLedger()
        ledger.start()
        ledger.record_accepted(13, 1, "E")
        ledger.end("Stopped manually")
        self.assertFalse(ledger.start())
        ledger.mark_unconfirmed(13)
        self.assertTrue(ledger.mark_report_sent())
        self.assertTrue(ledger.start())

    def test_report_separates_unknown_order_acceptance_from_confirmed_trades(self):
        ledger = SessionLedger()
        ledger.start()
        ledger.note_unknown_order_request()
        ledger.end("Order request timed out")
        report = ledger.build_report()
        self.assertIn("Order requests with unknown broker acceptance: 1", report)
        self.assertIn("Accepted trades: 0", report)


if __name__ == "__main__":
    unittest.main()
