"""Per-order EMA trade history persisted as a workflow artifact CSV."""
import csv
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


FIELDS = (
    "order_id", "entry_time_utc", "entry_time_wat", "pair", "direction",
    "stake", "payout_percent", "expiry_minutes", "recovery_attempt",
    "outcome", "net_profit",
)


class EmaTradeHistory:
    def __init__(self, path="ema_trade_history.csv"):
        self.path = Path(path)
        self.lock = threading.RLock()

    def record_accepted(self, order_id, asset, direction, stake, payout,
                        accepted_at, expiry_minutes, recovery_attempt):
        timestamp = float(accepted_at)
        utc = datetime.fromtimestamp(timestamp, timezone.utc)
        wat = utc.astimezone(ZoneInfo("Africa/Lagos"))
        row = {
            "order_id": str(order_id),
            "entry_time_utc": utc.isoformat(),
            "entry_time_wat": wat.isoformat(),
            "pair": str(asset),
            "direction": str(direction).upper(),
            "stake": f"{float(stake):.2f}",
            "payout_percent": "" if payout is None else f"{float(payout) * 100:.2f}",
            "expiry_minutes": int(expiry_minutes),
            "recovery_attempt": int(recovery_attempt),
            "outcome": "PENDING",
            "net_profit": "",
        }
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            exists = self.path.exists() and self.path.stat().st_size > 0
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                if not exists:
                    writer.writeheader()
                writer.writerow(row)
        return True

    def record_result(self, order_id, outcome, net_profit=None):
        wanted = str(order_id)
        with self.lock:
            if not self.path.exists():
                return False
            with self.path.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            found = False
            for row in rows:
                if row.get("order_id") == wanted:
                    row["outcome"] = str(outcome).upper()
                    row["net_profit"] = "" if net_profit is None else f"{float(net_profit):.2f}"
                    found = True
            if not found:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp",
                                             dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=FIELDS)
                    writer.writeheader()
                    writer.writerows(rows)
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return True
