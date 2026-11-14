import base64
import tempfile
import unittest
from pathlib import Path

from google_drive_upload import collect_audit_files


class GoogleDriveUploadTests(unittest.TestCase):
    def test_collects_only_expected_csvs_with_run_specific_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ema_trade_history.csv").write_text("order_id,outcome\n1,WIN\n")
            (root / "ema_scan_audit.csv").write_text("scan_time_utc\nnow\n")
            (root / "other.csv").write_text("private\n")
            files = collect_audit_files("run-42", directory)
            self.assertEqual(
                [item["name"] for item in files],
                [
                    "ema_trade_history_run_run-42.csv",
                    "ema_scan_audit_run_run-42.csv",
                ],
            )
            self.assertEqual(
                base64.b64decode(files[0]["content_base64"]).decode(),
                "order_id,outcome\n1,WIN\n",
            )

    def test_missing_audit_files_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(collect_audit_files("42", directory), [])


if __name__ == "__main__":
    unittest.main()
