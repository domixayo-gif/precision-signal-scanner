import unittest
from broker_results import extract_net_profit, get_binary_option_net_profit


class FakeApi:
    def __init__(self, payload=None):
        self.get_options_v2_data = payload
        self.requested = []

    def get_options_v2(self, limit, instruments):
        self.requested.append((limit, instruments))
        self.get_options_v2_data = {
            "msg": {"closed_options": [
                {"id": [123], "win": "loose", "win_amount": 0, "amount": 1.0}
            ]}
        }


class FakeClient:
    def __init__(self, api):
        self.api = api


class BrokerResultTests(unittest.TestCase):
    def test_extracts_net_profit_for_win_loss_and_draw(self):
        rows = [
            {"id": [1], "win": "win", "win_amount": 1.82, "amount": 1.0},
            {"id": [2], "win": "loose", "win_amount": 0, "amount": 1.0},
            {"id": [3], "win": "equal", "win_amount": 1.0, "amount": 1.0},
        ]
        self.assertEqual(extract_net_profit(rows, 1), 0.82)
        self.assertEqual(extract_net_profit(rows, 2), -1.0)
        self.assertEqual(extract_net_profit(rows, 3), 0.0)

    def test_missing_order_returns_none(self):
        self.assertIsNone(extract_net_profit([], 123))

    def test_bounded_lookup_uses_closed_options_history(self):
        api = FakeApi()
        self.assertEqual(get_binary_option_net_profit(FakeClient(api), 123, timeout=0.1), -1.0)
        self.assertEqual(api.requested, [(100, "binary,turbo")])

    def test_lookup_times_out(self):
        api = FakeApi({"msg": {"closed_options": []}})
        with self.assertRaises(TimeoutError):
            get_binary_option_net_profit(FakeClient(api), 999, timeout=0.05, poll_interval=0.01)


if __name__ == "__main__":
    unittest.main()
