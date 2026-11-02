import unittest
from broker_results import extract_net_profit, get_binary_option_net_profit


class FakeBetInfo:
    def __init__(self, successful=True, payload=None):
        self.isSuccessful = successful
        self.dict = payload or {}


class FakeApi:
    def __init__(self, betinfo):
        self.game_betinfo = betinfo
        self.requested = []

    def get_betinfo(self, order_id):
        self.requested.append(order_id)


class FakeClient:
    def __init__(self, api):
        self.api = api


class BrokerResultTests(unittest.TestCase):
    def test_extracts_net_profit(self):
        payload = {"result": {"data": {"123": {"win": "win", "profit": 1.82, "deposit": 1.0}}}}
        self.assertEqual(extract_net_profit(payload, 123), 0.82)

    def test_unsettled_result_returns_none(self):
        payload = {"result": {"data": {"123": {"win": "", "profit": 0, "deposit": 1.0}}}}
        self.assertIsNone(extract_net_profit(payload, 123))

    def test_bounded_lookup(self):
        payload = {"result": {"data": {"123": {"win": "loose", "profit": 0, "deposit": 1.0}}}}
        api = FakeApi(FakeBetInfo(True, payload))
        self.assertEqual(get_binary_option_net_profit(FakeClient(api), 123, timeout=0.1), -1.0)
        self.assertEqual(api.requested, [123])

    def test_lookup_times_out(self):
        api = FakeApi(FakeBetInfo(None, {}))
        with self.assertRaises(TimeoutError):
            get_binary_option_net_profit(FakeClient(api), 123, timeout=0.05, poll_interval=0.01)


if __name__ == "__main__":
    unittest.main()
