import unittest

from asset_execution import find_enabled_turbo_active_id


class TurboAssetExecutionTests(unittest.TestCase):
    def test_uses_exact_turbo_name_and_active_id(self):
        data = {"result": {"turbo": {"actives": {
            "101": {"name": "forex.GBPJPY-op", "enabled": True, "is_suspended": False}
        }}, "binary": {"actives": {
            "3": {"name": "forex.GBPJPY", "enabled": True, "is_suspended": False}
        }}}}
        self.assertEqual(find_enabled_turbo_active_id(data, "GBPJPY-op"), 101)

    def test_does_not_accept_binary_only_availability(self):
        data = {"result": {
            "binary": {"actives": {"3": {"name": "forex.GBPJPY", "enabled": True}}},
            "turbo": {"actives": {"101": {"name": "forex.GBPJPY-op", "enabled": False}}},
        }}
        self.assertIsNone(find_enabled_turbo_active_id(data, "GBPJPY-op"))

    def test_rejects_suspended_turbo_asset(self):
        data = {"result": {"turbo": {"actives": {
            "101": {"name": "forex.GBPJPY-op", "enabled": True, "is_suspended": True}
        }}}}
        self.assertIsNone(find_enabled_turbo_active_id(data, "GBPJPY-op"))

    def test_requires_exact_original_broker_name(self):
        data = {"result": {"turbo": {"actives": {
            "101": {"name": "forex.GBPJPY-op", "enabled": True}
        }}}}
        self.assertIsNone(find_enabled_turbo_active_id(data, "GBPJPY"))

    def test_supports_otc_names(self):
        data = {"result": {"turbo": {"actives": {
            "808": {"name": "forex.EURUSD-OTC", "enabled": "1", "is_suspended": "0"}
        }}}}
        self.assertEqual(find_enabled_turbo_active_id(data, "EURUSD-OTC"), 808)

    def test_missing_or_malformed_data_is_unavailable(self):
        for data in (None, {}, {"result": {"binary": {"actives": {}}}}):
            with self.subTest(data=data):
                self.assertIsNone(find_enabled_turbo_active_id(data, "GBPJPY-op"))


if __name__ == "__main__":
    unittest.main()
