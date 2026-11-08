import unittest
from broker_results import extract_net_profit, get_binary_option_net_profit


WINNING_ORDER = {
    "id": [123],
    "win": "win",
    "win_amount": 1.82,
    "amount": 1.0,
}


class FakeApi:
    def __init__(self, payload=None, history_responses=None):
        self.get_options_v2_data = payload
        self.requested = []
        self.history_responses = list(history_responses or [])
        self.default_response = payload

    def get_options_v2(self, limit, instruments):
        self.requested.append((limit, instruments))
        if self.history_responses:
            self.get_options_v2_data = self.history_responses.pop(0)
        elif self.default_response is not None:
            self.get_options_v2_data = self.default_response
        else:
            self.get_options_v2_data = {
                "msg": {"closed_options": [dict(WINNING_ORDER)]}
            }


class FakeClient:
    def __init__(self, api, async_result=None):
        self.api = api
        self.async_result = async_result

    def get_async_order(self, order_id):
        return self.async_result or {}


class BrokerResultTests(unittest.TestCase):
    def test_extracts_net_profit_for_win_loss_and_draw(self):
        rows = [
            dict(WINNING_ORDER),
            {"id": [2], "win": "loose", "win_amount": 0, "amount": 1.0},
            {"id": [3], "win": "equal", "win_amount": 1.0, "amount": 1.0},
        ]
        self.assertEqual(extract_net_profit(rows, 123), 0.82)
        self.assertEqual(extract_net_profit(rows, 2), -1.0)
        self.assertEqual(extract_net_profit(rows, 3), 0.0)

    def test_extracts_order_specific_async_result(self):
        from broker_results import extract_async_order_net_profit
        self.assertEqual(
            extract_async_order_net_profit(
                {"option-closed": {"msg": {"profit_amount": 1.82, "amount": 1.0}}}
            ),
            0.82,
        )
        self.assertIsNone(extract_async_order_net_profit({"option-closed": {}}))

    def test_order_specific_async_result_is_checked_before_history(self):
        api = FakeApi({"msg": {"closed_options": []}})
        client = FakeClient(
            api,
            {"option-closed": {"msg": {"profit_amount": 0, "amount": 1}}},
        )
        self.assertEqual(
            get_binary_option_net_profit(client, 123, timeout=0.1),
            -1.0,
        )
        self.assertEqual(api.requested, [])

    def test_missing_order_returns_none(self):
        self.assertIsNone(extract_net_profit([], 123))

    def test_bounded_lookup_uses_closed_options_history(self):
        api = FakeApi()
        self.assertEqual(
            get_binary_option_net_profit(FakeClient(api), 123, timeout=0.5),
            0.82,
        )
        self.assertEqual(api.requested, [(100, "binary,turbo")])

    def test_refreshes_history_when_first_snapshot_is_stale(self):
        api = FakeApi(
            history_responses=[
                {"msg": {"closed_options": []}},
                {"msg": {"closed_options": [dict(WINNING_ORDER)]}},
            ]
        )
        result = get_binary_option_net_profit(
            FakeClient(api), 123, timeout=1.0, poll_interval=0.02
        )
        self.assertEqual(result, 0.82)
        self.assertGreaterEqual(len(api.requested), 2)

    def test_lookup_times_out_without_guessing_an_outcome(self):
        api = FakeApi({"msg": {"closed_options": []}})
        with self.assertRaises(TimeoutError):
            get_binary_option_net_profit(
                FakeClient(api), 999, timeout=0.05, poll_interval=0.01
            )


if __name__ == "__main__":
    unittest.main()
