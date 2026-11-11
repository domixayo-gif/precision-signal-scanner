"""Accurate per-AUTO-session accounting from broker-confirmed order outcomes."""
from datetime import datetime, timezone


class SessionLedger:
    def __init__(self):
        self.started_at = None
        self.ended_at = None
        self.end_reason = ""
        self.accepted = 0
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.total_profit = 0.0
        self.total_loss = 0.0
        self.net_profit = 0.0
        self.pending = {}
        self.unconfirmed = set()
        self.order_ids = set()
        self.report_sent = True

    @property
    def started(self):
        return self.started_at is not None

    @property
    def ended(self):
        return self.ended_at is not None

    def start(self):
        if self.ended and not self.report_sent:
            return False
        self.started_at = datetime.now(timezone.utc)
        self.ended_at = None
        self.end_reason = ""
        self.accepted = self.wins = self.losses = self.draws = 0
        self.total_profit = self.total_loss = self.net_profit = 0.0
        self.pending = {}
        self.unconfirmed = set()
        self.order_ids = set()
        self.report_sent = False
        return True

    def record_accepted(self, order_id, stake, strategy):
        key = str(order_id)
        if not self.started or key in self.order_ids:
            return False
        self.order_ids.add(key)
        self.accepted += 1
        self.pending[key] = {"stake": float(stake), "strategy": str(strategy).upper()}
        return True

    def record_result(self, order_id, net_profit):
        key = str(order_id)
        if key not in self.pending:
            return False
        self.pending.pop(key)
        value = round(float(net_profit), 2)
        self.net_profit = round(self.net_profit + value, 2)
        if value > 0:
            self.wins += 1
            self.total_profit = round(self.total_profit + value, 2)
        elif value < 0:
            self.losses += 1
            self.total_loss = round(self.total_loss + abs(value), 2)
        else:
            self.draws += 1
        return True

    def mark_unconfirmed(self, order_id):
        key = str(order_id)
        if key not in self.pending:
            return False
        self.pending.pop(key)
        self.unconfirmed.add(key)
        return True

    def mark_non_ema_unconfirmed(self):
        for key, details in list(self.pending.items()):
            if details["strategy"] != "E":
                self.mark_unconfirmed(key)

    def end(self, reason):
        if not self.started or self.ended:
            return False
        self.ended_at = datetime.now(timezone.utc)
        self.end_reason = str(reason or "AUTO stopped")
        self.mark_non_ema_unconfirmed()
        return True

    def build_report(self):
        if not self.ended:
            return ""
        duration = self.ended_at - self.started_at
        minutes = max(0, int(duration.total_seconds() // 60))
        pending = len(self.pending)
        unknown = len(self.unconfirmed)
        lines = [
            "📒 AUTO SESSION REPORT",
            f"Duration: {minutes} min",
            f"End reason: {self.end_reason[:180]}",
            f"Accepted trades: {self.accepted}",
            f"Confirmed results: {self.wins + self.losses + self.draws}",
            f"Wins: {self.wins} | Losses: {self.losses} | Draws: {self.draws}",
            f"Total won: +${self.total_profit:.2f}",
            f"Total lost: -${self.total_loss:.2f}",
            f"NET BOT P/L: {'+' if self.net_profit >= 0 else '-'}${abs(self.net_profit):.2f}",
            f"Unconfirmed outcomes: {unknown + pending}",
        ]
        if pending:
            lines.append(f"Still awaiting broker result: {pending}")
        if unknown:
            lines.append(f"Broker result not verified: {unknown}")
        lines.append("P/L includes only broker-confirmed results; unconfirmed trades are excluded.")
        return "\n".join(lines)

    def ready_to_report(self):
        return self.ended and not self.pending and not self.report_sent

    def mark_report_sent(self):
        if self.ready_to_report():
            self.report_sent = True
            return True
        return False
