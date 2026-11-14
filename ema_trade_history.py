"""Per-order EMA account audit CSV with broker-confirmed outcomes and signal context."""
import csv
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


FIELDS = (
    "order_id", "session_id", "strategy", "market_mode",
    "entry_time_utc", "entry_time_wat", "pair", "direction",
    "stake", "balance_before", "payout_percent", "expiry_minutes",
    "recovery_attempt", "candle_time_utc", "entry_price",
    "ema20", "ema50", "rsi14", "adx14",
    "outcome", "net_profit", "balance_after", "result_time_utc",
)


def _number(value, digits=8):
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError, OverflowError):
        return ""


class EmaTradeHistory:
    def __init__(self, path="ema_trade_history.csv"):
        self.path = Path(path)
        self.lock = threading.RLock()

    def record_accepted(self, order_id, asset, direction, stake, payout,
                        accepted_at, expiry_minutes, recovery_attempt,
                        metadata=None):
        metadata = metadata or {}
        timestamp = float(accepted_at)
        utc = datetime.fromtimestamp(timestamp, timezone.utc)
        wat = utc.astimezone(ZoneInfo("Africa/Lagos"))
        candle_time = metadata.get("candle_time")
        candle_utc = ""
        if candle_time not in (None, ""):
            try:
                candle_utc = datetime.fromtimestamp(
                    float(candle_time), timezone.utc
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass
        row = {
            "order_id": str(order_id),
            "session_id": str(metadata.get("session_id", "")),
            "strategy": str(metadata.get("strategy", "E")),
            "market_mode": str(metadata.get("market_mode", "")),
            "entry_time_utc": utc.isoformat(),
            "entry_time_wat": wat.isoformat(),
            "pair": str(asset),
            "direction": str(direction).upper(),
            "stake": _number(stake, 2),
            "balance_before": _number(metadata.get("balance_before"), 2),
            "payout_percent": "" if payout is None else _number(float(payout) * 100, 2),
            "expiry_minutes": int(expiry_minutes),
            "recovery_attempt": int(recovery_attempt),
            "candle_time_utc": candle_utc,
            "entry_price": _number(metadata.get("price")),
            "ema20": _number(metadata.get("ema20")),
            "ema50": _number(metadata.get("ema50")),
            "rsi14": _number(metadata.get("rsi14"), 4),
            "adx14": _number(metadata.get("adx14"), 4),
            "outcome": "PENDING",
            "net_profit": "",
            "balance_after": "",
            "result_time_utc": "",
        }
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            exists = self.path.exists() and self.path.stat().st_size > 0
            if exists:
                with self.path.open("r", newline="", encoding="utf-8") as handle:
                    old_header = next(csv.reader(handle), [])
                if old_header != list(FIELDS):
                    with self.path.open("r", newline="", encoding="utf-8") as handle:
                        old_rows = list(csv.DictReader(handle))
                    fd, temp_name = tempfile.mkstemp(
                        prefix=self.path.name, suffix=".tmp", dir=str(self.path.parent)
                    )
                    try:
                        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
                            writer = csv.DictWriter(handle, fieldnames=FIELDS)
                            writer.writeheader()
                            writer.writerows(old_rows)
                        os.replace(temp_name, self.path)
                    finally:
                        if os.path.exists(temp_name):
                            os.unlink(temp_name)
            exists = self.path.exists() and self.path.stat().st_size > 0
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                if not exists:
                    writer.writeheader()
                writer.writerow(row)
        return True

    def record_scan_summary(self, assets_available, assets_attempted, assets_scanned,
                            valid_candidates, rejections, auto_mode, manual_scan,
                            session_id="", market_mode="", timed_out=False, error=""):
        """Append one row per EMA rejection reason for transparent scan diagnostics."""
        scan_time = datetime.now(timezone.utc).isoformat()
        common = {
            "scan_time_utc": scan_time,
            "session_id": session_id,
            "strategy": "E",
            "market_mode": str(market_mode),
            "auto_mode": bool(auto_mode),
            "manual_scan": bool(manual_scan),
            "assets_available": int(assets_available),
            "assets_attempted": int(assets_attempted),
            "assets_scanned": int(assets_scanned),
            "valid_candidates": int(valid_candidates),
            "timed_out": bool(timed_out),
            "error": str(error or "")[:240],
        }
        reasons = list((rejections or {}).items()) or [("NONE", 0)]
        with self.lock:
            path = self.path.with_name("ema_scan_audit.csv")
            path.parent.mkdir(parents=True, exist_ok=True)
            exists = path.exists() and path.stat().st_size > 0
            with path.open("a", newline="", encoding="utf-8") as handle:
                fields = (
                    "scan_time_utc", "session_id", "strategy", "market_mode",
                    "auto_mode", "manual_scan", "assets_available",
                    "assets_attempted", "assets_scanned", "valid_candidates",
                    "rejection_reason", "rejection_count", "timed_out", "error",
                )
                writer = csv.DictWriter(handle, fieldnames=fields)
                if not exists:
                    writer.writeheader()
                for reason, count in reasons:
                    row = dict(common)
                    row["rejection_reason"] = str(reason)
                    row["rejection_count"] = int(count)
                    writer.writerow(row)
        return True

    def record_result(self, order_id, outcome, net_profit=None, balance_after=None):
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
                    row["net_profit"] = "" if net_profit is None else _number(net_profit, 2)
                    row["balance_after"] = _number(balance_after, 2)
                    row["result_time_utc"] = datetime.now(timezone.utc).isoformat()
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
